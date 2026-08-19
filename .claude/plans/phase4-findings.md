# Phase 4 — what the first backtest actually found

Status of the phase-3 gate: **not passed, and not yet failed either.** The
tier-1 backtest printed `no name reached a valuation on any rebalance date`.
That is not a verdict on the strategy — three defects upstream of the strategy
made the run meaningless. Two are fixed; the rest are listed here with the
evidence, so the gate is re-run against clean data rather than argued about.

Evidence throughout is from a probe that bypasses the criteria and values every
name with a usable EPS history (104 names at 2024-12-31, 110 at 2019-12-31).

---

## Fixed

### 1. Share counts filed in millions reached the store

A handful of filers tag `WeightedAverageNumberOfDilutedSharesOutstanding` in
millions while declaring the unit as `shares`. Bruker filed `156.6` for
156,600,000. The error is invisible downstream — it only surfaces as an EPS a
million times too large, which then reads as an extraordinary compounder.

- **Scale:** 301 ticker-years across 53 tickers, including EG (30), GRMN (24),
  WRB (18), RGEN (18), NVDA (6).
- **Fix:** `Concept.minimum` in `edgar/concepts.py`. A row below the floor is
  dropped, never rescaled — inferring the intended scale is a guess, and a
  guessed denominator is exactly the confidently-wrong number the module exists
  to avoid. The ticker loses coverage and gets excluded, which is the honest
  outcome.
- **Existing store:** the 301 rows were deleted in place. A fresh ingest never
  writes them again.

### 2. Prices and share counts were on different split bases

Two separate faults with the same symptom, a P/E off by the split factor.

**a. yfinance back-applies splits that happen after the as-of date.** Netflix's
2024-12-31 close reads 89.13 today because of a 10-for-1 split in November
2025; the real close was 891.32. `auto_adjust=False` suppresses the *dividend*
adjustment only. This is also look-ahead: knowledge of a 2025 split leaking
into a 2024 valuation.

**b. A ten-year history is assembled from several 10-Ks, on several bases.**
`facts_as_of` takes the latest filing *per fiscal year*, and EDGAR restates
share counts forward only in filings that carry the year as a comparative. So
the series steps:

```
NVDA  FY2022 24,940,000,000 (filed 2026-02-25)   FY2021  2,535,000,000 (filed 2024-02-21)
LRCX  FY2023  1,358,336,000 (filed 2025-08-11)   FY2022    140,628,000 (filed 2024-08-29)
AVGO  FY2022  4,232,000,000 (filed 2024-12-20)   FY2021    429,000,000 (filed 2023-12-14)
WMT   FY2021  8,415,000,000 (filed 2024-03-15)   FY2020  2,847,000,000 (filed 2023-03-17)
```

Read as one series, EPS collapses 90% in a year that was in fact flat, and the
growth fit — the single largest input to intrinsic value — is fitted through
the step. **Every company that split in the last decade was mis-valued**, which
is a large share of exactly the quality names this screen is built to find.

