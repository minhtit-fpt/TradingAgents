# Dividend feature — the screen (D1), the book (D2), the weekly job (D3), the backtest (D4)

Deliverable: `tradingagents/value/dividend/`. Tests: `tests/value/test_dividend.py`,
`tests/value/test_dividend_ledger.py`, `tests/value/test_dividend_weekly.py`,
`tests/value/test_dividend_backtest.py`.
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

## D4 — the backtest

`tradingagents/value/dividend/backtest.py`, tests in
`tests/value/test_dividend_backtest.py`. Closes both items that D3 left open.

The question is deliberately narrower than phase 4b's. That replay asked whether
a screen beat SPY, which needs a simulated book, costs and a rebalance schedule.
This one asks the thing the dividend screen actually claims:

> a name that clears the screen on date X — does it still hold its dividend
> through X+horizon, more often than a name that failed the screen on the same
> date?

It is answerable from the dividend cache alone, offline, before any question
about price. A screen that cannot separate the cutters is not worth pricing.

**The line that matters.** The forward window is look-ahead on purpose: it is the
outcome being scored, not an input to the decision. Everything on the decision
side — dividend history and 10-K facts alike — goes through the same
point-in-time filters the live screen uses, and a test pins exactly that: a name
whose dividend collapses in the first forward year must still *pass* the screen
standing on the cohort date.

Design notes worth keeping:

- **Baseline is the last year the screen itself saw**, not the first forward
  year. Otherwise a board that cuts immediately reads as a new record starting
  low.
- **A year with no payment is a fall to zero, not missing data.** A payer that
  simply stops has done the exact thing the screen exists to avoid; calling that
  "no data" would score the worst outcome as the absence of one.
- **One read of the inputs answers every grid cell.** The payout sweep differs
  in a single comparison, so screening the universe once per cell would triple
  the work for the same three answers.
- **Cluster bootstrap over cohort dates**, not over names. Two payers screened
  the same morning share a market; resampling them independently would treat one
  correlated decade as a few hundred trials — the phase-4 error in a new costume.
  The cost is a wide interval, and a wide honest interval is the finding.
- **Return uses split-adjusted `Close`, never `market.AS_TRADED`.** This is the
  only place in the feature that puts a price and a figure from the `dividends`
  table over one line, and the two share a basis exactly when the price is the
  split-adjusted one. `AS_TRADED` undoes the splits on the price and would read
  a 4-for-1 as a 75% loss against an unchanged dividend. A test holds it.

Cohort return against the benchmark is reported and **gates nothing** —
equal-weighted buy-and-hold cohorts, no costs, no rebalancing, dividends
collected and not reinvested. What it is good for is catching the opposite
failure: a screen that avoids cuts by only ever naming companies going nowhere.

### D4 result

`pytest tests/value` — 449 passed, `ruff check .` clean. Dividend cache warmed
across the whole store: of 1,066 names with 10-K facts, **725** have a dividend
history and 341 have never paid one (AMZN, AMD, ANET and similar) — reported,
not silently dropped. Cohorts 2012–2019 on a five-year forward window:

```
8 cohorts, 4099 name-dates, 5-year forward window, payout_max 0.60
  2012-01-02 -> 2017-01-02  pass  95 (cut   15%)  reject  312 (cut   26%)
  2013-01-02 -> 2018-01-02  pass 109 (cut   17%)  reject  365 (cut   33%)
  2014-01-02 -> 2019-01-02  pass 102 (cut   10%)  reject  386 (cut   23%)
  2015-01-02 -> 2020-01-02  pass 104 (cut   12%)  reject  415 (cut   22%)
  2016-01-02 -> 2021-01-02  pass 104 (cut   19%)  reject  430 (cut   32%)
  2017-01-02 -> 2022-01-02  pass  98 (cut   20%)  reject  449 (cut   31%)
  2018-01-02 -> 2023-01-02  pass 100 (cut   14%)  reject  458 (cut   32%)
  2019-01-02 -> 2024-01-02  pass 122 (cut   16%)  reject  450 (cut   34%)

cut rate, reject arm less pass arm: +13.72% [+11.53%, +15.84%]
VERDICT: pass against the pre-registered criterion

  cohort return vs SPY, per holding window: +13.40% [+4.80%, +21.59%]

payout_max sensitivity — a display, not evidence:
  0.50:  715 passes, cut-rate gap +14.38% [+12.11%, +16.54%]
  0.60:  834 passes, cut-rate gap +13.72% [+11.53%, +15.84%] <- configured
  0.70:  898 passes, cut-rate gap +14.68% [+12.30%, +17.01%]
```

