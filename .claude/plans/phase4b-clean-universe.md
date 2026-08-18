# Phase 4b — fix the test before touching the strategy

Phase 4's honest summary is that three gate verdicts in sequence were each
overturned by a defect in the **test**, not by evidence about the strategy:

| | verdict | vs SPY @30% MoS | max DD vs index | overturned by |
|---|---|---|---|---|
| item 7 | fails, does not protect capital | −8.81% | 47.6 / 33.7 | one Biogen position at 100% NAV; universe crippled to 53–84 names |
| item 8 | passes, +2.67% over SPY | +2.67% | 35.2 / 34.1 | universe selected on **2026** size |
| item 9 | no measurable edge | +0.77% | 47.8 / 34.1 | rests on a lower-bound survivorship repair; 170 adversely-selected names still outside it |

Each defect moved the result by more than the effect being measured. So phase 4b
spends nothing on the strategy until the apparatus can resolve an effect of the
size being looked for. **Universe first, then power, then one strategy change
per run.** LLM tiers (phases 5–6) stay off throughout, regardless of outcome.

---

## Steps, in this order

### 1. Point-in-time index membership as the backtest universe  *(measurement)*

Today `backtest/numeric.py` applies one static ticker list — `db.tickers()`, i.e.
EDGAR `company_tickers.json` in 2026 size order — to every rebalance date. That
is the defect behind items 8 and 9. Replace it with membership **as of each
date**, so survivorship is zero by construction rather than patched name by name.

Source: `fja05680/sp500` point-in-time S&P 500 membership, cached at
`MEMBERSHIP_PATH` (`VALUE_MEMBERSHIP_PATH`, default
`~/.tradingagents/value/sp500_history.csv`). The copy pulled during item 9 spans
**1996-01-02 .. 2026-06-30**, 2718 snapshots, 1206 distinct tickers — wider than
the 2014-2026 slice item 9 quoted. Snapshots are **change-driven, not weekly**
(modal gap 1 day, tail to 7), which is why the lookup forward-fills and why the
staleness limit exists. Dataset tickers carry share classes as `BRK.B`; the store
and yfinance use `BRK-B`, so the loader normalises.

- As-of lookup is the latest snapshot **at or before** the date. Never the
  nearest, never the next — the same look-ahead rule the fundamentals path
  already enforces.
- The window's tail is bounded by the dataset (2025-12-22), not by today.
- S&P 500 membership is itself a committee quality filter, so the universe is
  narrower than "all US large caps". That is acceptable and must be **stated in
  the report**: the benchmark is SPY, so screen-on-index vs index is
  apples-to-apples.
- Names that leave the index leave the universe on the date they left, and their
  open positions are liquidated at the next rebalance like any other exit.

**Acceptance:** a backtest date's universe equals the membership snapshot for
that date, intersected with what the store can actually value; the report prints
both counts and the shortfall between them.

**Done.** `backtest/membership.py` + `--universe index|store` on
`backtest.numeric` (index is the default; `store` reproduces the pre-4b runs and
prints a warning that it is biased). `tests/value/test_membership.py` and
`PointInTimeUniverseTest` in `tests/value/test_backtest.py` pin the two rules:
the snapshot used is the newest **at or before** the date, and a date outside the
data raises rather than yielding an empty universe.

The shortfall step 2 has to close, measured against the 1,020-ticker store:

| as-of | index members | in store | shortfall |
|---|---|---|---|
| 2014-12-31 | 499 | 368 | **131** |
| 2019-12-31 | 505 | 433 | 72 |
| 2024-12-31 | 503 | 484 | 19 |

The gradient is the bias itself: the further back the date, the more of that
year's index has since been acquired, taken private or demoted out of the
current ticker file.

### 2. Ingest the historical members the store never had  *(measurement)*

Step 1 makes the shortfall visible; this closes it. Of item 9's 289 missing
names, 119 resolved through EDGAR's current ticker file and 170 did not — the
acquired and delisted, i.e. the adversely-selected half.

- Resolve the 170 by **company name → CIK** from EDGAR's quarterly `form.idx`,
  not by ticker. Ticker identity drifts under a live company (BK → BNY in 2025),
  and delisted issuers keep no current ticker at all.
- Names still unresolved after that are listed individually in the report with
  their reason. No silent shrinkage.