**Fix.** Every price frame now carries two columns: `Close` (today's basis,
continuous through splits, what the portfolio simulator needs) and
`AsTradedClose` (each day's own basis). Their ratio at a date is the product of
every split since, which is the only thing needed to put both halves of a P/E
on one basis:

- `market.split_basis_factors(ticker, filed, as_of)` — per fiscal year, what to
  multiply its filed share count by, keyed on the *filing* date because that is
  the basis the count was written on.
- `intrinsic.on_current_share_basis(financials, factors)` — returns a rebased
  copy. A year with no factor loses its share count rather than joining on a
  guessed basis.
- `market.year_ends` puts the P/E-median prices on the as-of basis too.
- `db.filed_as_of` supplies the filing dates.

Before and after, at 2024-12-31:

| | EPS as filed | EPS rebased | P/E before | P/E after |
|---|---|---|---|---|
| LRCX | 29.00 | 2.90 | 2.5 | 24.9 |
| NVDA | 11.93 | 1.19 | 11.3 | 112.5 |

Covered by `tests/value/test_market.py` and `tests/value/test_concepts.py`.

---

## Open — decisions, not defects

### 3. Nothing passes the criteria. This is what produced zero trades.

At 2024-12-31: screened 427, **passed 0**. The best name in the universe still
fails one criterion, and all thirteen must pass.

```
NetMargin                 411 / 427 fail
SGAToGrossProfit          397
CapexToNetIncome          378
DebtToEquity              351
ReturnOnEquity            337
LongTermDebtToNetIncome   330
```

No valuation fix changes this; the funnel dies before the valuation runs. The
distribution of failures per name (1 name fails one criterion, 5 fail two, 5
fail three, then a long tail out to twelve) says the gate is near-binary rather
than close to calibrated. Both open items in the plan live here — the
thresholds themselves and `VIOLATION_TOLERANCE`, currently 1 year in 10.

Untangling coverage from genuine failure came first, and it is now done — see
item 5. The short version: coverage was worth repairing, and it was not what
was killing the funnel.

### 4. Intrinsic value is systematically low, and three conservatisms cause it

Confirmed. The terminal multiple is the binding one — it caps 92 of 104 names.

```
2019-12-31, r = 1.92% floored to 4%      median MoS   names >= 30% MoS
base                                        -28.4%          23 / 110
+ dividends over the projection              -5.7%          32
- discount floor removed                     -4.9%          33
- terminal P/E cap removed                   +7.7%          40
all three                                   +34.9%          57
```

`min(median_pe, 15)` is one-sided: a median P/E of 30 is cut to 15, a median of
10 stays 10. Nothing is ever credited above the cap. On top of that the
projection ignores ten years of dividends outright (a documented simplification
worth ~16 points of MoS), and the discount floor charges 4% in a 1.9% world.

Removing all three is not the answer — it fires on 58% of the universe, which
is not a screen. The one that is a plain omission rather than a deliberate
margin is the dividend stream. Suggested order: add dividends, then re-measure
before touching the cap or the floor.

---

## Fixed, after the first backtest

### 5. The four thin concepts: one real tag gap, one structural non-fact

`RnD`, `SGA`, `D&A` and `GrossProfit` were the thinnest concepts in the store.
Probing live companyfacts payloads for the filers that resolved none of them
separates two different things that both looked like missing coverage.

**A real gap — SG&A.** `_TAG_SUMS` required both `GeneralAndAdministrativeExpense`
and `SellingAndMarketingExpense`, and refused a partial sum. But REITs, banks,
insurers and a good many industrials file a lone G&A line with no selling line
at all, so nothing resolved for them: BA, GD, HAL, SLB, FRT, EGP, UDR, SCI, MTZ.
The fill is now staged — pair-sum first, with the selling half matched against
four alternate tag names, and lone G&A only for a year where no selling tag
exists anywhere in the filing. The order is the whole safeguard: reversed, every
retailer would silently lose its selling costs and read as a disciplined
business.

Three smaller chain gaps went with it: `CostOfServices` and the
`…ExcludingDepreciationDepletionAndAmortization` cost variants (last in the
chain — a cost base without D&A is not the same footing as one with it),
`DepreciationAndAmortization`, and R&D's `…ExcludingAcquiredInProcessCost`.

**A non-fact — gross profit at a bank.** Of 40 probed filers with no
`GrossProfit`, only 2 gained one. The other 38 are banks, brokers and insurers
(SF, WTFC, BAC, USB, CME, IVZ): they have no cost-of-revenue concept in any
tag, because the line does not exist in their income statement. The same holds
for R&D — 19 of 20 probed filers report no R&D tag anywhere, which criterion 4
already treats as zero.

`CostsAndExpenses` and `OperatingExpenses` were rejected as stand-ins. They are
the whole expense base, not the cost of revenue; subtracting either would invent
a gross profit that no filing states. A test pins that refusal.

So gross-margin thinness is not an ingest defect to fix. It is financials
sitting in a universe screened on gross margin — an exclusion decision, and one
that belongs with the threshold work in item 3.

**Measured, at 2024-12-31, before and after a full 911-ticker re-ingest:**

| | before | after |
|---|---|---|
| SGA rows / companies | 21,757 / 606 | 27,870 / 761 |
| D&A rows / companies | 30,639 / 854 | 33,485 / 887 |
| GrossProfit rows / companies | 20,563 / 652 | 21,485 / 671 |
| RnD rows / companies | 14,336 / 434 | 15,496 / 450 |
| above the 80% coverage floor | 456 / 911 | 482 / 911 |
| screened | 427 | 450 |

Failures that were caused by a missing input rather than a violated threshold:

| criterion | missing-only, before | after |
|---|---|---|
| SGAToGrossProfit | 82 | 67 |
| DAToGrossProfit | 73 | 56 |
| GrossMargin | 75 | 73 |

**Passed: still 0.** That is the point of the exercise. NetMargin now fails 430
of 450 names and 430 of those are genuine violations, not holes — the funnel
dies on thresholds, and coverage can no longer be offered as the explanation.

---

## Re-run the gate in this order

1. ~~Repair coverage for the four thin concepts.~~ Done — item 5.
2. ~~Re-tune the criteria thresholds and `VIOLATION_TOLERANCE`.~~ Done — item 6.
   Still open: whether financials belong in a universe screened on gross margin.
3. Re-run the tier-1 backtest. Its numbers were never about the strategy — they
   were about the split bases. The screen now passes names and two of them clear
   the trigger at each test date, so the run has something to trade.
4. Item 4's dividend omission is still unaddressed, and it moves MoS by ~16
   points. Fix it before reading the backtest as a verdict on the thresholds.


### 6. The thresholds: a conjunction problem, not a mis-set level

The first instinct — find the one threshold that is wrong — does not survive
contact with the data. At the original levels no single criterion is binding:
of the names failing exactly two criteria at 2024-12-31, the pairs are spread
across `NetMargin`+`SGAToGrossProfit` (5), `NetMargin`+`RnDToGrossProfit` (2),
`DebtToEquity`+`SGAToGrossProfit` (2), `CapexToNetIncome`+`DAToGrossProfit` (2)
and a tail of singles. Moving any one level on its own buys one extra name at
most. Twelve conjunctive gates admitting 4–36% of the universe each multiply to
zero; that is arithmetic, not a discovery about American business.

**Instrument.** Rather than re-running the screen per candidate, the tuning
works off a critical-value matrix: for each company and criterion, the threshold
that would *just* admit it given the tolerance — the (t+1)-th worst yearly
ratio, after missing years have eaten into the budget. Pass rate at any
candidate vector is then a lookup. Built at two as-of dates whose ten-year
windows share only half their years.

**Choice.** The book states two tiers for most of these ratios: a durable
competitive advantage level and a wider band it still calls acceptable. Five
levels moved to the second tier; five stayed at the first, and the ones that
stayed are the ones the thesis is about.

| criterion | was | now | why |
|---|---|---|---|
| NetMargin | 0.20 | 0.12 | 0.20 admits 4% of the universe; 0.10 admits the same names as 0.12, so this is a plateau, not a knife-edge |
| SGAToGrossProfit | 0.30 | 0.80 | the book's own acceptable band; 0.30 admitted 9% |
| DAToGrossProfit | 0.10 | 0.15 | 0.10 admitted 31% |
| CapexToNetIncome | 0.25 | 0.50 | the book's acceptable band; 0.25 admitted 12% |
| InterestToOperatingIncome | 0.15 | 0.20 | 0.15 is the consumer-durables level, not a universal one |
| GrossMargin | 0.40 | **0.40** | pricing power is the thesis |
| DebtToEquity | 0.80 | **0.80** | so is the balance sheet |
| LongTermDebtToNetIncome | 4.0 | **4.0** | as above |
| ReturnOnEquity | 0.15 | **0.15** | as above |
| RnDToGrossProfit | 0.30 | **0.30** | already admitted 61% |
| `VIOLATION_TOLERANCE` | 1 | **2** | any ten-year window ending this decade contains 2020; at 1 the allowance is spent before the business has been asked anything |

**Result, and the reason to believe it is not fitted:**

| | 2024-12-31 | 2019-12-31 |
|---|---|---|
| screened | 450 | 351 |
| passed, before | 0 | 0 |
| passed, after | 9 (2.0%) | 7 (2.0%) |
| at or above the 30% MoS trigger | 2 | 2 |

Two windows sharing half their years land on the same 2.0%. The survivors are
recognisable: ADBE, CTAS, FAST, IEX, JKHY, LRCX, PG, RMD, TXN at 2024, and
BIIB, FAST, JKHY, MMM, RMD, SPGI, TXN at 2019.

**What this does not fix.** BIIB clears the 2019 trigger at +66.5% MoS on a
growth rate pinned at the 15% cap — Biogen in 2019, going into a patent cliff.
The Graham-number warning fires on it, which is that check doing its job, but
the screen has no view on why a decade of growth might stop. Extrapolation risk
is the strategy's standing weakness, not something a threshold can price.

Tolerance is now the most sensitive single knob: 3 roughly doubles the
survivors again and starts admitting names whose bad years run consecutively,
which is the pattern the criterion exists to catch. It was left at 2.

---

### 7. Dividends added, execution repaired, and the gate re-run

**Dividends.** The projection now values both halves of what a holder receives:
the discounted payout stream over the ten years, plus the terminal
capitalisation. Payout is the median year rather than the latest — one special
dividend is a board decision, not a decade of policy — and capped at all of
earnings, since a decade above 100% compounds cash the business cannot fund.
Both halves of the ratio are company totals, so it is immune to the split-basis
gaps that drop years out of the EPS history. A non-payer gets `None` and a zero
stream, not an error.

The movers are the income-heavy names, which is the omission being repaired
rather than a general inflation of value: PG -24.5% -> +7.6% MoS, FAST at 2019
+22.5% -> +39.7%, TXN +32.0% -> +44.4%. BIIB is unchanged at +66.5% — it pays
nothing.

**An execution defect found by re-running the gate.** The first ten-year run
reported *23 orders refused by the broker against 9 completed trades*. More
rebalances bounced than executed, so its +6.52% CAGR measured a broker that
could not fund the trades. Every refusal was `Margin`, and the sizes name the
cause — `MMM size=146 cash=23,023 value=86,826`, a $24.8k buy against $23.0k of
cash. `order_target_percent` sizes a buy off portfolio *value*; the broker funds
it from *cash*. The target list is ranked by margin of safety, so a name
**entering** the portfolio leads it, and its buy was submitted ahead of the
reductions in the existing holdings that release the cash for it. Orders fill at
the next open in submission order, so it was checked against cash that had not
arrived. Fixed by submitting every reduction before any increase.

Worth recording how it hid: the first version of the regression test passed by
accident, because the new name was listed last, which put the reductions first.
The bug only appears when the buy leads — which is exactly what the real ranking
does.

**The gate, on clean execution.**

| MoS trigger | CAGR | vs SPY | max drawdown | trades | hit rate |
|---|---|---|---|---|---|
| 0% | +4.04% | -8.95% | 47.6% | 14 | 71% |
| 10% | +4.96% | -8.04% | 44.9% | 11 | 64% |
| 20% | +4.47% | -8.52% | 42.8% | 10 | 70% |
| 30% (configured) | +4.18% | -8.81% | 39.7% | 9 | 67% |
| 40% | +5.46% | -7.53% | 56.5% | 9 | 78% |
| SPY buy-and-hold | +12.99% | — | **33.7%** | — | — |

**This fails the phase-4 gate, and the failure is not an artefact.** Three
candidate excuses were tested and all three are closed:

1. *Cash drag from the trigger.* Removing it entirely (0% row) makes the result
   slightly worse, not better. The screen was not sitting out the decade.
2. *Survivorship.* The run's own check reports 0 of 15 valued names lost to
   missing prices.
3. *Bad execution.* Repaired above; the corrected numbers are lower than the
   broken ones.

The damning line is the drawdown, not the return. This strategy's entire claim
is that a portfolio of low-debt, high-margin, high-ROE businesses protects
capital when the market falls. It drew down **47.6% against the index's 33.7%**
— worse on the one dimension it exists to win. Underperforming a
growth-led decade would be defensible; failing to protect is not.

The one legitimate objection left is the window. 2016-2026 contains 2020 and
2022 and no true credit event, and the case for this screen rests on
2008-09 — which is item 1 of the original phase-4 options, still unexecuted and
now the only remaining way to change the verdict. Note also that the universe is
current index membership, so it is already biased *in the strategy's favour*;
the honest number is likely worse than the one above.

The non-monotonic CAGR across the trigger grid (4.04, 4.96, 4.47, 4.18, 5.46)
and 9-14 closed trades in a decade both say the same thing: this test is
underpowered. That is a statement about the test, not a defence of the strategy.
An underpowered test that cannot find an edge, on a universe tilted in the
strategy's favour, with a drawdown worse than the index, is not a reason to
keep spending.

**Recommendation: do not proceed to the LLM tiers.** Either extend the history
to 2006 and re-run — the only evidence that could overturn this — or stop, which
is what the plan says phase 4 is for.
---

### 8. The gate verdict was measuring the data ceiling, not the strategy

Item 7 recommended stopping, on one remaining objection: the window contained no
credit event, and only 2008-09 could overturn it. Chasing that objection is what
exposed the verdict as unsound.

**2008-09 is unreachable, and not for a fixable reason.** The store's `filed`
dates run `2009-10-27 .. 2026-08-14` — XBRL began in 2009, so no filing exists
before then to be point-in-time visible. A ten-year window makes the earliest
honest as-of date roughly 2016:

```
screenable universe, years=10
2009/2011/2013   0        2015    6        2016   59        2018   84
2019           292        2024  450
exclusion reasons at 2011: 427 "no 10-K facts as of this date",
                           258 "only 4 of 10 fiscal years", 183 "only 5 of 10"
```

The 2006-2026 run confirms it costs nothing to know: 39 of 80 rebalance dates
screened zero names, and the trade count, hit rate and drawdown came back
*identical* to the 2016-2026 run (14/11/10/9/9 trades, 47.6/44.9/42.8/39.7/56.5%
drawdown). CAGR fell to +2.07% purely from spreading the same trades over twenty
years with ten of them in cash. Reaching 2008 would require dropping the
`filed <= as_of` filter, which is not a longer backtest but a look-ahead one.

**What the per-date detail shows instead.**

```
2016-03-31 .. 2016-12-31   passed=1  — MMM
2017-03-31 .. 2018-12-31   passed=1  — BIIB  (+61% .. +38% MoS)
2019-03-31                 passed=5
2022-09-30                 passed=9
```

The portfolio held **one name** for the first three years of the window, at 100%
of NAV, through Biogen's aducanumab collapse. The 47.6% drawdown that item 7
called the damning line is that single position. Items 6 and 7 each recorded half
of this — item 6 flagged BIIB as a growth rate pinned at the cap walking into a
patent cliff, item 7 flagged the drawdown — without connecting them. Nothing in
the 2016-2018 stretch was a test of whether a portfolio of quality businesses
protects capital, because there was no portfolio. The universe was 53-84 names,
crippled by the same XBRL ceiling.

**Control: the same strategy over 2019-2026, where the universe is complete.**

| MoS trigger | CAGR | vs SPY | max DD | trades | hit rate |
|---|---|---|---|---|---|
| 0% | +8.28% | -7.12% | **31.3%** | 14 | 79% |
| 20% | +8.92% | -6.48% | 36.3% | 10 | 80% |
| 30% (configured) | +5.87% | -9.53% | 39.7% | 8 | 75% |
| SPY | +15.40% | — | 34.1% | — | — |

Averages 410 screened and 8.0 passed per date. Two things fall out. The quality
screen **did** protect — 31.3% against the index's 34.1% — so item 7's central
claim is withdrawn. And the margin-of-safety overlay subtracts on both axes:
tightening it from 0% to 30% costs 2.4 points of CAGR and adds 8.4 points of
drawdown. That is item 4 surfacing as a return: an intrinsic value biased low by
a one-sided terminal cap ranks names by noise, so selecting on it is worse than
not selecting.

**Buying back the lost decade with a shorter history window.** The ten-year
requirement, not the data, is what empties 2014-2015:

```
screenable      2012  2013  2014  2015  2016
years=10           0     0     0     6    59
years=7            4    17   144   287   332
years=5          120   259   353   380   367
```

Seven years is still a durability window, and the change is uniform across every
date rather than fitted to any name — but it moves the violation budget, so the
level was re-checked against item 6's calibration before being used:

| years | tolerance | 2019-12-31 | 2024-12-31 |
|---|---|---|---|
| 10 | 2 | 7 / 351 = 2.0% | 9 / 450 = 2.0% |
| 7 | 1 | 7 / 387 = 1.8% | 10 / 483 = 2.1% |
| 7 | 2 | 12 / 387 = 3.1% | 18 / 483 = 3.7% |

`years=7, tolerance=1` reproduces the 2.0% item 6 settled on and stays stable
across two windows sharing half their years; `tolerance=2` at seven years is a
silent loosening (a 2/7 budget against 2/10) and is carried below only as a
sensitivity check.

**The gate, re-run on 2014-2026.** 48 rebalance dates, 338 screened and 7.2
passed per date. SPY: +11.59% CAGR, 34.1% max drawdown.

| MoS trigger | CAGR | vs SPY | max DD | trades | hit rate |
|---|---|---|---|---|---|
| 0% | +14.04% | +2.45% | 30.9% | 26 | 77% |
| 10% | +10.78% | -0.81% | 46.7% | 27 | 81% |
| 20% | +11.02% | -0.57% | 43.8% | 22 | 77% |
| 30% (configured) | +14.26% | **+2.67%** | 35.2% | 22 | 77% |
| 40% | +13.08% | +1.49% | 29.5% | 15 | 73% |

Sensitivity at `tolerance=2` (13.1 passed per date): +1.63% at 0%, +2.66% at 30%,
+3.11% at 40%, with drawdowns of 40.1 / 42.1 / 30.9%.

Against item 7's verdict, at the configured trigger: -8.81% vs SPY becomes
**+2.67%**, 47.6% drawdown against a 33.7% index becomes 35.2% against 34.1%, and
9 closed trades become 22.

**What this does not establish.**

- The grid is still not monotonic — 14.04, 10.78, 11.02, 14.26, 13.08 across
  adjacent trigger levels that hold nearly the same names. Worse, changing only
  the tolerance moves the 0% drawdown from 30.9% to 40.1%. A 2.5-point edge sits
  inside the noise the grid is itself displaying.
- **Survivorship is now the largest known bias and remains unquantified.** The
  universe is EDGAR's `company_tickers.json` order, which is size *at the time
  SEC generated the file* — that is, 2026. The run's own check (0 of 27 valued
  names missing prices) measures the wrong thing: it catches a name that was
  ingested and lost its prices, not a name that was never a candidate because it
  is no longer large today. Extending the window to 2014 makes this worse, not
  better.