This is the module's first replay to pass its own pre-registered criterion —
worth stating plainly beside 4b and 6, and worth reading narrowly. It says the
screen separates future cutters from the rest. It does not say the screen beats
SPY.

An earlier run on a hand-picked 49 long-listed payers gave +8.22%
[+2.97%, +13.04%]. The full universe did not deflate that, it sharpened it: when
both arms are drawn from names that have paid for decades, the reject arm is also
full of good companies and the gap is compressed. Choosing the sample by hand
understated the effect. That is the opposite of the failure that was expected
from it, and it is the reason the item was not left as a caveat.

**On `VALUE_DIVIDEND_PAYOUT_MAX`.** Measured, and the answer is that it barely
matters on this axis: 0.50, 0.60 and 0.70 give cut-rate gaps of +14.38%, +13.72%
and +14.68%, three intervals overlapping almost entirely. What moves is the count
— 715, 834, 898 passes. So the level is a breadth-versus-concentration
preference, **not** a safety trade-off, and the seven names D3 flagged (PG, PEP,
ABT, TGT, SYY, GPC, MCD) are not being excluded for cause. 0.70 shows the
highest point estimate, which is exactly the cell the pre-registered criterion
forbids reading a result from; the 0.96-point spread sits inside the noise.
Default stays 0.60 because nothing here argues for moving it. An operator who
wants a wider income list can set 0.70 knowing what it buys, which is what a
measured knob is for.

### Two defects the D4 runs exposed

**The benchmark was measured on a different basis from the names.** The first
priced run reported +25.99% excess against SPY. `total_return` reads its
dividends from the `dividends` table, and an uncached name yields an empty list —
at that layer indistinguishable from a name that paid nothing. SPY is an ETF with
no 10-K facts, so no cache warmer ever reaches it: every name was measured with
its dividends and the benchmark without. Correcting it moved the figure to
+13.40%, i.e. the overstatement was SPY's own yield compounded over five years.
Fixed as a hard stop (`BenchmarkError`), not a caveat — a quietly price-only
benchmark is precisely the confidently wrong figure the module's error rule
exists to prevent.

**The first cohort could not be priced at all.** It reported `unpriced 47` —
every name in it. Cohort dates land on 2 January, a market holiday about as often
as not, and a price frame fetched *from* the cohort date has no bar at or before
it. Every name silently left the return figure and the run raised nothing. Fixed
by fetching from `ENTRY_LOOKBACK_DAYS` before the first cohort; a test asserts a
holiday entry date still prices.

Both belong to the same family as the two D3 defects, and it is now four in a
row: the wrong answer was never an exception, always a quietly emptier number.

## Still open

- **Survivorship, and it is no longer hypothetical.** The corrected run reports
  `unpriced 0` across all 4,099 name-dates — not one name lost its price series
  over a decade. A real 2012 universe would have lost several per cent of its
  members to delisting and acquisition, so their absence is a property of the
  store, not of the market. Every number above is conditioned on surviving to
  2026. Phase 4b step 2b established this hole is unrepairable on free data;
  what is missing here is 4b's habit of *bounding* it with a stub sweep rather
  than noting it.
- **No costed simulation.** The return line is equal-weighted buy-and-hold
  cohorts, and the mean of individual multi-year returns runs above a
  cap-weighted index mechanically. Whether a real book of these names,
  rebalanced and paying commission, beats SPY is the phase-4b question and is
  still unanswered for the dividend screen.

## D5 — the two things the operator's own brief asked for

Stated after D4 shipped, and worth writing down because one of them is a
requirement the module can serve and the other is a requirement no screen can:

> a portfolio bought to pay cash for monthly living expenses; 5–10% yield is
> fine; the whole portfolio must not lose more than 5%.

### Yield, finally — on the candidate list

Deferred since D1 for a real reason (the split-basis trap) and now cheap for an
equally real one: **at the latest bar `AS_TRADED` equals `Close`**, because
`AS_TRADED` is `Close` with *later* splits undone and today has none after it.
So today's price over the dividend table's back-adjusted per-share figure is
exact, and only today's is. `weekly.forward_yield` therefore prices at
`date.today()` even when the run carries a past `--as-of`, and the column is
labelled as today's rather than repaired.

Ranking changes with it. The screen's order is clean-share, which answers how
*durable* the payout is; a book that is spent from also needs how *much*, so
yield decides the order. The whole pass list is priced in **one** `yf.download`
(`history.last_closes`) rather than name by name, which is what makes ranking
the whole list affordable. A name with no price reads `yield unknown`, never
`0.00%`, and a dead feed becomes a note beside the breaks rather than an
exception — the D3 rule, reused.

**On the 5–10% target.** It is not reachable through this screen, and the
conflict is structural rather than a matter of tuning: `PayoutRatio ≤ 0.60`
excludes the entire class of securities that yields that much — REITs, BDCs,
MLPs, high-payout utilities — because they pay out most of what they earn. What
clears a decade of payments, no cut, payout inside earnings and free cash flow
covering the cheque yields roughly 2–4%. The payout sweep in D4 already measured
that loosening the limit to 0.70 buys breadth and not yield. The honest options
are a lower income expectation on a screen that has evidence behind it, or a
different screen for a different asset class; silently widening this one until
the yield line looks right would be fitting the criteria to a wish.

### Drawdown, because a screen cannot bound a loss

"The whole portfolio must not lose more than 5%" cannot be delivered by choosing
better payers. It is an allocation question, and answering it needs a measured
number rather than a reassurance, so `backtest.book_drawdown` reports the worst
peak-to-trough fall of an equal-weight book of the names that passed, held to
the end of each forward window.

**Price only, and this is the one place in the module where excluding dividends
is the accurate choice.** The return line collects them because it asks what the
names did. This line asks what the *account* did, and a book funded for living
expenses has spent that cash by the time the fall arrives — it is not there to
cushion it. Counting it would flatter precisely the number the floor is sized
against.

The report converts it once, with `sizing_for_floor`: the share of capital that
would have kept the book inside the floor, the rest in something that does not
fall. `--loss-floor` moves it. It is arithmetic on the worst window in the
sample and is labelled as such — not a recommendation, and not a bound on the
next fall, which can be deeper than anything measured here.

### D5 result

`pytest tests/value` — 469 passed (18 new), `ruff check .` clean. Both surfaces
run against the warm store (726 cached histories, 1,066 with facts) on
2026-08-25.

**The drawdown, which is the answer to the 5% question.** Cohorts 2012–2019, the
equal-weight book of everything that passed, held five years, price only:

```
  2012-01-02 -> 2017-01-02   -11.6%
  2013-01-02 -> 2018-01-02   -11.4%
  2014-01-02 -> 2019-01-02   -19.1%
  2015-01-02 -> 2020-01-02   -19.0%
  2016-01-02 -> 2021-01-02   -36.8%
  2017-01-02 -> 2022-01-02   -37.2%
  2018-01-02 -> 2023-01-02   -36.4%
  2019-01-02 -> 2024-01-02   -37.8%

worst across cohorts: -37.8%
holding the whole portfolio inside 5% would have needed at most 13% of capital
in these names, the rest in something that does not fall.
```

The split in that column is one event: every cohort whose window contains
February 2020 falls about 37%, every earlier one about half that. So the honest
reading is not "these names fall 12%" but "a decade with one crash in it prices
the floor, and the sample contains exactly one". Thirteen per cent is the
sizing that survived the worst month in the sample, not a bound on the next one.