- Coverage floor (80%) still excludes thin names, as with the 49 of 119.

**Acceptance:** the step-1 shortfall is either ingested or itemised by name and
reason. The report states the residual bias as a count, not as "some".

**Done, and it found the thing that matters more than itself.**

*Resolver.* `edgar/tickermap.py` merges SEC's two ticker files — `include/ticker.txt`
(~12k rows, includes issuers delisted within the last few years) and
`company_tickers.json` — into 16,467 ticker→CIK pairs cached at
`TICKER_MAP_PATH`. Measured against the 179 absent members: `company_tickers.json`
alone resolved **11**, `ticker.txt` **67**. Fable's proposed `form.idx` name→CIK
path was not needed, and EDGAR full-text search by ticker was tried and rejected —
querying `EMC`, `FB` and `BCR` against 10-K text returns unrelated filers in the
top hits, so it cannot be trusted as an identity resolver.

*Aliases, not ingest, for renames.* `screen/identity.py` maps a historical ticker
onto whichever ticker the store already holds that CIK under. The store's primary
key is `(cik, …)`, so a rename was never missing data — only a missing label. 473
aliases; over the window 777 raw members collapse to **763** distinct companies
(`BK`→`BNY`, `ABC`→`COR`, `GOOGL`→`GOOG`, `FOXA`→`FOX`, `NWSA`→`NWS`, `MMC`→`MRSH`,
`PKI`→`RVTY`, `RE`→`EG`, `UA`→`UAA`, `PEAK`→`DOC`, `CHK`→`EXE`, `FLT`→`CPAY`,
`NLOK`→`GEN`, `SATS`→`ECHO`). Deduplication is load-bearing: an index holding both
`GOOGL` and `GOOG` is one company, and screening it twice would double its weight.

*Ingest.* 53 of the 55 CIK-resolved members ingested (27 below the 80% coverage
floor, so excluded as designed); store 1,020 → **1,066** tickers. Shortfall after:

| as-of | index | after aliasing | in store | absent | was |
|---|---|---|---|---|---|
| 2014-12-31 | 499 | 498 | 405 | **93** | 131 |
| 2019-12-31 | 505 | 501 | 470 | **31** | 72 |
| 2024-12-31 | 503 | 500 | 498 | **2** | 19 |

Window union: 179 absent → **118**.

*The two that failed are the two that matter.* `FRC` and `SBNY` — First Republic
and Signature Bank, the 2023 failures — both return **404 from companyfacts**. The
names that went to zero are the names that cannot be ingested. That is adverse
selection stated as an HTTP status code, and no amount of resolver work fixes it.

### 2b. The binding constraint is prices, not filings

Established by direct probe before building anything, and it reframes the whole
step: **yfinance serves no history for a delisted ticker.** Empty for `ATVI`,
`ABMD`, `ANSS`, `CMA`, `BCR`, `CELG`, `RTN`, `UTX`, `XLNX`, `WFM`, `TIF`, `MYL`;
controls `AAPL` and `AEP` return 3,140 bars over the same window.

Consequences, in order of importance:

1. **Ingesting a delisted company's filings makes it screenable but never
   holdable.** It reaches valuation, finds no price, and lands in the report's
   `missing` set — excluded from returns exactly as before. The fundamentals work
   above improves *honesty* (the name is now counted rather than invisible) and
   changes no return number.
2. **So the residual 118 is not repairable by any EDGAR work.** Removing it needs
   a price source covering delisted issuers, which breaks the free-data design.
   What is left is to *bound* it — see step 3.
3. **Ticker reuse is a live hazard.** `EMC` returns 783 bars dated 2023-05-15 to
   2026-06-29; EMC Corp was acquired by Dell in 2016, so that series belongs to a
   different issuer. Splicing it in would be a confidently wrong price history.
   The existing guard holds: `History.close` raises when no bar exists at or
   before the as-of date, so a fully-reused ticker is rejected rather than
   substituted. The narrow residual case — delisting *and* reuse both inside the
   window, with bars before the as-of date — is unguarded and recorded here
   rather than solved.
