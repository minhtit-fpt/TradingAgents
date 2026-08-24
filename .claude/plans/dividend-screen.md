# Dividend feature — the screen (D1), the book (D2), the weekly job (D3)

Deliverable: `tradingagents/value/dividend/`. Tests: `tests/value/test_dividend.py`,
`tests/value/test_dividend_ledger.py`, `tests/value/test_dividend_weekly.py`.
Branch: `feat/value-dividend-screen`, cut from `origin/main` after phase 8 merged.

## Why this phase exists

The request was a dividend agent: build a portfolio of good cash-dividend
payers, refresh it weekly, and keep a ledger of what was invested, what changed,
what was bought and sold, what the portfolio yields, how it grew, and the P&L.

Most of that fits the module as it stands. One part of it does not, and the
mismatch is the reason this document exists rather than a one-line commit.

**What fits.** The store already holds point-in-time EDGAR facts, the criteria
framework already asks "sustained, over a decade" rather than "good this year",
`decisions.py` already logs what the operator did and why, and `jobs/daily.py`
already ships a screen-plus-heartbeat pattern that a weekly job can copy.

**What does not.** "The agent builds the portfolio and updates it weekly" is an
automated buy decision. Phase 4b found the numeric strategy does not beat SPY;
phase 6 found an LLM veto at entry cannot be separated from a random one. Both
are findings about *automation deciding when to buy*, and phase 7 answered them
by moving the decision to the operator. A dividend agent that writes positions
would re-open exactly what those phases closed — and would do it in the corner
of the module where the mistake is most expensive, because a dividend book is
held for years.

So the split is the same one phases 7 and 8 already drew:

- the screen proposes — candidates, and names whose payout has broken;
- the operator disposes, and `decisions record` is what makes it durable.

The operator loses nothing they asked for. Every number on the list — yield on
cost, income, growth, P&L — is computed from a ledger either way. The only
difference is whose finger is on the buy.

**Weekly is also the wrong cadence for changing anything.** Payout ratio moves
once a year, coverage four times. A weekly *re-screen* is free and worth having;
a weekly *rebalance* is churn wearing a schedule. The weekly job (D3) therefore
reports and alerts on breaks, and proposes nothing on a timer.

## Scope of D1

Quality only. No prices, no yield, no positions, no ledger.

That is not timidity, it is ordering. A ledger built against a screen that has
not yet been shown to name sensible companies is a schema with rows nobody
trusts — the same argument phase 7 used to defer the decision journal until
there were decisions to put in it.

## Requirements

1. Four criteria over a decade, each returning the years that violated it, in
   the shape the existing screen already uses.
2. Per-share dividends on **one** share basis, so a split does not read as a
   cut.
3. Point-in-time: a screen as of a date must not see a dividend whose ex-date
   had not arrived.
4. Free and replayable. No LLM, and no network at all once the cache is warm.
5. Independent in the import graph, not merely in the directory name: deleting
   the directory deletes the feature.
6. Names no action and writes no position, like every other surface here.

## Non-goals

- **Yield, and yield against the name's own history.** It needs prices, and a
  price divided by a figure from the dividend cache is a basis error (see
  Design). It belongs in D3, where a price is fetched anyway.
- **Positions, lots, P&L.** D2.
- **The weekly job.** D3.
- **Sector or peer comparison.** This module has never ranked a name against its
  industry, and a dividend screen is a poor place to start: the peer group of a
  utility paying 85% of earnings is other utilities paying 85% of earnings.

## Design

Five modules under `tradingagents/value/dividend/`, plus one test file. **No
existing file is edited** — the isolation contract's own standard, applied one
level down.

```
config.py     VALUE_DIVIDEND_* knobs
store.py      the `dividends` table: DDL + accessors
history.py    per-share history from yfinance, cached by ex-date
criteria.py   the four criteria, pure
runner.py     screen + render + CLI
```

### The four criteria

| # | Name | Source | Limit | Tolerance |
|---|---|---|---|---|
| 1 | `PaidEveryYear` | per-share history | any payment | **0, not configurable** |
| 2 | `DividendNeverCut` | per-share history | no year-on-year fall | 0 (`VALUE_DIVIDEND_CUT_TOLERANCE`) |
| 3 | `PayoutRatio` | EDGAR | `DividendsPaid / NetIncome` ≤ 0.60 | 2 |
| 4 | `FreeCashFlowCover` | EDGAR | `(OperatingCashFlow − Capex) / DividendsPaid` ≥ 1.0 | 2 |

Criterion 2's tolerance is zero and separate from `VIOLATION_TOLERANCE` on
purpose. A margin that dips for one year is noise; a dividend cut for one year
is the board telling you what it thinks, and tolerating one cut in ten years
defeats the screen. Flat is not a cut — a board that *holds* the dividend
through a bad year is doing the thing this screen looks for.