**The yield column, and a defect the first live run exposed.** The first version
priced the top 25 candidates by clean-share and re-sorted those. It looked like
cost control and was an alphabetical cut: over a hundred names tie at 100%
clean, so the slice ran BR, CBSH, CDW, CSL, DGX, DPZ … and the message ranked
ten names yielding 1.1–2.0% while calling itself a ranking by income. Every
higher yielder later in the alphabet was scored `unknown`. Replaced with one
batched download over the whole pass list:

```
CANDIDATES (153 pass, not held)
  RHI  clean 95%, yield 5.23%      OMC  clean 98%, yield 3.26%
  EMN  clean 92%, yield 4.58%      MKC  clean 92%, yield 3.26%
  SWKS clean 95%, yield 4.22%      PB   clean 100%, yield 3.22%
  HPQ  clean 100%, yield 4.08%     UBSI clean 92%, yield 3.12%
  HBAN clean 98%, yield 3.64%      HRB  clean 90%, yield 2.96%
```

That is the fifth defect in this family and the same shape as the other four:
never an exception, always a quietly emptier or quietly wronger number that
renders as if it were the answer.

It also sharpens the yield finding. The top of the list reaches 5.2%, not the
2–4% the D5 note first assumed — a book of the ten highest yielders here would
run about 3.6%. The 5–10% *portfolio average* remains out of reach for the
structural reason above, but the gap is smaller than stated and the top of the
list is worth reading before concluding anything about it.

### 2008 is not measurable point-in-time, and what was measured instead

The obvious next question after a -37.8% worst case is what 2008 did. It cannot
be answered the way the rest of D4 is answered: the store's earliest EDGAR
filing is **2009-10-27**, so a cohort standing on 2008-01-02 or 2009-01-02 sees
no facts at all and screens **zero** names. Not thin — empty. The first workable
cohort is 2011 (254 screenable, 54 pass), and 2012 is where D4 starts for that
reason.

So the GFC number was taken the only other way available, with the hindsight
stated rather than hidden: the 153 names that pass the screen **today**, priced
through the crash. 142 of them had a series back to 2007; the 11 that did not
are post-crisis spinoffs and IPOs (CDW, MPC, XYL, HII, ALSN …) which simply did
not exist, so the sample tilts old.

```
2007-10-01 -> 2009-03-31   -54.6%
  floor  5%: at most  9% of capital     floor 20%: at most 37%
  floor 10%: at most 18% of capital     floor 30%: at most 55%
```

The selection is hindsight, but it is worth being precise about which kind. The
list is not conditioned on having survived 2008 *with the dividend intact* — the
screen reads 2015–2024, so it contains banks (HBAN among them) that cut
savagely in 2009 and rebuilt afterwards. What it is conditioned on is existing
in 2026. That makes -54.6% a good deal closer to the real figure than a
survivors-only replay would be.

The practical consequence for the sizing line: the 5% floor implies 9–13% of
capital in these names depending on which crash is taken as the reference, and
the module has no basis for preferring one. Both are in the record; neither
bounds the next one.

Left as a scratch measurement rather than a flag on `backtest.py`. A
`--include-2008` switch would put a hindsight-selected number inside the surface
whose whole discipline is that the decision side is filtered as-of, and one
labelled paragraph in a plan is cheaper than a footnote nobody reads on a
command output.

## D5 — the price-stability rank

Deliverable: `dividend/stability.py`. Tests: `tests/value/test_dividend_stability.py`.

The requirement, in the operator's words: a book of names that pay cash
dividends regularly, whose price does not move much, does not fall much, and
rises if it can.

D1 answers only the first clause. It reads the payout and nothing else, and D4
measured what that omission costs — an equal-weight book of the whole pass list
fell 37.8% through 2020 and 54.6% through 2008. So D5 scores the three price
properties the screen never looked at, over the names it has already passed.

### Three limits and one rank

```
filter   forward yield              >= min_yield        (2%)
filter   annualised volatility      <= max_volatility   (28%)
filter   worst peak-to-trough fall  <= max_drawdown     (40%)
rank     annualised price return, highest first
```

Limits on the three requirements and a plain rank on the bonus, rather than one
weighted score. A score needs weights nobody here can defend, and it hides which
constraint bound — which is the only part of an empty answer worth reading.

