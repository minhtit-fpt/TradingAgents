# Phase 6 — does tier 3 earn its cost?

Deliverable: `tradingagents/value/backtest/llm_sample.py`, plan §12 phase 6.
Tests: `tests/value/test_llm_sample.py`.

## What is being asked, and against what

Not "does the value module beat SPY" — phase 4b answered that, and the answer was
no. Phase 6 is a **paired** question: over the same dates, the same point-in-time
universe and the same construction, does vetoing names on a read of Items 1, 1A
and 7 beat *not* vetoing them? The unfiltered book stands where SPY stands in the
phase-4 gate. What has to be beaten is zero.

## Pre-registered pass criterion

Fixed here before any run, and reproduced verbatim in `llm_sample.CRITERION` so
the report cannot cite a criterion edited later:

> Tier 3 earns its cost only if the bootstrap CI for the filtered book's CAGR
> against the unfiltered book is entirely above zero **and** the point estimate
> clears the measured noise floor — the effect a skill-free random veto of the
> same size produces on the same dates. Both at the configured settings.

The second half is the phase-4b lesson made mechanical. Three of that phase's
verdicts were overturned by defects that moved the number by more than the effect
being measured; here the apparatus is asked to state its own resolution first.

## Method

1. Replay the screen (`numeric.snapshots`) and build the unfiltered schedule with
   the same `numeric.construct` every phase-4b run used. One setting, no grid.
2. **Events are `(ticker, fiscal year)`**, not `(ticker, date)`. A 10-K covers a
   year, so all four quarterly dates inside one year read the same filing and
   produce a byte-identical prompt — one paid call, and `--sample N` counts calls
   rather than dates that collapse onto each other.
3. For each sampled event, fetch the most recent 10-K **filed on or before** the
   first date the name was held, extract Items 1/1A/7, and hand the analyst the
   screen's own verdict at that date as the numeric summary. Nothing in the
   prompt postdates the decision.
4. Veto rule (`--veto avoid` by default) drops the event; the drop applies to
   **every date in that year**, because the verdict is about a filing and the
   filing does not change between quarters.
5. Simulate both books, bootstrap the paired excess, and measure the floor:
   `--reps` random vetoes of the same size, median CI upper bound. $0.
6. Report cost, coverage, veto rate and book cut on the face of the verdict.

## What phase 4b step 6 already established, and what it means for the run

The probe run before any analyst code was written found:

- Under `--exit quality` a 25% veto cuts ~5% of the book — incumbents carry
  forward, so an entry filter has no leverage. **Run `--exit rebalance`** (the
  default here). The report prints a `LEVERAGE:` caveat if you do not.
- At 4- and 8-period oracle horizons even a *perfect* veto sits below the noise
  floor. The only headroom found was at a one-quarter horizon, which is a price
  predictor, not what reading a 10-K is for.

So the expected outcome is a fail, and the module is built to say that cleanly
rather than to find something. A pass here would be surprising and should be
re-examined before being believed.

## Cost

