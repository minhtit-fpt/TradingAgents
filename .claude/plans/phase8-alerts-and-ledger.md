# Phase 8 — push the attention, log the decision

Deliverables: `tradingagents/value/alerts/*`, `tradingagents/value/jobs/daily.py`,
`tradingagents/value/decisions.py`. Tests: `tests/value/test_alerts.py`,
`test_dedupe.py`, `test_decisions.py`, `test_daily.py`.
Branch: `feat/value-p8-alerts`. Depends on phase 7 being merged.

## Why this phase exists

Phase 7 built the pull surface: the operator names a ticker, the machine
assembles evidence. That is the right shape for the *decision* — 4b and 6 both
showed automation deciding when to buy does not work. But it left two holes.

**Hole 1: pull requires the operator to already suspect something.** The trigger
price for PG is 130.49 and today's price is 143.45. Nothing tells anyone when
143 becomes 130. Phase 7's answer was "you asked, so you were watching", which
is not a mechanism, it is a hope. Notification is not the failed thing. What 4b
and 6 measured is a machine *choosing*; a machine *pointing* costs $0, decides
nothing, and is deterministic — the trigger price is arithmetic on numbers this
module already computes daily.

**Hole 2: no feedback loop exists at all.** Every decision the operator makes
from these dossiers vanishes the moment the terminal scrolls. In three years
there will be a portfolio and no record of why any of it was bought, and — worse
— no record of what was looked at and declined. The declined names are the
counterfactual; without them a journal is only survivorship applied to oneself.
This costs approximately nothing to build and cannot be built retroactively.

Phase 8 is those two holes and nothing else.

## What changed since the master plan

Section 5 of `long-term-value-investing.md` specced `alerts/*` as phase 7 and a
`alerts(ticker, trigger_date, mos_pct, sent_at)` table. That spec predates 4b and
6, so its tier-3 role is wrong and it has no decision log. Three corrections:

1. **Tier 3 is a briefing, not a gate.** The master plan's daily flow reads
   `triggered tickers -> value_analyst -> Telegram`, implying the analyst stands
   between the trigger and the send. It does not, here. The MoS trigger alone
   decides that an alert goes out; the filing read is attached to a message
   already committed. Phase 6 measured the gate: over 44 filings the analyst
   returned zero `avoid`, and `caution` on **70%** of this screen's picks — so as
   a gate it carries almost no information. (Written as 82% from phase 6's
   original run; re-measured at 70% on the same 44 after the extraction fix, with
   the `avoid` column empty under both. See the results section below.)
2. **`verdict` comes off the decision path.** It stays in the schema (removing
   it would change the prompt and break comparability with phase 6's 44-filing
   sample) but it renders **last**, beside that share, so it cannot be
   over-read. What phase 6 found to be specific and checkable was
   `accounting_flags`, `key_risks` and `evidence_gaps` — the Gillette intangible
   with <10% headroom, TXN's 219-day inventory, a truncated MD&A named as a gap.
   Those lead.
3. **Env namespace.** Master plan §9.7 names `TELEGRAM_BOT_TOKEN`. The isolation
   contract requires `VALUE_*`. Go with `VALUE_TELEGRAM_BOT_TOKEN` /
   `VALUE_TELEGRAM_CHAT_ID`.

Also dropped: the weekly near-miss digest. The daily heartbeat already carries
"closest candidate at X% MoS", which is the same information one line earlier.
Two channels for one fact is two things to maintain.

## Requirements

1. **Alerts.** A name crossing the MoS trigger produces one Telegram message.
   Dedupe on `(ticker, trigger_date)` so a retried or rebooted cron does not
   re-send. The dedupe row is written **before** the send, per master plan §9.4.
2. **Heartbeat.** One Telegram line per day regardless of whether anything
   triggered: ran OK, N screened, M passed, K triggered, closest candidate at
   X% MoS. Mandatory, not optional — silence is this screen's normal state, so a
   dead cron and a quiet market are otherwise indistinguishable.
3. **Briefing order.** Every surface that renders a `ValueAssessment` — the
   alert and `report.py` section 4 — leads with accounting flags, key risks and
   evidence gaps. `verdict` renders last, annotated. No code branches on it.
4. **Portfolio guidance in the alert.** A constant footer stating the 15% position
   cap and 5-name minimum, with the measurement attached: phase 4b step 5 cut max
   drawdown from 48.6% to 18.9% with the cap alone, while CAGR *rose*. Stated as
   a measurement of this backtest, not as sizing for the operator's account.
