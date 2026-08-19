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