4. The direction of the residual bias is **not** uniformly upward, which item 9
   assumed. An acquisition completes at a premium (good for a holder); a failure
   goes to zero. `SIVB`, `FRC` and `SBNY` are in the absent set and went to zero;
   `ATVI`, `ABMD`, `CTXS`, `MON` and `TWTR` left at a premium. Which way the net
   runs is what step 3 has to bound, and assuming "upward" is as unfounded as
   assuming nothing.

### 2c. The gate re-run on the clean universe

2014-01-01 → 2026-06-30, `years=7`, `tolerance=1`, 50 rebalance dates, 473 aliases
applied. Per date: 500 in the index, 246 screened, 7.5 passed, 7.2 valued, 35
absent from the store, 219 excluded on their merits. SPY +11.92% CAGR.

| MoS trigger | CAGR | vs SPY | max DD | trades | hit rate | item 9 vs SPY |
|---|---|---|---|---|---|---|
| 0% | +9.37% | −2.55% | 54.1% | 27 | 74% | +1.41% |
| 10% | +9.45% | −2.47% | 54.1% | 32 | 84% | −1.77% |
| 20% | +8.75% | −3.17% | 54.7% | 25 | 72% | −2.28% |
| 30% (configured) | +11.98% | **+0.06%** | **48.6%** | 24 | 75% | +0.77% |
| 40% | +10.22% | −1.70% | 34.6% | 17 | 71% | −0.94% |

**The item-9 verdict survives a cleaner universe: still no edge.** At the
configured trigger the excess over SPY falls from +0.77% to **+0.06%**, four of
five levels trail the index outright, and max drawdown at the configured trigger
is 48.6% against an index that fell ~34%. The screen again fails on the axis it
exists to win.

The grid remains non-monotonic — 9.37 / 9.45 / 8.75 / **11.98** / 10.22 — and the
one cell that clears SPY sits between two that trail it by 3.17 and 1.70 points,
holding nearly the same names. That shape is noise, and reading the 30% cell as
the result is the error that produced the item-8 verdict. Step 3 exists to stop
this being arguable.

Note also `screened 246` against `500` in the index: early dates screen only
128-143 names because `years=7` at a 2014 as-of reaches back to 2007, before XBRL.
The pre-2009 data ceiling from item 8 is still the ceiling, and the clean universe
did not raise it.

*One reporting defect found and fixed by this run.* The survivorship caveat
printed "0 of 28 names had no price series", and the first version of the
point-in-time text called that "the whole of the remaining bias". It is the
opposite: a delisted name never reaches that counter, because EDGAR has no
companyfacts for it or it fell below the coverage floor, so it is missing from the
universe rather than from the priced set. The counter is near-zero **by
construction** and the caveat now says so and points at the 118 named members
instead.

---

### 3. Statistical power and a pre-registered pass criterion  *(measurement)*

24 closed trades cannot rank adjacent trigger levels. The non-monotonic grid
(13.00 / 9.81 / 9.31 / 12.36 / 10.65 across triggers holding nearly the same
names) **is** the confidence interval, and reading it as a signal is what
produced two of the three wrong verdicts.

- Report bootstrap confidence intervals from resampled rebalance dates for CAGR
  vs benchmark and max drawdown.
- **Pre-register the pass criterion before the run**, in this file: the gate
  passes only if the CI for CAGR vs SPY excludes zero **and** max drawdown does
  not exceed the index's, at the configured settings — not at a
  best-of-grid setting.
- Stop reporting the trigger grid as evidence. Keep it as a sensitivity display,
  labelled as such.

**Bound the unpriceable residual**, since step 2b established that repairing it is
impossible on free data. For the absent names that pass the screen, re-run with a
stub terminal return assigned to positions that cannot be priced, swept over
{−100%, 0%, +25%}: total loss, dead money, and a typical acquisition premium. If
the verdict is the same at all three, the residual does not matter and can be
declared closed. If it flips, the honest report is an interval, not a number —
and that interval is the answer phase 4 asks for.

**Acceptance:** the report carries CIs, and the verdict cites the pre-registered
criterion rather than a chosen cell. The unpriceable residual is reported as a
swept interval rather than as a caveat sentence.