Criterion 3 scores a loss year as a violation rather than as missing data.
Paying a dividend out of a loss is precisely the behaviour the limit is drawn to
catch, and treating it as unevaluable would let it through.

Criterion 4 exists because 3 is not enough. Earnings are an opinion about
timing; the cheque is not. A payer that clears 3 and fails 4 is funding the
dividend from the balance sheet, and does so for exactly as long as the balance
sheet allows.

The two coverage criteria get their own tolerance knob rather than reading the
business screen's. Retuning what counts as a durable margin must not silently
retune what counts as a safe payout.

### Two calendars, deliberately

Criteria 1 and 2 read the per-share series by **calendar year of ex-date** —
that is the series a holder actually received. Criteria 3 and 4 read EDGAR by
**fiscal year**, because coverage is an accounting question and has to be asked
against the year the accounts were drawn for. Pairing a calendar-year dividend
with a June fiscal year would turn a two-quarter offset into a finding.

### Per-share dividends come from yfinance, not EDGAR

EDGAR's `DividendsPaid` is a total cash outflow. Turning it into a per-share
figure means dividing by a filed share count — and filed share counts straddle
splits on different bases, which is the trap `screen/market.py` documents at
length. yfinance publishes the dividend per share already back-adjusted
uniformly across the whole history, so a year-on-year ratio is exact with no
basis repair at all.

The price of that choice is one rule, recorded in the table's DDL: **never
divide a price from `market.close()` by a figure from the `dividends` table.**
One is on the as-traded basis, the other on today's. Their ratio is a split
factor, not a yield. This is why yield is deferred rather than dropped in as an
afterthought.

### The year in progress is excluded

On 2026-08-24 a quarterly payer is two payments into a four-payment year.
Comparing that against last year reads as a 50% cut on every such name alive, so
the window is the *N fully elapsed calendar years* ending before the as-of date.
Annualising the stub instead would mean inventing the two payments still to
come — the invented number this codebase refuses everywhere else.

A year inside the window with no cached payment is stored as `0.0`, not omitted.
The window is dense by construction; absence is the signal, and a dict that
silently dropped 2020 would score a payer that stopped in 2020 as clean.

### Independence, asserted rather than promised

The feature owns its knobs, its table and its criteria. `value/config.py` has no
dividend knob; `value/store/db.py` has no dividend table — the DDL runs from
`dividend/store.py` on connect, idempotently, against the same SQLite file
(the screen reads EDGAR facts on every run, so a second file would be a second
connection to keep in step for no gain).

Every import points outward, into exactly three places:

| Import | Why it is not duplicated |
|---|---|
| `value.config` (`_env_*` helpers) | They raise on a bad value instead of falling back. A second copy is a second place for that to drift. |
| `value.store.db` | Reads EDGAR facts. |
| `value.screen.criteria` | Reuses `CriterionResult` / `ScreenResult`. |
| `value.screen.market` | D2 only: today's price, and the split factors that move a recorded share count onto today's basis. |
| `value.alerts.telegram` | D3 only: one POST. The channel is the operator's, and a second bot for the same person is not independence, it is a second token to rotate. |

Three tests hold the line, by AST rather than by review:

1. nothing outside `dividend/` imports `dividend`;
2. `dividend/` reaches outward only through that allowlist;
3. `value/config.py` contains no `VALUE_DIVIDEND`, and `db.py` no `dividends`
   table — so "no existing file was edited" is checked, not asserted in a commit
   message.

Deleting the directory and its test file removes the feature completely, leaving
one orphaned SQLite table and nothing else.

## How to run

```bash
python -m tradingagents.value.dividend --tickers PG,KO,JNJ --as-of 2026-08-24
python -m tradingagents.value.dividend --all --offline
```

```bash
python -m tradingagents.value.dividend.ledger buy --ticker PG --shares 12 --price 148.20
python -m tradingagents.value.dividend.ledger dividend --ticker PG --amount 11.87
python -m tradingagents.value.dividend.book --refresh
python -m tradingagents.value.dividend.weekly --dry-run
```

`--offline` uses the cache and fetches nothing. `--all` screens every name with
10-K facts in the store. Names rank by criteria-clean share, passes first; a
rejection prints the criteria that failed and the years they failed in.

## Risk

The screen finds names whose *payout* has been durable. That is not the same as
a name worth owning, and it is emphatically not the same as one that is cheap —
criterion 3 caps the payout, so a screen of survivors will lean toward mature,
slow businesses with room to keep paying. Anyone reading the pass list as a buy
list has skipped both the business screen in `screen/` and the valuation in
`report.py`, either of which can reject a name this one passes.