The yield floor is not decoration, and the first live run is why it exists.
Ranking by return with no floor, the module returned MSFT, MSI, V, WMT and NDAQ
at the top and a basket yielding **1.32%**: durable payers every one, and an
income book by no definition at all. "If it rises, that's good" is a tie-break
among names that already pay, not a reason to hold one that barely does.

A name whose yield could not be priced is cut by the floor rather than passed by
it — an unpriced name is not a name that pays enough, it is a name nobody
measured. Yields are therefore computed for the whole scored set *before* the
cut, not for the survivors after it: filtering on a column that does not exist
yet is the slice-and-call-it-a-ranking defect D3 already paid for once.

**Price only, dividends excluded.** `auto_adjust=False`, so `Close` is
split-adjusted and dividend-unadjusted — the basis `backtest.book_drawdown`
already uses, for the reason it documents: income spent as it arrives is not in
the account to cushion a fall.

**Ten years, not five.** A trailing five-year window on 2026-08 starts in 2021
and contains no crash, which scores every name as calm. Ten reaches back through
March 2020.

**The book is measured as one path**, not as the average of its names' falls.
Names do not all bottom on the same day, so the average of the parts is always
worse than the whole and would reject baskets that were fine. A test pins this
with two names that crash 50% at opposite ends of the window: each reads −50%,
the book reads −25%.

**Names that did not live through the whole window are dropped, not scored.** A
series starting after the crash has a shallow drawdown for a reason that has
nothing to do with the company, and scoring it would put exactly the wrong names
at the top.

### D5 result

`pytest tests/value` — 485 passed (16 new), `ruff check .` clean. Run against
the D4 store copy (726 names with cached dividend history, 153 passing D1) over
2016-08-25 → 2026-08-25:

| min yield | pass yield | too volatile | fell too far | basket |
|---|---|---|---|---|
| none | 153 | 101 | 33 | 15 names (the `--size` cap), **1.32%** yield, 16.3% vol, −30.2% worst, 14.6%/yr |
| 2% | 35 | 25 | 7 | 3 names (HD, ITW, LMT), **2.43%** yield, 19.4% vol, −35.8% worst, 9.1%/yr |
| 3% | 9 | 8 | 1 | **empty** |
| 4% | 4 | 4 | 0 | **empty** |

Four findings. The first is the one that decides whether the feature can do
what was asked of it; the second is the one most likely to be got wrong by
anyone tuning the knobs afterwards.

**Yield and price stability are anti-correlated across this universe.** Only 9
of the 153 durable payers yield 3% or more, and 8 of those 9 are too volatile to
hold under a 28% limit; at 4% it is 4 of 4. The names that pay well here are
banks, chemicals and hardware — the cyclicals whose price moves are the reason
they yield well. There is no corner of this list where the operator's income
target and their stability target are both met. That is a property of the
universe, not a tuning problem, and no combination of the four knobs finds one.

So the 5–10% portfolio yield remains out of reach for the same structural reason
D4 recorded, now with the mechanism named rather than inferred.

**Drawdown is the binding knob; volatility only looks like it.** By raw counts
the volatility filter cuts far more names than the drawdown filter — 25 against 7
at a 2% yield floor — which reads as though loosening it would open the list.
It does not. A second sweep at a 1.5% floor, holding everything else fixed:

| min yield | max vol | max drawdown | too volatile | fell too far | basket |
|---|---|---|---|---|---|
| 1.5% | 28% | 40% | 35 | 13 | 7 names, 2.00% yield, 17.3% vol, −33.8% worst, 10.5%/yr |
| 1.5% | **32%** | 40% | 18 | 30 | **the same 7 names**, identical basket |
| 1.5% | 28% | **45%** | 35 | 6 | 14 names, 1.92% yield, 17.6% vol, −34.3% worst, 11.3%/yr |
| 1.5% | **32%** | **45%** | 18 | 19 | 15 names (the `--size` cap, not the end of the list) |