**Done, and the verdict is fail — for the first time on evidence rather than on a
defect.** `backtest/stats.py` + `--bootstrap`/`--seed` on `backtest.numeric`;
`Result.curve` records portfolio value per bar because intervals need the path,
not the endpoints. The configured trigger is now forced into the grid whatever
`--mos-grid` says, so the criterion can never be judged off a chosen cell, and the
grid is printed under the label *sensitivity display — not evidence*.
`tests/value/test_stats.py` (26 tests) pins the arithmetic; two integration tests
in `test_backtest.py` pin that the report carries the interval and the verdict.

Same run as 2c — 2014-01-01 → 2026-06-30, `years=7`, `tolerance=1`, 49 rebalance
periods, 2000 resamples, 95% CI, at the configured 30% trigger:

| | point | 95% CI | vs SPY |
|---|---|---|---|
| CAGR vs SPY | +0.26% | **[−10.73%, +10.53%]** | contains zero |
| max drawdown | 35.6% | [15.9%, 64.2%] | 24.8% |

**VERDICT: fail.** Both halves of the criterion fail independently. The interval is
±10 points wide around a +0.26% effect — the apparatus cannot resolve what phase 4
is asking about, which is the answer phase 4 exists to give rather than a reason to
keep tuning. It also settles 2c's suspicion: the +0.06% cell at 30% was noise, and
so was item 8's +2.67%. Note the benchmark drawdown reads 24.8% here against ~34%
elsewhere — measured on the same 49 quarterly periods as the strategy, so
peak-to-trough within a quarter is invisible to both sides equally.

*The residual is closed.* Per rebalance the unpriceable share is 18.0% of NAV
(passers with no price series, plus store-absent members estimated at the observed
pass and trigger rates). Swept over the three stub terminal returns, amortised over
the 4.4-period average holding span:

| stub terminal return | CAGR vs SPY | max DD | verdict |
|---|---|---|---|
| −100% (total loss) | −18.35% [−28.00%, −9.45%] | 77.8% | fail |
| 0% (dead money) | −1.67% [−10.85%, +6.56%] | 27.5% | fail |
| +25% (acquisition premium) | +2.74% [−6.34%, +11.07%] | 23.2% | fail |

The verdict holds at all three, so **the residual does not change the answer** and
step 2b's unrepairable hole is closed as a question. Note the direction: even
assuming every unpriceable name was acquired at a premium, the CI still contains
zero. Item 9's "the bias runs upward" assumption was wrong in both directions —
it does not run far enough either way to matter.

*One defect found in this step's own apparatus, before its numbers were believed.*
The first implementation charged each stub **per rebalance period** while a position
is held ~280 bars (4.4 periods), so "+25% once" compounded to +25% a quarter and
printed a spurious pass (+18.37% [+7.97%, +28.50%]). Stubs are terminal returns and
are now amortised across the holding span — arithmetically, because a total loss has
no geometric per-period equivalent. Fixed before reporting; the headline interval is
untouched by it, since amortisation only enters the sweep.

**One soft edge, stated rather than buried:** a name with no price cannot be checked
against the MoS trigger either, so the phantom count is scaled by the priced
population's trigger rate on the same date. That assumption sets the 18.0% weight.

### 4. MoS: trigger → ranking key  *(strategy — first strategy change, alone)*

Item 8's own 0%-trigger row beat the configured 30% row on CAGR, and item 4
showed the one-sided terminal-P/E cap makes MoS largely noise. Gating on a noisy
quantity selects noise. Buy top-N on quality rank, MoS as tie-break.

Also triples trade count, which feeds step 3.

**Acceptance:** run alone, on the clean universe, against the pre-registered
criterion.

**Done. Fail, and the hypothesis behind it is not supported.** `ScreenResult.quality`
scores the share of (blocking criterion, year) checks that came out clean — the
module's own "sustained" thesis as a number, with unevaluated years counting against
it exactly as violations do. `Snapshot.top_ranked` sorts on it with the margin of
safety breaking ties and unscored names last; `--select trigger|rank`, `--rank-grid`
and `VALUE_BACKTEST_TOP_N` (10) drive it. `trigger` stays the default so nothing
about step 3's verdict moves underneath. `QualityScoreTest`, `RankSelectionTest`.

Same run as 2c and step 3, `--select rank`:

| position count | CAGR | vs SPY | max DD | trades | hit rate | avg bars held |
|---|---|---|---|---|---|---|
| top 5 | +7.05% | −4.86% | 48.3% | 28 | 71% | 369 |
| **top 10** (configured) | +9.04% | **−2.88%** | 54.0% | 24 | 83% | 540 |
| top 15 | +8.77% | −3.14% | 54.0% | 19 | 79% | 643 |
| top 20 | +8.77% | −3.14% | 54.0% | 19 | 79% | 643 |

At the configured setting: CAGR vs SPY **−2.68% [−11.89%, +4.90%]**, max drawdown
**43.4%** against SPY's 24.8%. **VERDICT: fail**, both halves. Residual sweep fails
at all three stubs (−13.09% / −3.50% / −1.02%), so it is closed here too.

*Three things this run establishes, two of them against expectation:*

1. **The turnover prediction was backwards.** Step 4 was supposed to triple trade
   count and narrow the interval that way. Trades went 24 → 24, and average holding
   went 280 → 540 bars: a name that ranks top-10 one quarter still ranks top-10 the
   next, so ranking *lowered* turnover. The interval did narrow (21.3 → 16.8 points
   wide), from holding a steadier book rather than from more trades. It still
   contains zero.
2. **The rank grid saturates above ~10.** Top 15 and top 20 are identical to the
   digit, because only ~7 names reach a valuation on an average date. At N≥15
   "top-N" *is* "hold everything that passed" — and its +8.77% sits beside step 3's
   0%-trigger row at +9.37%, which is the same portfolio by another name. Only
   N=5 and N=10 are distinct measurements here.
3. **Replacing the gate with a ranking did not help.** The point estimate moved from
   +0.26% to −2.68% — worse, though the two intervals overlap so heavily that the
   honest statement is "indistinguishable, and neither is separated from zero". Item
   8's argument that the MoS gate selects noise may still be right; it simply does
   not follow that ranking on quality selects signal.

*What is left.* The failure is now concentrated on drawdown: 43.4% against an index
that fell 24.8% over the same periods, holding ten names at equal weight with no cap
and no minimum. That is exactly step 5's target, and step 5 is the last free test.

### 5. Portfolio construction  *(strategy — second strategy change, alone)*

The 47.6% drawdown in item 7 was one Biogen position at 100% of NAV, not the
strategy. Current construction is equal weight across whatever passed, quarterly
rebalance, no cap.

- Position cap ~15% NAV; minimum 5 names before the strategy invests at all;
  maximum ~15.
- **Sell on quality exit, not on MoS closing** — see the deferred list below.

**Done. The drawdown half of the criterion passes for the first time; the gate
still fails, and the change that was supposed to matter most did nothing.**

`BACKTEST_POSITION_CAP` (15%), `BACKTEST_MIN_POSITIONS` (5) and
`BACKTEST_MAX_POSITIONS` (15) drive `--construct equal|capped`; the quality exit
is `--exit rebalance|quality`. Both default to the pre-step-5 behaviour, so
step 3's and step 4's verdicts do not move underneath. The cap is applied inside
`portfolio.Rebalance`; everything else is `numeric.construct`, which sits above
whichever selection is running rather than inside it. `ConstructionTest`,
`PositionCapTest`.

Three rules, and what each is careful *not* to do:

- **Cap.** The excess above 15% stays in cash rather than being spread over the
  remaining names — spreading it would push those through the cap in turn.
- **Minimum.** It blocks *opening* a book, never forcing a liquidation. A book
  that has decayed to three names decayed through quality exits, and selling
  those three is precisely the trade step 5 exists to stop.
- **Maximum.** Incumbents are listed first, so it turns away new ideas rather
  than selling held ones.

`Snapshot.passed_names` was added so the quality exit tests the criteria rather
than the valuation: a name that passed and then had no quote is a *price* exit
wearing a quality exit's clothes, and it keeps its slot.

Same run as 2c, step 3 and step 4 — 2014-01-01 → 2026-06-30, `years=7`,
`tolerance=1`, 49 rebalance periods, 2000 resamples, 95% CI. Three runs, each one
change from a stated baseline:

| run | base | levers | CAGR vs SPY | 95% CI | max DD | verdict |
|---|---|---|---|---|---|---|
| step 3 | trigger | — | +0.26% | [−10.73%, +10.53%] | 35.6% | fail, both halves |
| step 4 | rank | — | −2.68% | [−11.89%, +4.90%] | 43.4% | fail, both halves |
| 5a | rank | cap + min + max | −2.20% | [−7.26%, +2.79%] | **18.9%** | fail, CAGR only |
| 5b | rank | cap + min + max, quality exit | −2.48% | [−7.63%, +2.44%] | **18.9%** | fail, CAGR only |
| 5c | trigger | cap + min + max, quality exit | −2.20% | [−8.31%, +3.98%] | **19.5%** | fail, CAGR only |

Benchmark drawdown is 24.8% over the same 49 periods throughout.

**VERDICT: fail.** One half of the criterion now holds — every step-5 run draws
down less than the index, against 43.4% in step 4 — and the other half is not
close: three independent point estimates at −2.20%, −2.20% and −2.48%, every
interval containing zero and every one of them centred below it.

*What the attribution says, and it is not what the plan predicted:*

1. **The cap did all of the work.** 5a and 5b report the same 18.9% drawdown to
   the digit. The quality exit contributes nothing to the axis step 5 was aimed
   at, and its CAGR is 0.28 points *worse* — well inside noise, but not the
   direction F1 argued for.
2. **F1 is not supported by this test.** "Buffett's return is the not-selling"
   was the highest-fidelity single change on the deferred list. Measured, it
   lengthens the average hold from 476 to 616 bars, cuts closed trades from 25
   to 19, and moves nothing that the criterion reads. The argument may still be
   right about Buffett; it is not right about this screen on this window.
3. **Capping raised CAGR while cutting drawdown.** Step 4's uncapped rank book
   returned +9.04%; 5a returns +9.52% holding materially more cash. The
   concentration it removed was buying risk without return — which is item 7's
   lesson restated as a measurement rather than as an anecdote.
4. **The interval narrowed by a third and still contains zero.** 21.3 points at
   step 3, 16.8 at step 4, ~10.1 here. Three rounds of apparatus work have
   halved the noise and the effect has not emerged from it — it has drifted
   *below* zero as the measurement got cleaner.
5. **5c is thin enough to distrust on its own.** The trigger base with a
   5-name minimum closes 8 trades over the window, and its 40% grid row closes
   2. It is reported because it is one change from step 3's baseline, not
   because 8 trades rank anything.

*The residual is closed here too.* Swept at −100% / 0% / +25% terminal returns on
the unpriceable share, every run fails at all three stubs (5b: −11.32% / −3.62% /
−1.64%). As at step 3 and step 4, even assuming every unpriceable name was
acquired at a premium the interval still contains zero.

*One defect found in this step's own apparatus, before its numbers were believed.*
`stats.phantom_weight` scales the unpriceable population by the observed selection
rate, `held / valued`. Under the quality exit the book carries incumbents forward,
so `held` counts names selected on earlier dates and the ratio exceeds 1.0 —
claiming more phantom positions than there are phantom names, and inflating the
very bound that exists to keep the report honest. The rate is now capped at 1.0.
It is a no-op for the trigger and rank schedules, where the book is always a
subset of `valued`, so no earlier verdict moves under the fix.

*And one in the harness rather than the arithmetic.* The first `--construct` help
string contained a literal `15%`; argparse `%`-expands help text and refused to
build the parser at all, so the CLI was completely dead while all 241 tests stayed
green — because no test reached `main`. `CommandLineTest` now builds the parser.
Worth recording as a class of hole, not a typo: the suite covered every function
the entry point calls and not the entry point.

---

### 6. Is phase 6 answerable at all?  *(measurement, run before deciding to build)*

Asked before writing any analyst code, because "run it and see" is how items 7,
8 and 9 each produced a verdict that a defect in the test later overturned.

Phase 6 asks whether an LLM veto improves outcomes **over the numeric screen
alone**. That is a *paired* comparison — filtered book against unfiltered book
over the same dates — so its noise floor is not the ~10-point strategy-vs-SPY
interval. It is the interval around the difference, and that depends on how much
of the book the veto actually changes.

*Method.* Veto names at random at rate v. A random veto has no skill, so
whatever interval the bootstrap puts around its zero effect is the noise floor
at that rate: the **minimum detectable effect**. Then veto by oracle — drop the
picks with the worst forward return — to bound what a *perfect* veto could earn.
Look-ahead by construction and declared as such, in the same spirit as the
residual stub sweep. Both arms reuse `numeric.construct` and
`portfolio.simulate`; the veto is applied to `picks`, so a vetoed name never
enters the book. 10 reps per cell, $0 in tokens.