5. **Decision log.** Append-only. Every decision, including the ones to do
   nothing, with the numbers as they stood at that moment so a review in 2029
   does not have to re-derive them.
6. Nothing executes an order. Nothing sizes a real position. The alert names no
   action.

## Non-goals

- No broker, no order, no position tracking against a real account. The decision
  log records what the operator says they did; it does not verify or reconcile it.
- No re-litigating 4b or 6. Alerts route attention; they do not restore the
  automated entry decision those phases closed.
- No fix for the phase-6 `fetch_10k` pagination defect (only bites on filings 10+
  years old; the daily job reads the current one).
- No backfill of the decision log. It starts empty and that is correct.

## Design

### Store — two tables in `store/db.py`

```sql
CREATE TABLE IF NOT EXISTS alerts (
    ticker       TEXT NOT NULL,
    trigger_date TEXT NOT NULL,
    mos_pct      REAL NOT NULL,
    price        REAL NOT NULL,
    queued_at    TEXT NOT NULL,   -- written before the send
    sent_at      TEXT,            -- NULL until Telegram confirms
    PRIMARY KEY (ticker, trigger_date)
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    decided_on      TEXT NOT NULL,
    action          TEXT NOT NULL,  -- buy | add | trim | sell | pass | watch
    reason          TEXT NOT NULL,
    price           REAL,
    intrinsic_value REAL,
    mos_pct         REAL,
    screen_passed   INTEGER,
    filing_read     INTEGER NOT NULL DEFAULT 0,
    recorded_at     TEXT NOT NULL
);
```

`sent_at NULL` is the retry handle: a Telegram outage leaves a queued row, and
the next run re-sends it rather than losing the alert forever. Dedupe is on the
primary key, so a re-send is still one message per trigger date.

`decisions` is append-only — no UPDATE, no DELETE. Changing your mind is a new
row with `action` set accordingly. A journal that can be edited after the fact
is not evidence about your past self.

### `alerts/` — three small modules

| module | contents |
|---|---|
| `telegram.py` | one `requests.post` to `sendMessage`; raises on non-200, never swallows |
| `dedupe.py` | `should_send(conn, ticker, trigger_date) -> bool`, `queue()`, `confirm()` |
| `message.py` | composition: trigger alert body, heartbeat line, briefing block |

`message.py` is where requirements 3 and 4 live, and it is pure — takes an
`Outcome` plus an optional `ValueAssessment`, returns a string. That is what the
tests assert against; no network in the test path.

Alert body order:

```
PG at 30% MoS — 2026-11-14
price 129.80 vs intrinsic 186.42  (MoS +30.4%)
screen: PASSED 12/12 blocking, criteria-clean 96%

accounting flags: <...>
key risks: <...>
evidence gaps: <...>
thesis: <...>
verdict: caution — phase 6: covers 70% of picks, near-zero information

Sizing measured in backtest, not advice: 15% max per name, 5 names minimum.
Phase 4b step 5: that cap alone cut max drawdown 48.6% -> 18.9%, CAGR unchanged.
Evidence for your decision. Not a recommendation, not an order.
Log what you decide: python -m tradingagents.value.decide --ticker PG
```

### `jobs/daily.py` — the cron entry

```
tier 1  new 10-K filings since last run -> re-ingest those tickers        $0
tier 2  runner.run(conn, today) -> outcomes                               $0
tier 3  for each newly-triggered name only: report.read_filing            ~$0.05
        heartbeat, always                                                 $0
```

Tier 3 fires only on names that pass `dedupe.should_send`, so a name that stays
below trigger for a month is read once, not thirty times. Volume per master plan
§7 is ~10–20/quarter, so ~$1/year against a $10/month cap that already fails
closed. Budget exhaustion aborts and alerts; it never silently continues, and it
never suppresses the trigger alert — a briefing that could not be produced is a
missing paragraph, not a missing alert.

Reused as-is: `screen.runner.run`, `report.read_filing`, `report.numeric_summary`,
`llm.budget.Budget`, `llm.cache`, `edgar.*`. New code is the three alert modules,
the job, and the decision log.

### `decisions.py` — log and CLI

```bash
python -m tradingagents.value.decide --ticker PG --action buy \
    --why "entry price hit; Gillette intangible already impaired in FY2019"
```

Snapshots price / intrinsic / MoS / screen-passed by calling `report.build` at
record time ($0, no filing read unless `--read-filing` was what prompted it).
`--action pass` is not a second-class row: it is the only way the log ever
answers "what did I turn down, and was I right".