The second risk is the one D2 will carry: a ledger makes the portfolio feel
managed, and a managed-feeling portfolio invites the weekly fiddling this design
argues against. The weekly job must stay a report.

## Not investment advice

This module produces research output. It executes nothing and recommends
nothing.

## D1 result

Built on `feat/value-dividend-screen`. `git status` shows only new files —
`tradingagents/value/dividend/`, `tests/value/test_dividend.py` and this
document — and no modified file, which is the design claim and also test 3.

- `pytest tests/value` — 376 passed (20 new), `ruff check .` clean.
- Live check against PG on 2026-08-24: 260 payments cached, and the window
  2015–2024 reads 2.633 → 3.962 per share, rising in every one of the ten years.
  Real quarterly data through the calendar-year aggregation, with the 2025 stub
  correctly excluded.
- The CLI refuses to score a name with no 10-K facts in the store — "no verdict"
  rather than a pass on two criteria out of four.

## D2 — the book

Deliverable: `dividend/ledger.py` and `dividend/book.py`. Tests:
`tests/value/test_dividend_ledger.py`.

This is the rest of the request: how much was put in, what the portfolio holds,
what was bought and sold, what it pays, how it grew, and the profit and loss.

### Two tables and no third

```sql
dividend_lots   -- buy | sell: ticker, date, shares, price, fees, note
dividend_cash   -- deposit | withdraw | dividend: date, amount, ticker, note
```

Both append-only. No edit, no delete — a mistyped trade is corrected by
recording its reverse, which leaves both rows visible. Positions, cost basis and
profit are **derived on every run** rather than stored: a stored balance is a
number that can disagree with the events behind it, and the disagreement is
always found later than it happened. `decisions` already works this way.

The fold is also the validator. Recording a trade replays the whole name and
refuses the row if the book would stop making sense — selling 500 shares of a
100-share position raises before anything is written, and so does a back-dated
sale that invalidates a later one. A negative position would otherwise report a
profit that never existed.

### Cost basis: weighted average

Confirmed with the operator, over FIFO and specific-lot. One basis per name,
updated on every purchase; realised profit on a partial sale is proceeds minus
the average cost of the shares sold, and cannot be steered by choosing a lot.
Simplest thing that is correct for a book meant to be held for years, and the
one method that needs no extra argument at sale time — where a mistake would be
the operator's to make and the ledger's to keep forever.

The cost of the choice: these numbers will not tie out to a US broker's 1099,
which defaults to FIFO. Recorded here so the surprise happens now rather than in
April.

### Splits, and why the book has no `--as-of`

Share counts are recorded as traded. A 3-for-1 since purchase leaves the ledger
holding a third of what the broker shows, and multiplying that by today's price
understates the position by two thirds — silently. So every trade is moved onto
the current share basis before folding, reusing
`screen.market.split_basis_factors`, which exists for exactly this and is
already load-bearing in the valuation.

That is also why the book is valued **as of today only**. A historical book
needs price, split basis and dividend basis all moved to the same past date, and
two of those come from a source that back-adjusts to the present. An `--as-of`
that is subtly wrong before a split is worse than no `--as-of`.

`--offline` skips the adjustment *and every figure that depends on it*, rather
than printing an unadjusted one.

### Two dividend numbers, never merged

`recorded` is cash the operator told the ledger about. `expected gross` is
shares held on each ex-date times the dividend per share. The gap between them
is real information — a 30% treaty withholding looks exactly like it — and
averaging them into one "income" figure would destroy it. Neither is adjusted
into the other anywhere.

Forward income is last full year's dividend per share times shares held, and is
labelled as a rate rather than a forecast. A name with **no cached history**
reports unknown, not zero: a zero on the yield line that actually means "no data"
is precisely the confidently wrong number this codebase refuses. `--refresh`
fetches what is missing.

### Return

Total P&L is equity minus what was paid in, and the report also prints its three
components — realised, unrealised, dividends — which sum to it exactly.