DeepSeek pricing verified 2026-08-19 (plan open item #2, now closed).
`MODEL_PRICES_USD_PER_MTOK` carries the **peak** rates — $1.32/$3.96 per Mtok for
`deepseek-v4-pro`, with off-peak at exactly half — because charging the cheaper
tier would let a peak-hours run spend twice its cap before the cap noticed.

Measured projection: **~$0.052 per event**. `--dry-run` counts the events and
prints the worst case before anything is spent; `--budget` fails closed, and the
disk cache means a run resumed after a breach does not re-buy what it already
answered.

## How to run

```bash
# free: how many events, and what they would cost
python -m tradingagents.value.backtest.llm_sample \
    --start 2014-01-01 --end 2026-06-30 --years 7 --tolerance 1 --dry-run

# the run — same window and settings as every phase-4b gate run
VALUE_SEC_USER_AGENT='TradingAgents research you@example.com' \
python -m tradingagents.value.backtest.llm_sample \
    --start 2014-01-01 --end 2026-06-30 --years 7 --tolerance 1 \
    --exit rebalance --sample 60 --budget 5
```

## Result

Run 2014-01-01 → 2026-06-30, `years=7`, `tolerance=1`, `--exit rebalance`,
`--select trigger` at the configured 30% trigger — the same window and settings
every phase-4b gate run used. 49 events held, 49 sampled (100%), 44 assessed,
5 failed. **$1.91 of DeepSeek tokens, once**; the second row re-used the cache
and cost nothing.

| veto rule | vetoed | book cut | filtered CAGR | vs unfiltered | 95% CI | noise floor | max DD | verdict |
|---|---|---|---|---|---|---|---|---|
| `avoid` (configured) | 0 of 44 | 0.0% | +11.98% | +0.00% | [+0.00%, +0.00%] | not measurable | 48.6% | **fail** |
| `avoid+caution` | 36 of 44 | 73.2% | +10.00% | −1.98% | [−12.37%, +7.21%] | +5.16% | 31.4% | **fail** |

Unfiltered book: +11.98% CAGR, 48.6% max drawdown, 24 closed trades.

**VERDICT: tier 3 does not earn its cost.** Neither row clears the
pre-registered criterion, and they fail for two different reasons that are worth
keeping apart.

### The `avoid` row fails by construction, not by measurement

Over 44 filings the analyst returned **zero `avoid` verdicts** — 36 `caution`,
8 `proceed`. The default veto had nothing to veto, so the two books are the same
book and the effect is exactly zero.

That is not a broken analyst. It is what a veto layered on a screen that has
already passed a name looks like: the events are ADBE, PG, TXN, LRCX, RMD, FAST,
BIIB — businesses the numeric criteria selected precisely because ten years of
statements look excellent. Asked "does the filing contradict the numeric case",
the honest answer for a name like that is rarely "avoid". The schema's own
definition makes this explicit: `caution` is a real concern *that does not sink
the thesis*, and that is the verdict the analyst reached for 82% of the time.
(Read through a broken section extractor. The controlled re-run below puts it
at 70% — 31 of the same 44 — with the `avoid` column still empty.)

**So the binding question is not whether tier 3 reads well. It reads well** —
the flags it raised are specific and checkable (Gillette's $12.8B indefinite-lived
intangible with <10% headroom and a prior $1.3B impairment; TXN inventory at 219
days against a 12.5% revenue decline; RMD SaaS growth that is 8% ex-acquisition).
The question is whether that reading maps onto a *tradeable* three-state verdict,
and on this screen's own picks it collapses to one state.

### The `avoid+caution` row fails on the floor, exactly as step 6 predicted

Widening the rule gives the veto leverage — 73.2% of held slots — and the effect
is −1.98% with a CI of ±10 points, against a random-veto floor whose upper bound
is +5.16%. A skill-free veto of the same size does as well. Phase 4b step 6 named
this cell in advance: **a veto above ~50% is self-defeating**, because the cutting
itself becomes the dominant noise source. The measurement agrees, and the
criterion refuses it on both halves.

*One thing worth noting that the criterion does not read.* Drawdown falls from
48.6% to 31.4% under the wide veto. Phase 4b step 5 showed the position cap
already buys that (18.9%) for free, so this is not new capability — but it is the
same direction: the interventions that help this strategy help by holding less,
not by picking better.

### The defect this run found in its own apparatus

Five events could not be assessed, all the same cause: `edgar.filings.fetch_10k`
reads only EDGAR's `recent` submissions page, which does not reach back far enough
for a heavy filer. KO at 2014, INTU at 2014-2015 and FFIV at 2015-2016 returned
`FilingNotFound` rather than a wrong filing — the failure is loud and the names
stayed in the book, which biases the effect toward zero and never away from it.
Closing it needs the paginated `files` list from the submissions payload. It is
recorded rather than fixed, because at 5 of 49 it cannot change either verdict:
the `avoid` row has no effect to move, and the `avoid+caution` row misses the
floor by 7 points.

### What this closes

Plan open item #5 — "whether tier 3 justifies its cost" — is answered: **no**, on
this screen, at this horizon, with this apparatus. Consistent with phase 4b step
6, which predicted it before the analyst existed, and now measured with the
analyst actually reading the filings rather than with an oracle standing in for it.

The one thing this run does *not* say is that reading 10-Ks is worthless. It says
a three-state verdict applied at entry, over picks a quality screen already made,
on a book of 7 names a quarter, cannot be separated from noise. A different use
of the same reading — sizing, or an exit signal, or a screen that admits weaker
names for tier 3 to actually reject — is untested, and phase 4b's stop condition
applies before any of them is worth funding.

---

## Qualification added 2026-08-19 (found in phase 8)

**The 44-filing sample was read through a broken section extractor.**
`edgar/filings.py` could not tell a heading from a filing's citation of its own
items, so `extract()` routinely returned the wrong section. Measured across six
real 10-Ks after the fix:

| filing | what `mdna` actually contained before | after |
|---|---|---|
| KO | 3,199 characters, and `risk_factors` held 96k of MD&A text | 109,639 characters of MD&A |
| PG | 49k opening mid-sentence in a citation | 83,734, opening at the heading |
| DECK | 22k that was the tail of Item 1 | 49,171 |
| RMD | 14,653 | 49,910 |
| ADBE | 13,785 | 46,636 |
| MSFT | 22,369 | 51,249 |

Five of six were wrong. `backtest/llm_sample.py` calls the same
`filings.sections_for`, so the analyst in this phase was frequently asked to
assess Item 1 boilerplate as though it were MD&A.

**Re-measured on 2026-08-20 against the same 44 events.** `--years 7
--tolerance 1`, fixed extractor, $1.64. This is a controlled pair: identical
population, identical as-of dates, the same 5 `fetch_10k` failures, the same
unfiltered book (+11.98% CAGR / 48.6% max DD / 24 closed trades). The only
difference is which text the analyst was shown.

| | `caution` | `proceed` | `avoid` |
|---|---|---|---|
| broken extractor (original) | 36 | 8 | 0 |
| fixed extractor | **31** | **13** | 0 |

**The 82% does not survive the fix. The figure is 70% (31 of 44).** A net five
events moved `caution` -> `proceed`; the original run's per-event verdicts were
not recorded, so five is the net and the per-event churn could be larger.

**The pre-registered verdict is unchanged, and for the same reason as before.**
Zero `avoid` again, so the default veto still has nothing to veto, still cuts 0%
of held slots, and the CI is still exactly [+0.00%, +0.00%]. Tier 3 does not earn
its cost as a gate. That conclusion never depended on the exact `caution` share —
it depends on the `avoid` column being empty, which it is under both extractors.

**Three wrong claims were made about this before it was measured, and the order
matters.** First, that the extraction bug was changing verdicts — asserted from a
single DECK pair, which did not support it. Second, that the flip was model noise
— asserted from a different-as-of reading of the same name, which is not a
controlled pair either. Third, that 80% on a 25-event run replicated the 82% —
that run had silently dropped `--years 7 --tolerance 1` and was a different
population. The measurement above says the first claim was right and the two
corrections after it were wrong. None of the three was worth its confidence; only
this run is evidence.

**What the fix changed most is the briefing.** The flags came back specific and
checkable in a way the original sample's did not: ResMed's 16.5% effective rate
resting on a ~$30M IRS interest/penalty refund and a ~$21.4M cessation benefit;
Applied Materials selling $501M of receivables without recourse while $719M of
equity-investment gains sat below operating income; Adobe collapsing three
reportable segments into one from Q1 FY2026, removing the disclosure that showed
Digital Experience +9% against Publishing -7%, with FY2025 net income up 28% only
against the prior year's $1B Figma termination fee.

That is what phase 8's field ordering rests on, and it rests on it alone. The
earlier framing — "`verdict` is uninformative *and*, separately, the promoted
fields are worth reading" — overstated the independence, because the verdict
distribution did move (36/8 to 31/13). The ordering argument that survives is the
narrow one: the flags are specific and checkable, and `verdict` collapses onto one
label 70% of the time under either extractor. Both halves are still true; they are
just not as unrelated as claimed.

**A second, separate error: the 25-event run.** Before the controlled re-run
above, a re-measure was attempted without `--years 7 --tolerance 1` and therefore
ran the defaults, 10 and 2 — a different candidate universe. It held 25 events
against 49 (sharing only 15) and its unfiltered book came out at +2.13% CAGR /
39.7% max DD / 11 closed trades. Nothing was wrong with the store: verified on
2026-08-20, `--years 7 --tolerance 1` still reproduces 49 events exactly. An
earlier draft blamed store drift, and that was wrong.

What that episode exposes is that the report gave no way to notice. `--years` and
`--tolerance` decide which names are candidates at all, and neither appeared
anywhere in the output — two runs over different halves of the universe printed
headers that read alike. `llm_sample.setting_label` now puts both in the header
and in the `--dry-run` count, with `SettingLabelTest` pinning it.

**The standing problem behind all of this.** Both data defects in this module —
the extraction bug and the dropped flag — were caught because a person happened to
read one line. The flag case is now instrumented. The extraction case is not: the
only thing that noticed was the analyst writing "this reads as Item 1 rather than
MD&A" into `evidence_gaps`, and that only surfaced because phase 8 had just
promoted that field. A cheap standing assertion on `sections_for` — that a span
opens at a heading, and that its length is within an order of magnitude of its
siblings — would have caught it on any run. It is not built.

## The extractor, third round — 2026-08-26

The standing problem above closes one notch further, and the instrumentation the
last paragraph asked for is what closed it.

`_geometry_faults` was built after phase 6 and fires on `sections_for`. On
2026-08-26 the dividend module's first LLM briefing (D7) read three filings and
**all three came back flagged**: LMT with no Item 7 at all, HD with MD&A text in
the `risk_factors` span, ITW with MD&A opening late and closing early. Nobody had
to read one line to notice — the check said so on the run, which is exactly what
the previous entry said was missing.

### A third citation form the punctuation rules cannot see

Phase 6's fix separated headings from citations by punctuation: a comma after the
number (DECK) or an opening quote before it (KO). Neither fires on a citation
written as plain prose, because those are punctuated exactly like a heading:

| filing | text | after the number |
|---|---|---|
| LMT | "...notes thereto included **in** Item 8 - Financial Statements" | dash |
| LMT | "For additional information..., **see** Item 1A - Risk Factors" | dash |
| HD | "...related notes **and Part II,** Item 7. Management's Discussion" | full stop |
| ITW | "**Refer to** Item 7. Management's Discussion and Analysis" | full stop |
| JNJ | "...under the **captions** Item 1. Election of Directors" | full stop |

What separates them sits *before* the number. A heading follows the end of
something — a full stop, a page number, "Table of Contents". A prose citation
follows the word that governs it. So `_is_citation` gained `_XREF_BEFORE_RE`, a
bounded 48-character lookback matching a governing preposition, verb or noun,
optionally with a `Part II,` wedged between it and the number.

Matched by cue word rather than by enumerating what a heading looks like, because
the default must stay "this opens a section": an opener wrongly dropped truncates
the section before it, which is the more expensive of the two errors and the one
phase 6's own comment warns about.

### Measured over 18 filings, old extractor against new

Same 18 cached 10-Ks, both code paths, faults counted as `missing + suspect`:

```
filings with any fault:   old 12/18   new 0/18
```

Zero regressions — no filing that was clean became faulty. Three had lost a
section outright and got it back (LMT, MMM, PEP: `mdna` 0 characters -> 48,000).
Six more had MD&A truncated to between 9,813 and 44,963 characters and now reach
the budget. HD's `mdna` got *shorter*, 37,251 -> 31,356, because the old span was
holding text that was never MD&A.

Seven new tests in `test_filings.py`, each carrying the verbatim sentence from
the filing it came from.

### What this does not change

- **The phase 6 verdict stands.** Tier 3 does not earn its cost as a gate; that
  rested on the `avoid` column being empty, not on which text was read. This
  round was not re-measured against the 44 events, and no claim here needs it to
  have been.
- **The 70% `caution` share is now unverified in either direction.** It was
  measured on the previous extractor. Whether better text spreads the verdicts is
  an open question and a paid one; `alerts.message.briefing` still prints the 70%
  because that is the last figure actually measured.
- **Nothing proves the forms are exhausted.** Four are handled because four were
  observed. A fifth filer will write a fifth form, and `_geometry_faults` is
  still the thing that will say so.