```bash
python -m tradingagents.value.decisions --ticker PG      # read one name back
python -m tradingagents.value.decisions --since 2027-01-01
```

Review tooling beyond a chronological dump is deliberately absent. What the
right review looks like is unknowable until there are rows; guessing now builds
the wrong report.

### `config.py` additions

| var | default | note |
|---|---|---|
| `VALUE_TELEGRAM_BOT_TOKEN` | `""` | empty = alerts print to stdout instead of sending |
| `VALUE_TELEGRAM_CHAT_ID` | `""` | same |
| `VALUE_ALERT_DRY_RUN` | `0` | compose and log, never send |

Empty token falling back to stdout is a deliberate exception to the
never-default rule: it is the dev path, it is loud (every message visible), and
the alternative is that no one can run the job locally without a bot.

## Steps

| # | Step | Acceptance |
|---|---|---|
| 1 | Two tables + accessors in `store/db.py` | round-trip tests; `decisions` append-only asserted |
| 2 | `alerts/message.py` | composition tests: flags before verdict, cap footer present, no action verb |
| 3 | `alerts/dedupe.py` | same `(ticker, date)` sends once across three simulated runs; a `sent_at IS NULL` row retries |
| 4 | `alerts/telegram.py` | non-200 raises; dry-run path sends nothing |
| 5 | `report.py` section 4 reorder | existing `test_report.py` extended; verdict last, annotated |
| 6 | `jobs/daily.py` | fake-clock run with zero triggers still emits exactly one heartbeat |
| 7 | `decisions.py` + CLI | `pass` and `buy` both record; snapshot columns populated |
| 8 | `tests/value/test_isolation.py` still green | no new import outside the allowlist, no edit outside `value/` |

## Risk

**Reopening the automated path by the back door.** This phase adds a machine that
speaks first, which is the shape 4b and 6 argued against. The separation that
makes it defensible is narrow and must stay narrow: the alert fires on
arithmetic, carries no action verb, and the only thing that writes to
`decisions` is a human typing a command. If a future phase makes anything
downstream of `alerts/` write a position, that separation is gone.

**Alert fatigue inverting the intent.** A daily heartbeat plus rare triggers is
one message a day, and messages that arrive every day stop being read — at which
point the trigger alert arrives into a muted channel and the system is worse than
pull-only. Mitigation is the message shape, not the frequency: the heartbeat is
one line, the trigger alert is a block. If they start to look alike, the
heartbeat gets quieter, never the alert.

**The log's value is entirely in the discipline.** A decision log with half the
decisions in it is worse than none — it will be read as complete. The `pass`
action exists for exactly this and the `--why` field is required, not optional.

## Not investment advice

This module produces research output and notifications. It executes nothing,
sizes nothing, and recommends nothing. The position figures in the alert footer
are a restatement of a backtest measurement over 2014–2026, and that same
backtest did not beat SPY.

## Result

Built on `feat/value-p8-alerts`, verified on 2026-08-19 against the live store.
329 tests in `tests/value/` pass, 905 across the repo, `ruff check .` clean, and
`test_isolation.py` still green — nothing outside `tradingagents/value/` was
touched except `CLAUDE.md`.

Live free-path run over the real store, `--dry-run --no-filing`:

```
2026-08-19 ok — screened 575, passed 10, triggered 3, closest DECK +66.7% MoS
```

Three names at the trigger (ADBE +58.9%, DECK +66.7%, RMD +52.3%), each composing
an alert that leads with the numbers, says the filing was not read, and closes
with the sizing measurement and the two commands. 30 seconds wall clock, $0.

### Three deviations from the design above

1. **No `alerts/dedupe.py`.** Its entire body would have been
   `not db.alert_queued(...)`. The predicate and the state live together in
   `store/db.py` with every other piece of this module's state; `alerts/` is two
   modules, not three.
2. **No daily-index crawler for tier 1.** The master plan's tier 1 re-screens on
   new filings. But a 10-K lands once a year while the margin of safety moves
   every day, and it moves because the *price* moved — which needs no EDGAR call
   at all. `daily.refresh` instead re-ingests the names whose newest fact is more
   than 400 days old, a few per run, and the crawler is not built.
3. **`report.render` delegates section 4 to `alerts.message.briefing`.** The
   ordering is the phase's actual claim, so it exists once rather than twice.

### One defect the live run found

The first live `--dry-run` wrote three rows into the `alerts` table. A rehearsal
claiming the dedupe row would have silenced the real run behind it — the same
lost alert the write-before-send ordering exists to prevent, arriving by the
other door. `record=False` now skips the table in both directions, a test pins
it, and the three stray rows were deleted from the store.