Row 2 is the finding. Raising the volatility ceiling by four points moved 17
names out of one rejection column and straight into the other, and admitted
nobody: high volatility and deep drawdown are one property of one group of
names here, not two properties that can be traded against each other. Raising
the drawdown ceiling by five points doubled the basket instead.

So the earlier reading of the count — that volatility "does all the work" —
was backwards about which knob to turn. The count says which filter fires last,
not which one binds.

**The book absorbs falls the names do not.** In the 14-name basket NSC fell
44.7%, AVY 43.8% and CBSH 43.5% on their own, and the book fell 34.3% — half a
point deeper than the 7-name basket that excluded all three. Names admitted by a
looser drawdown limit did not deepen the book's own drawdown, because they did
not bottom on the same day. This is the reason the book is measured as one path
(above) rather than as an average of its names, stated in numbers.

**None of it moves the 5% constraint.** Every row of both tables lands on 14–17%
of capital, the same range D4 arrived at from cohorts rather than from a single
window. The knobs trade names against income against return; the share of
capital that would have held the whole portfolio inside 5% is not among the
things they change. Arithmetic on one past window, not a bound on the next one.

### What D5 does not do

- **No weights.** Equal weight and a count. Once two names clear the same
  limits, this module has no basis for preferring one, and inventing one would
  be the weighted score the design just rejected.
- **No position.** Same rule as every other surface: it names candidates and
  writes nothing.
- **No forward test of its own.** Selection is on a past window, and the window
  contains exactly one crash. D6 below replays the two price limits forward
  against a random-selection baseline; until that existed, this bullet read as a
  standing objection rather than a pointer.


## D6 — the forward test of the price filters

Deliverable: `dividend/forward.py`. Tests: `tests/value/test_dividend_forward.py`.

D5 selects names whose price sat still over a trailing window, which is a
description of what already happened. D4 is the precedent for what a description
is worth before it is replayed: the dividend criteria were reasoned too, and
reasoning is what phases 4b and 6 each established is not evidence.

```bash
python -m tradingagents.value.dividend.forward --start 2012 --end 2020
```

### The question, and why the baseline is random selection

    a book chosen on trailing volatility and drawdown as of date X — does it
    fall less over the next five years than a book of the same size drawn at
    random from the same pass list?

**Random selection, not the whole pass list.** Picking 15 names out of 100
changes a drawdown all by itself, in either direction, and a filter measured
against the 100-name book would be credited with an effect that concentration
produced. Phase 6 had to add its noise floor as a separate measurement after the
fact; here it is the baseline from the first line.

**The yield floor is held off.** It is a requirement the operator states, not a
claim about the future. Mixing it in would shrink both arms to test something
nobody asserted. On trial are the two price limits and nothing else.

Pre-registered, printed verbatim above every run, and reproduced in
`forward.CRITERION`:

> the price filters earn their place only if the 95% cluster-bootstrap CI for
> (filtered book's forward max drawdown − the median forward max drawdown of
> random books of the same size on the same date), pooled over cohort dates, is
> entirely above zero, at the configured limits, never at a best-of-grid cell.
> Forward return is descriptive and gates nothing.

The bootstrap resamples whole cohort dates rather than names, for the reason
`backtest.interval` documents: two names screened on the same morning share a
market.

### The point-in-time line, which is the whole risk

`trailing()` reads a price frame that also spans the forward window — it has to,
the same cache prices the outcome — so the slice at the cohort date is the only
thing standing between the decision and the answer. It is pinned by a pair:
a name that falls 60% *after* the cohort date must measure a drawdown of exactly
zero, and the control reads the same frame at a later as-of and finds the −60%.
A leak here would not fail, it would flatter.

The first draft of that test placed the crash by counting business days and put
it seven bars on the wrong side of the cohort date. The fixture now places
events by date.

### D6 result

`pytest tests/value` — 504 passed (19 new), `ruff check .` clean. Cohorts
2012–2020, five-year horizon, 200 random books per date, against the D4 store
copy:

```
from        to            pool  filtered   random   effect      all
2012-01-02  2017-01-02      95    -11.0%   -15.2%    +4.2%   -11.6%
2013-01-02  2018-01-02     109     -9.2%   -14.5%    +5.3%   -11.4%
2014-01-02  2019-01-02     102    -11.9%   -19.9%    +8.0%   -19.1%
2015-01-02  2020-01-02     103    -12.3%   -19.3%    +7.1%   -19.0%
2016-01-02  2021-01-02     102    -24.8%   -37.5%   +12.6%   -36.8%
2017-01-02  2022-01-02      95    -24.8%   -37.7%   +12.9%   -37.2%
2018-01-02  2023-01-02      98    -27.9%   -37.2%    +9.3%   -36.4%
2019-01-02  2024-01-02     122    -35.8%   -38.2%    +2.3%   -37.8%
2020-01-02  2025-01-02     135    -34.9%   -38.9%    +4.0%   -38.6%
```

**VERDICT: the price filters earn their place.** Effect +7.30%, CI
[+5.16%, +9.66%], positive on all nine dates. Trailing calm predicts forward
calm, and not by concentration — the baseline already holds fifteen names.

This is the first pass any gate in this module has returned, and it was
predicted to fail. The prediction is recorded because it is the only evidence
that the criterion was not reverse-engineered from the number: the reasoning was
that March 2020 took 30–40% off almost everything, so forward drawdown would be
dominated by whether the window contained a crash rather than by which names
were held. That reasoning was wrong about the magnitude of what the filter
avoids, and right about where it stops working — see the second caveat.

Four things the verdict does not say, none of them cancelled by the pass.

**It is bought with return.** The filtered book trails the random one by −6.3%
over the five-year window, about −1.2% a year. Same shape phase 6 recorded about
every intervention that has helped this strategy: they help by holding less
risk, not by picking better. Here it is falling less and earning less.

**The effect shrinks exactly where it is wanted most.** The 2019 and 2020
cohorts — entering shortly before February 2020 — return +2.3% and +4.0%,
against +12.9% for the 2017 cohort. When the crash arrives early in the holding
period the filter barely helps, and the filtered book still fell 35.8%. A
selection made on calm cannot outrun a market-wide repricing it enters into.

**Nine cohorts are not nine experiments.** Five-year windows stepped one year
apart mean the 2016 and 2017 cohorts share four years. The cluster bootstrap
resamples whole dates but cannot undo that overlap, so the interval is narrower
than the number of independent observations would support. D4 carries the same
property and the same caveat.

**This is almost certainly the low-volatility anomaly**, which has a long
published literature on equity markets. Recovering it is evidence the apparatus
is not broken, not evidence of an edge peculiar to this screen. It also does
nothing for the 5% constraint: the worst filtered cohort still fell 35.8%, and
the sizing arithmetic is unchanged.

### What D6 does not close

- **The yield conflict.** D5's first finding stands: yield and stability are
  anti-correlated here, and this run deliberately switched the yield floor off
  to isolate the price limits. A forward test of the three-limit basket is a
  different and smaller-armed measurement.
- **The knob levels.** The run is at the configured limits, once. No grid, and a
  best-of-grid cell would not have been reportable under the criterion anyway.
- **Anything about acting on it.** Same rule as every surface here: it names
  candidates and writes no position.

## D7 — the filing read on the names D5 chose

Deliverable: `dividend/brief.py`. Tests: `tests/value/test_dividend_brief.py`.

```bash
python -m tradingagents.value.dividend.brief --dry-run
python -m tradingagents.value.dividend.brief --tickers HD --budget 1.00
```

D1 reads the payout, D5 reads the price, and between them they do not read a
sentence of English. The operator's remaining question about a name that cleared
both is the one the statements cannot answer — is the moat eroding, does MD&A
hide an accounting problem, does a handful of customers carry the revenue — and
that is what tier 3 already asks for the business screen.

### Why this is a briefing and not a gate

Phase 6 is the reason the distinction is load-bearing rather than decorative: an
LLM veto at entry could not be told apart from a random one, and `verdict`
landed on `caution` for 31 of 44 picks. So nothing in this surface can add a
name, remove one, or reorder the basket. `select` has already run; its output is
what prints, with the read attached underneath, and `verdict` renders last
through the same `alerts.message.briefing` the dossier uses — the two surfaces
still cannot drift, because there is still one renderer.