- Degrees of freedom were spent to get here: three windows and two tolerance
  settings, with `years=7` chosen after `years=10` failed. The choice has a
  data-availability reason and was calibration-checked before use, but it was
  still made with knowledge of the earlier result.

**Revised recommendation.** Item 7's "do not proceed" is withdrawn — it rested on
a drawdown produced by a single position in a crippled universe. It is not
replaced by "proceed": a 2.5-point edge with a non-monotonic grid is not yet a
result. Quantify survivorship first, since it is the one remaining bias whose
direction is known and whose size is not, and it flatters every number above.
Only then is spending LLM tokens defensible.

---

### 9. Survivorship measured: the edge in item 8 was the universe, not the screen

Item 8 left one bias named but unmeasured, and it is the one that flatters every
number. This is that measurement.

**What the run's own check does not catch.** The report's survivorship line
counts names that were ingested and then lost their price series — 0 of 27, which
reads as reassurance. It cannot count a name that was never a candidate. The
universe comes from EDGAR's `company_tickers.json` in file order, which is size
*at the time SEC generated the file*: 2026. Every company that mattered in 2014
and has since been acquired, taken private, or shrunk out of the large-cap range
is absent from the screen's input, not from its output.

**Magnitude.** Point-in-time S&P 500 membership from a third-party dataset
(`fja05680/sp500`, 692 weekly snapshots spanning 2014-01-07 to 2025-12-22),
against the 911 tickers then in the store:

```
distinct S&P 500 members, 2014-2026     767
  present in the store                  478   (62%)
  never a candidate                     289   (38%)
      had left the index by 2026        242
      still in the index, outside the
      911-name slice                     47
```

**Two mechanisms, not one.** The obvious one is exit: acquisition (ATVI, ALXN,
ABMD, BCR, CAM), going private (WBA), demotion (PARA, JNPR, HES, K, CPB).
The second is quieter — **ticker identity drifts under a live company**. BK still
files and still trades; Bank of New York Mellon rebranded to `BNY` in 2025, so a
universe keyed on today's ticker string loses its entire history under the old
one. Recorded while checking this, and worth a separate look: `AEP` is absent
from both `company_tickers.json` and `company_tickers_exchange.json` as fetched
2026-08-17, though the company is an index member that files 10-Ks. The universe
source has holes in the present, not only in the past.

**How much is reachable.** Of the 289 missing, 119 still resolve to a CIK through
EDGAR's ticker file and 170 do not. The 170 are precisely the acquired and
delisted — the adversely selected half. Reaching them needs a historical
ticker-to-CIK map, which the ingest path does not have. So what follows is a
**lower bound on the bias, measured on its milder half.**

The 119 were ingested (49 fell below the 80% coverage floor); the store went from
911 to 1,020 tickers and is kept at 1,020. Backup at
`value.db.bak-presurvivorship`.