Oracle horizons of 1, 4 and 8 rebalance periods, because a one-quarter oracle
vetoes a name for a bad quarter inside a good multi-year hold. The first pass ran
at one quarter only and printed a *negative* ceiling, which is the tell that it
was not a ceiling at all.

| exit | veto | book cut | MDE | oracle @1q | @4q | @8q | verdict |
|---|---|---|---|---|---|---|---|
| quality | 10% | 1% | +0.66% | +0.36% | +0.21% | +0.27% | below MDE |
| quality | 25% | 5% | +1.20% | +0.93% | +0.94% | +0.61% | below MDE |
| quality | 50% | 12% | +2.04% | −3.01% | −1.60% | −1.63% | below MDE |
| rebalance | 10% | 13% | +1.83% | **+5.02%** | −0.07% | −0.88% | 1q only |
| rebalance | 25% | 34% | +3.78% | **+10.81%** | +1.69% | −0.09% | 1q only |
| rebalance | 50% | 72% | +7.57% | +5.02% | −1.10% | −2.75% | below MDE |

**Phase 6 is not answerable, and the reason is the horizon rather than the
sample size.**

1. **Under the step-5 quality exit the veto has no leverage.** A 25% veto cuts
   5% of the book: incumbents carry forward regardless, so an entry-time filter
   barely touches what is held. Even a perfect veto sits below the noise floor
   at every rate. Any phase 6 would have to run on `--exit rebalance`, which
   step 5 showed costs nothing to give up.
2. **The only headroom is at a one-quarter horizon** — +5.02% and +10.81%
   against floors of +1.83% and +3.78%, i.e. a filter would need ~35% of a
   perfect veto. But that oracle is a next-quarter *price* predictor.
3. **Every long-horizon oracle is below its floor.** At 4 and 8 periods the best
   perfect veto earns +1.69% against a +3.78% floor, and is negative in five of
   six cells. A veto that knows which businesses do worse over two to four years
   — which is exactly what reading Item 1, 1A and 7 is for — cannot be separated
   from noise on this apparatus even when it is right every time.
4. So the headroom that exists is on the horizon tier 3 has no claim to, and the
   horizon tier 3 is designed for has no headroom. Building the analyst to
   *measure* an edge is spending against a question this apparatus cannot answer.
5. **A 50% veto is self-defeating** at any horizon: cutting 72% of the book makes
   the cutting itself the dominant noise source. Useful range is 10-25%.

*Limits of this measurement, stated rather than buried.* 10 reps per cell is thin
and the medians move a few tenths between seeds. The oracle vetoes only at entry,
so a real ceiling — one that could also time exits — is higher than these numbers.
The `min 5` rule interacts with heavy vetoing, which is part of why the 50% rows
go negative. None of that changes the ranking of ceiling against floor at the long
horizons, which is where the conclusion rests.

*Not shipped.* The probe is an analysis script, not a product module, so it has no
tests and does not live in `tradingagents/value/`. The method above is sufficient
to rebuild it; it is worth promoting to `backtest/power.py` with tests only if a
future phase needs to re-ask this question.

---

## Refuse list

Degrees of freedom are already spent. Do not:

- tune any threshold against 2014-2026 again
- promote `VIOLATION_TOLERANCE=2` because the sensitivity row printed +3.11%
- try `years=5`
- uncap `TERMINAL_PE_CAP` because item 4 showed uncapping adds MoS
- bundle steps 1-2 with 4-5 in one run. Universe first; then one strategy change
  per run, or the result is unreadable
- read a best cell out of a grid the CIs say is noise

## Stop condition

Steps 1-3, then 4, then 5 — as the last free test. If top-N quality on a
survivorship-free universe shows no CI-separated edge, **stop**. An effect of
~2 points over one decade of 48 rebalance dates is unresolvable by this
apparatus at any effort level, and giving that answer is what phase 4 is for.

**Reached. Phase 4b is complete and the phase-4 gate does not pass.**

Every step landed: the universe is point-in-time and survivorship-free by
construction, the resolvable half of the missing members is ingested and the
unresolvable half is named and bounded, the criterion was pre-registered before
the run that judged it, and both free strategy changes have been measured alone.
Across the three that were judged on evidence rather than on a defect:

| | CAGR vs SPY | 95% CI | max DD vs index |
|---|---|---|---|
| step 3 — MoS trigger | +0.26% | [−10.73%, +10.53%] | 35.6% / 24.8% |
| step 4 — quality rank | −2.68% | [−11.89%, +4.90%] | 43.4% / 24.8% |
| step 5 — rank, capped book | −2.48% | [−7.63%, +2.44%] | 18.9% / 24.8% |

No interval excludes zero, and the point estimate moves further below it as the
measurement gets cleaner. The apparatus improved by every measure asked of it —
the interval narrowed from 21.3 points to 10.1, and the drawdown objection is
now answered — and the edge did not appear. That is a result, not an
inconclusive run: three rounds of removing defects moved the estimate down, and
the one axis the screen was built to win it now wins by holding cash.

**So nothing here funds phases 5-6.** The tier-1 numeric screen has been given a
fair test on clean data against a criterion fixed in advance, and did not clear
it. Spending LLM tokens on a value analyst layered over this screen would be
spending them on a selection process that is not distinguishable from the index.

What remains available is not more tuning — the refuse list above is still
binding, and every degree of freedom it names is still spent. It is either the
deferred Buffett-fidelity list (F2-F6), judged on its own terms as a different
screen rather than as another pass at this one, or a point-in-time fundamentals
vendor covering pre-2009 and delisted issuers, which breaks the EDGAR-only
design and costs money. Both are new decisions. Neither is a continuation of
phase 4.

---

## Deferred: Buffett-fidelity changes

Judged separately from the measurement work. The screen as configured is a
faithful Graham-flavoured quality screen; it is not a Buffett replication. These
are the gaps worth closing, and none of them may ride along with a gate run.

| # | Change | Why | Status |
|---|---|---|---|
| F1 | **Sell on quality exit, not on MoS closing** | Buffett's return *is* the not-selling. Quarterly rebalance ejects a compounder the moment a conservative DCF calls it rich. Highest-fidelity single change, and the existing evidence already argues for it | **measured in step 5 — no effect.** Holds 616 bars against 476 and closes 19 trades against 25; drawdown identical to the cap alone, CAGR 0.28 points worse. Not supported by this test |
| F2 | `SGA_TO_GROSS_PROFIT_MAX` 0.80 → ~0.50 | The book's ladder is <30% moat / 30–80% competitive / >80% brutal. Item 6 moved it to the **ceiling** of acceptable, where the gate admits everything but brutal industries and is dead weight. 0.30 admitted 9%, so the answer is a middle, not the ceiling | deferred |
| F3 | Criterion 13: treasury-stock **presence** → share-count **reduction** | Presence rewards an accounting artifact; many buyback machines retire shares outright and carry no treasury line, so the criterion misses exactly what it aims at. Diluted share counts are already in the store, so the reduction test is free | deferred |
| F4 | Value **owner earnings**, not diluted EPS | Owner earnings is the book's central concept; plan §6 defines it and then demotes it to a sanity anchor. Would also retire criterion 11 (capex/net income), which is a proxy for it | deferred |
| F5 | Exclude financials explicitly | Gross margin is a non-fact for banks, and item 5 proved it. Unfaithful to Buffett-the-man, who likes insurers and float, but faithful to this screen's method — and float leverage is unreplicable here | deferred |
| F6 | Drop redundant gates: criterion 6 (interest/operating income), 5 (D&A/gross profit) | Implied by the two debt gates and by capex respectively. Fewer conjunctive gates is the item-6 lesson | deferred |
| F7 | Reconsider `MARGIN_OF_SAFETY_MIN` 0.30 | A 30% discount to a 15%-capped-growth, 15x-capped-terminal DCF discounted at a 4%-floored risk-free rate is quadruple-counted conservatism — which is why item 4 found intrinsic value systematically low. It also filters out every wonderful-business-at-a-fair-price by construction, which is Graham, not post-1972 Buffett | superseded by step 4 |

**Explicitly not doing:** mechanical concentration and circle-of-competence.
Biogen at 100% of NAV is what mechanical concentration without judgment
produces. Equal weight under a position cap is the correct infidelity.