The annualised figure is money-weighted (IRR over the operator's own flows),
because money goes in over time and a simple percentage would be meaningless.
**A dividend is not an external flow**: it stays inside the book, and counting it
as a deposit would make every payer look like fresh capital and drag the return
toward zero.

### What D2 deliberately does not do

- No time-weighted return. It needs the portfolio valued at every flow date,
  which needs price history per name; money-weighted answers "how did *my* money
  do", which is the question actually asked.
- No positions written by anything but the operator. Nothing in `alerts/`,
  `runner.py` or the coming weekly job may insert a lot.
- No reconciliation of expected against recorded dividends. Ex-date is not pay
  date and withholding is jurisdictional; a matcher would be guessing.

## D2 result

`pytest tests/value` — 403 passed (27 new), `ruff check .` clean. End-to-end
against live prices on 2026-08-24, with a book of 30 PG at 160.00 (10 later sold
at 172.00) and 100 KO at 62.50 against a 10,000 deposit:

- realised 119.67 + unrealised 2,552.93 + dividends 22.15 = **total P&L
  2,694.75**, which is also equity 12,694.75 minus the 10,000 paid in. The
  identity holds by arithmetic, and a test asserts it rather than trusting it.
- income 287.56/yr, yield on cost 3.26% (KO) and 2.61% (PG).
- expected gross to date 510.61 against 22.15 recorded — the gap being dividends
  the operator has not entered, which is exactly what the two-number design is
  for.
- money-weighted 15.76%/yr.
- Selling 500 shares of a 100-share position: `not recorded: KO 2026-08-24:
  selling 500 shares but only 100 held`.

## D3 — the weekly job

Deliverable: `dividend/weekly.py`. Tests: `tests/value/test_dividend_weekly.py`.

`jobs/daily.py` in shape, with two differences that matter.

**Weekly, and a report rather than a rebalance.** Payout ratio moves once a year
and coverage four times. A book that re-sorts itself every Monday is churn
wearing a schedule; what actually changes between Mondays is the price, and this
job deliberately does not act on it. It writes no position and names no action.

**Priority order is reading order.** Breaks first — a name you hold whose screen
no longer passes — then candidates you do not hold, then the book. A message is
read from the top, so the thing that changed goes there.

### Saying it once, without ever losing it

The dedupe key is the **signature** — the set of criteria that failed — not the
date. A payout that broke in March is still broken in April, and repeating it
every Monday trains the operator to stop reading. A *second* criterion breaking
produces a new signature and speaks again, because that is the change worth
interrupting for.

The failure mode a dedupe table must never have is silencing an alert it never
delivered. So `announced()` tests `sent_at`, not the row's existence: a claimed
row whose send failed is retried next week. Rows are claimed before the send and
confirmed after it, and a `--dry-run` reads the table and writes nothing — a
rehearsal that claimed rows would silence the real run behind it.

A consequence worth knowing rather than debugging: with no `VALUE_TELEGRAM_*`
configured, `telegram.send` prints and returns False, nothing is ever confirmed,
and every run repeats itself. That is the design working, not a bug.

### Cost control

The candidate screen runs **offline**, over names whose dividend history is
already cached. Each run refreshes every held name plus a bounded slice of the
stalest cached ones (20 by default), so the universe fills in over weeks. A
weekly job that fetched a thousand tickers is a weekly job that gets banned.

A dead price feed degrades to a note in the message rather than an exception: the
breaks are the part worth waking up for and they need no price at all.

## D3 result

`pytest tests/value` — 420 passed, `ruff check .` clean. Run against a copy of
the live store (1,066 names with facts) on 2026-08-24, holding 40 MMM and 25 ITW
against a 20,000 deposit:

```
BROKEN (1 held)
  MMM NEW: DividendNeverCut, PayoutRatio (clean 82%)
CANDIDATES (1 pass, not held)
  WMT NEW: clean 95%
BOOK  value 14,215.65
  income 272.30/yr
  P&L +3,816.65 on 20,000.00 invested
  money-weighted +11.90%/yr
```

MMM is the real check: 3M cut its dividend in 2024 after the Solventum spinoff,
and the criterion found it in the per-share history without being told to look.

### A defect the live run exposed

ADP failed `FreeCashFlowCover` in all ten years — reading as a company that
cannot fund its dividend. It is not: EDGAR resolves no capex line for ADP at
all, so every year was *unevaluable*, and `runner.render` was printing violation
years and missing years under one label.

Fixed in D1's render, which now distinguishes them:

```
MMM  FreeCashFlowCover: failed 2019, 2020
ADP  FreeCashFlowCover: no data 2017, 2018, …
```

One is a finding about the business and the other about our data, and they call
for opposite responses. Same class of bug as the income line that read `0.00/yr`
when the dividend cache was empty — a number that means "unknown" must never
render as one that means "zero". The concept gap itself is in
`value/edgar/concepts.py`, outside this feature and left alone.

## Still open

- **`VALUE_DIVIDEND_PAYOUT_MAX` at 0.60 may be too tight.** In the live run it is
  the criterion that rejects most names — PG, PEP, ABT, TGT, SYY, GPC, MCD all
  fail on it and nothing else. That is defensible for a Buffett-style screen
  (retained earnings compound; distributed ones do not) but a screen built to
  find income may want 0.70. It is a knob, and nothing has measured it yet.
- **No backtest.** D1 through D3 have never been replayed over history the way
  phases 3 and 4b replayed the business screen. Until that exists, the criteria
  levels are reasoned, not evidenced — and the module's own history says that is
  the distinction that matters.