**The gate, re-run on the widened universe.** 2014-2026, `years=7`,
`tolerance=1`, 48 dates, 388 screened and 7.6 passed per date. SPY +11.59%,
34.1% max drawdown.

| MoS trigger | CAGR | vs SPY | max DD | trades | | item 8 CAGR | item 8 DD |
|---|---|---|---|---|---|---|---|
| 0% | +13.00% | +1.41% | 31.6% | 28 | | +14.04% | 30.9% |
| 10% | +9.81% | -1.77% | 53.5% | 29 | | +10.78% | 46.7% |
| 20% | +9.31% | -2.28% | 54.1% | 24 | | +11.02% | 43.8% |
| 30% (configured) | +12.36% | **+0.77%** | **47.8%** | 24 | | +14.26% | 35.2% |
| 40% | +10.65% | -0.94% | 34.6% | 17 | | +13.08% | 29.5% |

Every level degraded; none improved. At the configured trigger the edge falls
from +2.67% to +0.77% and the drawdown rises 12.6 points, back above the index.
Three of five levels now trail SPY outright. The one-sidedness is the tell — a
correction that only ever subtracts is a bias being removed, not noise being
added.

**Verdict.** Repairing 38% of the hole cost 1.9 points of CAGR at the configured
trigger. The unrepaired 62% is the worse-selected half. A +0.77% edge sitting
inside a grid that swings 13.00 / 9.81 / 9.31 / 12.36 / 10.65 across adjacent
trigger levels, with drawdowns from 31.6% to 54.1%, is not a measurement of
anything.

So the phase-4 gate is not passed. Three verdicts have now been wrong in
sequence, and it is worth being precise about why, because the pattern is the
finding:

1. *Item 7 — fails, does not protect capital.* Wrong: a single Biogen position
   held at 100% of NAV in a universe crippled to 53-84 names by the XBRL ceiling.
2. *Item 8 — passes, +2.67% over SPY.* Wrong: a universe selected on 2026 size.
3. *Item 9 — no measurable edge.* Rests on a lower-bound correction, an
   underpowered test, and 170 adversely-selected names still outside it.

Each verdict was overturned by a defect in the *test*, not by new evidence about
the strategy — and each defect flattered or damned it by more than the effect
being measured. That is the honest summary of phase 4: this apparatus cannot
resolve an edge of the size being looked for. The remaining ways to change that
are a point-in-time fundamentals vendor covering pre-2009 and delisted issuers
(breaking the EDGAR-only design and costing money), or accepting that the tier-1
numeric screen has been given a fair test and did not clear it.

Nothing here argues for spending LLM tokens on the higher tiers.