### What this does not settle

Whether alerts help. There is no measurement here and cannot be one for years —
which is the entire reason the decision log ships in the same phase as the thing
that generates decisions. The log starts empty; if it is still nearly empty in a
year, that is the finding.

## A phase 1-2 defect the phase-8 briefing exposed

The first paid dossier run under the new field order (DECK, $0.0432) put
`evidence_gaps` near the top, where it said: *"The provided Item 7 excerpt
contains no results-of-operations, segment, liquidity, or cash-flow discussion;
it reads as Item 1 operational text rather than MD&A."*

It was right. `edgar/filings.py` could not tell a heading from a filing's
citation of its own items, and filings cite themselves constantly — `Item 7,
"Management's Discussion and Analysis…"` occurs eight times inside DECK's Item 1.
Those citations broke extraction in both directions: they opened spans that were
really the tail of another section and won on length, and they closed real
sections early.

Two citation styles, needing two tests, because the punctuation lands in
different places:

```
DECK   Part II, Item 7, "Management's Discussion…"      comma after the number
KO     in Part I, "Item 1. Business" of this report     quote before it
```

KO's is why an after-the-number rule alone is not enough: its number sits inside
the quotation marks, so what follows is the same full stop a real heading uses.

Measured over six real 10-Ks, **five were extracting the wrong MD&A**; KO was
also returning 96k of MD&A text under `risk_factors`. Fixed in
`filings._is_citation`, with `SelfCitationTest` covering both styles.

**This reaches backwards, and the sample was re-run to find out how far.**
`backtest/llm_sample.py` uses the same `sections_for`, so phase 6's sample — the
source of the 82% figure this phase was built to print in every alert — ran on the
same broken extraction. Re-run on 2026-08-20 at phase 6's own settings
(`--years 7 --tolerance 1`, $1.64): the same 49 events, the same 44 assessed, the
same 5 `fetch_10k` failures, the same unfiltered book.

| | `caution` | `proceed` | `avoid` |
|---|---|---|---|
| broken extractor | 36 | 8 | 0 |
| fixed extractor | **31** | **13** | 0 |

**82% was wrong; the figure is 70%.** Every surface that printed it — the alert
annotation, `report.py` section 4, `CLAUDE.md`, the sample message above — now
prints 70%, and `test_alerts.py` / `test_report.py` pin it.

What does not move is the reason `verdict` is off the decision path: zero `avoid`
under both extractors, 0% of held slots cut, CI exactly [+0.00%, +0.00%]. The
gate finding was never a function of the `caution` share.

Getting here took three wrong claims in a row, all made before anything was
measured. That the extraction bug was changing verdicts — asserted from one DECK
pair. That the flip was model noise — asserted from a reading of the same name at
a different as-of, which is not a controlled pair either. That a 25-event re-run
showing 80% replicated the 82% — that run had silently dropped `--years 7
--tolerance 1` and screened a different universe (25 events against 49, sharing
15; +2.13% CAGR against +11.98%, which is the flag and not store drift). The
measurement says the first claim was right and both corrections to it were wrong.

**The ordering argument, restated honestly.** It was written as two independent
legs: `verdict` is uninformative, *and* separately the promoted fields are worth
reading. The verdict distribution did move, so the legs are not as independent as
claimed. What survives is narrower and still sufficient: `caution` covers 70% of
picks under the fixed extractor, and the flags are specific and checkable — ResMed's
16.5% effective rate resting on a ~$30M IRS refund and a ~$21.4M cessation benefit,
Applied Materials selling $501M of receivables without recourse against $719M of
equity gains below the operating line, Adobe collapsing three segments into one from
Q1 FY2026 and removing the disclosure that showed Digital Experience +9% against
Publishing -7%. Those lead because they say something; `verdict` renders last
because it mostly does not.

The design point that survives intact: this was found because the briefing order
changed. Under the old order the line that mattered was buried below a `caution`
that nobody would have questioned.

Which is also the uncomfortable part. Two data defects in this module — the
extraction bug and the dropped `--years` flag — were both caught by a person
happening to read one line, not by anything that checks. The flag case now has
`setting_label` printing the population settings on every run and in `--dry-run`.
The extraction case has nothing equivalent: the analyst noticing "this reads as
Item 1" in `evidence_gaps` is the whole detection mechanism, and it only worked
because the field had just been promoted. A cheap standing check on what
`sections_for` returns — that a span opens at a heading and is within an order of
magnitude of its neighbours — is not built and should be.