A name the analyst dislikes stays on the list. The operator is the filter, which
is phase 7's answer and not a new one. What the read buys is the sentence the
operator would otherwise go find in a 200-page filing themselves.

### What it costs, and the three things bounding it

The read is the only paid call in this package. It runs over the **basket**, not
the pass list — 3 names, not 153; it is cached on the exact prompt like every
other tier-3 call; and it is charged against `Budget`, which fails closed.
`--dry-run` names the filings and spends nothing.

### The prompt seam, stated because it is the one dishonest-looking part

`value_analyst.SYSTEM_PROMPT` says the company "has already cleared a
thirteen-criterion numeric screen". For a dividend candidate that is false — it
cleared four payout criteria and two price limits. The prompt is a byte-stable
cache prefix, so editing it would invalidate every assessment already paid for
on both surfaces, and forking it would buy a second prompt to keep in step.

So the correction goes in the `## Numeric screen result` block instead, which
opens by naming which screen actually ran and then lists the four criteria with
their violation and no-data years. The instruction the prompt actually gives —
do not re-derive the numbers, judge the moat and the language — is correct
either way, and it is the part that steers the answer.

### The independence contract, widened on purpose

`tests/value/test_dividend.py` failed the moment `brief.py` existed, which is
the allowlist doing its job. Six entries were added: `analyst.value_analyst`,
`analyst.schemas`, `edgar.filings`, `edgar.client`, `llm.budget`,
`alerts.message`. Every one is read-only reuse of tier 3 rather than a second
copy of it — a second analyst would be a second set of phase 6's mistakes to
make. The arrow still points outward: nothing in `value/` imports `dividend/`,
and deleting the directory still deletes the feature.

### D7 result

`pytest tests/value` — 521 passed (17 new), `ruff check .` clean. First live run,
against the D4 store copy, default knobs (2% yield floor), 2026-08-25:

```
HD     yield 2.73%, vol 25%, worst -38%, +9.6%/yr    verdict caution, confidence medium
ITW    yield 2.19%, vol 24%, worst -38%, +9.1%/yr    verdict proceed, confidence low
LMT    yield 2.37%, vol 24%, worst -37%, +8.5%/yr    verdict caution, confidence low
```

Three names, three filings, **$0.1125** total — 36,897 prompt and 16,119
completion tokens against a $2.00 run cap.

The verdict spread is 2 caution / 1 proceed on n=3, which is not evidence about
anything and is not offered as any. Two things in the run are worth recording.

**The extraction warnings fired on all three, and they were right.** LMT came
back with no Item 7 at all and a Business section of 4,384 characters against
88,265 for the largest section; HD's Risk Factors slot contained MD&A text; ITW's
MD&A opened late and closed early. The analyst said so itself — `evidence gaps`
on all three name the missing sections, and confidence is `low` on the two worst.
This is the `suspect` machinery from phase 6 working as designed on a new caller,
and it is also a standing defect in the extractor that D7 inherits rather than
fixes.

**The reads are specific, which is the part phase 6 said was worth paying for.**
LMT's is a single-customer risk the numeric screen cannot see at all — 72% of
revenue from the US government, 27% from one programme, and an executive order
that could restrict the dividend itself. HD's is a payout-relevant one: dividend
growth decelerating 2.2% to 1.3%, buybacks paused to pay down SRS/GMS debt, ROIC
36.7% to 25.7% in two years. Neither is in the four criteria, and both bear
directly on whether the payout survives ten more years.

### What D7 does not do

- **It does not measure whether the read helps.** Phase 6 measured the veto and
  found it unusable; nothing here re-measures anything, because a briefing that
  changes no selection has no outcome to score. The claim is only that the
  sentences are specific and cheap.
- **It does not fix the extractor.** Three of three filings came back suspect.
  The warnings travel to the operator, which is the minimum, not the repair.
- **It does not write a position.** Same rule as every surface here. Recording
  what was actually done with a name is still `decisions record`.
