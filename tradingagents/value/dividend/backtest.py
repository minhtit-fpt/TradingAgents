"""Replay the dividend screen over history: does passing it predict not being cut?

    python -m tradingagents.value.dividend.backtest --start 2012 --end 2019
    python -m tradingagents.value.dividend.backtest --no-prices --bootstrap 0

This is the thing D1 through D3 never had. The criteria levels are reasoned —
a decade of payments, no cut, payout inside earnings, free cash flow covering
the cheque — and reasoning is what phases 4b and 6 each established is not the
same as evidence. So the question here is deliberately narrow and pre-registered:

    a name that clears the screen on date X, does it still hold its dividend
    through X+horizon, more often than a name that failed it on the same date?

That is the claim the screen actually makes. It is answerable **offline**, from
the dividend cache alone, and it is answerable before any question about return.
A screen that does not separate the cutters is not worth pricing.

Return against the benchmark is reported second and gates nothing. Equal-weighted
buy-and-hold cohorts with no costs and no rebalancing are not the book anyone
would run; ``backtest/numeric.py`` is where a real simulation lives, and pointing
this feature at it would mean re-screening the business criteria too. What the
cohort return is good for is catching the opposite failure — a screen that avoids
cuts by only ever naming companies going nowhere.

The forward window is look-ahead **on purpose**: it is the outcome being scored,
not an input to the decision. Everything on the decision side of that line —
the dividend history, the 10-K facts — goes through the same point-in-time
filters the live screen uses, and ``test_dividend_backtest.py`` pins that.
"""

import argparse
import random
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..backtest import prices, stats
from ..config import BACKTEST_BENCHMARK
from ..store import db
from . import config, history, store
from .criteria import evaluate

# Pre-registered before the run, printed verbatim, and not to be edited after a
# result is known — the discipline phase 4b step 3 had to invent the hard way.
CRITERION = (
    "pre-registered pass criterion: the screen earns its levels only if the 95% "
    "bootstrap CI for (cut rate among names it rejected − cut rate among names it "
    "passed) is entirely above zero, at the configured payout_max, never at a "
    "best-of-grid cell. The payout grid below is a sensitivity display. Cohort "
    "return is descriptive and gates nothing."
)

# The knob nobody has measured. 0.60 is the default; a screen built for income
# might want 0.70, and that is the disagreement this sweep exists to settle.
DEFAULT_PAYOUT_GRID = (0.50, 0.60, 0.70)

DEFAULT_HORIZON = 5
CONFIDENCE = 0.95

# The loss the operator says the whole portfolio must stay inside. It is not a
# knob the screen can honour -- no screen can -- so it is used one way only: to
# turn the worst drawdown actually measured into the share of capital that would
# have kept inside it. Arithmetic on a past window, not a promise about the next.
PORTFOLIO_LOSS_FLOOR = 0.05

# Cohort dates land on the 2nd of January, which is a market holiday about as
# often as not. A price frame that starts *on* the cohort date therefore has no
# bar at or before it, and every name in that cohort drops out of the return
# figure while the run reports no error at all — the first live run lost an
# entire cohort this way. Fetch from before the first cohort instead.
ENTRY_LOOKBACK_DAYS = 30

# Reading the dividend cache with no point-in-time cutoff at all. Only ever used
# for the forward window, which is the outcome; the screen side passes a real
# as-of date and is asserted to.
_NO_CUTOFF = "9999-12-31"


@dataclass(frozen=True)
class NameResult:
    """One name, on one cohort date: what the screen said and what happened next."""

    ticker: str
    passed: bool
    cut: bool
    total_return: float | None = None


@dataclass(frozen=True)
class Cohort:
    """Every name screenable on one date, plus the benchmark over the same window."""

    as_of: str
    until: str
    results: tuple[NameResult, ...]
    benchmark_return: float | None = None
    unpriced: int = 0
    max_drawdown: float | None = None

    @property
    def passers(self) -> tuple[NameResult, ...]:
        return tuple(r for r in self.results if r.passed)

    @property
    def rejects(self) -> tuple[NameResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    @property
    def usable(self) -> bool:
        """Both arms populated. A date where nothing passed carries no comparison."""
        return bool(self.passers) and bool(self.rejects)


def cohort_dates(start_year: int, end_year: int) -> tuple[str, ...]:
    """One screen per year, on the first business-ish day. Inclusive of both ends."""
    return tuple(f"{year}-01-02" for year in range(start_year, end_year + 1))


def horizon_end(as_of: str, horizon: int) -> str:
    """The date ``horizon`` years after ``as_of``, on the same day of the year."""
    start = date.fromisoformat(as_of)
    return start.replace(year=start.year + horizon).isoformat()


def price_history(dates: Sequence[str], horizon: int, **kwargs) -> prices.History:
    """A price cache spanning the run, with enough lead-in to price the first entry."""
    start = date.fromisoformat(dates[0]) - timedelta(days=ENTRY_LOOKBACK_DAYS)
    return prices.History(start.isoformat(), horizon_end(dates[-1], horizon), **kwargs)


def screenable(conn: sqlite3.Connection) -> list[str]:
    """Names with both a cached dividend history and 10-K facts. No network."""
    return sorted(set(store.cached(conn)) & set(db.tickers(conn)))


def verdicts(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    *,
    years: int,
    payout_grid: Sequence[float],
) -> dict[float, bool] | None:
    """Pass/fail at every payout limit, from **one** read of the inputs.

    Screening the universe once per grid cell would triple the SQLite work for
    three answers that differ in a single comparison. ``None`` when the name
    cannot be screened at all on this date — a name with no facts yet is not a
    name that failed, and folding it into the reject arm would credit the screen
    with rejecting companies it never saw.
    """
    window = history.window_years(as_of, years)
    dps = history.annual(conn, ticker, as_of, window)
    if not any(dps.values()):
        return None
    financials = db.series_as_of(conn, ticker, as_of, years)
    if not financials:
        return None
    return {
        limit: evaluate(dps, financials, years_required=years, payout_max=limit).passed
        for limit in payout_grid
    }


def was_cut(conn: sqlite3.Connection, ticker: str, as_of: str, horizon: int) -> bool | None:
    """Did the per-share dividend fall in any year of the forward window?

    The baseline is the last year the screen itself saw, so a board that cuts in
    the very first forward year is caught rather than treated as a new record
    starting low. A year with no payment is a fall to zero and counts: a payer
    that simply stops has done the thing the screen exists to avoid, and calling
    that "no data" would score the worst outcome as an absence of one.

    ``None`` only when the window has not fully elapsed — the caller decides
    whether the cohort is old enough, and nothing is guessed about years that
    have not happened.
    """
    baseline = date.fromisoformat(as_of).year - 1
    window = tuple(range(baseline, baseline + horizon + 1))
    dps = history.annual(conn, ticker, _NO_CUTOFF, window)
    if not dps.get(baseline):
        # Nothing to be cut from. The screen cannot have passed such a name, and
        # scoring it in the reject arm would be scoring an unevaluable year.
        return None
    amounts = [dps[year] for year in window]
    return any(later < earlier for earlier, later in zip(amounts, amounts[1:], strict=False))


def total_return(
    conn: sqlite3.Connection,
    hist: prices.History,
    ticker: str,
    as_of: str,
    until: str,
) -> float | None:
    """Price change plus dividends received, over one holding window.

    ``Close``, not ``market.AS_TRADED``: this is the only place in the module
    that puts a price and a figure from the ``dividends`` table over the same
    line, and the two are on the same basis exactly when the price is the
    split-adjusted one. ``AS_TRADED`` undoes the splits on the price and would
    read a 4-for-1 as a 300% loss against an unchanged dividend.

    Dividends are collected, not reinvested. The benchmark is measured the same
    way, so the comparison is fair even though neither leg is a total-return
    index.
    """
    try:
        entry = _close(hist, ticker, as_of)
        exit_price = _close(hist, ticker, until)
    except prices.PriceError:
        return None
    if entry <= 0:
        return None
    collected = sum(
        float(row["dps"])
        for row in store.as_of(conn, ticker, until)
        if row["ex_date"] > as_of
    )
    return (exit_price + collected - entry) / entry


def _close(hist: prices.History, ticker: str, as_of: str) -> float:
    """Last split-adjusted close at or before ``as_of``. Raises rather than guessing."""
    frame = hist.frame(ticker)
    rows = frame[frame.index.date <= date.fromisoformat(as_of)]
    if rows.empty:
        raise prices.PriceError(f"no price for {ticker} at or before {as_of}")
    return float(rows["Close"].iloc[-1])


def book_drawdown(
    hist: prices.History,
    tickers: Sequence[str],
    as_of: str,
    until: str,
) -> float | None:
    """Worst peak-to-trough fall of an equal-weight book of ``tickers``, price only.

    Price only on purpose, and it is the one place in this file where excluding
    the dividends is the accurate choice rather than the lazy one: a book held to
    pay living expenses spends the cash as it arrives, so it is not in the
    account to cushion a fall. Counting it would flatter exactly the number this
    exists to give.

    Equal weight at entry, held to ``until``, never rebalanced -- the same book
    the return line describes. Names with no price series drop out rather than
    being scored as flat, which is the survivorship hole the report already
    prints and not a new one.
    """
    import pandas as pd

    paths = []
    for ticker in tickers:
        try:
            entry = _close(hist, ticker, as_of)
            frame = hist.frame(ticker)
        except prices.PriceError:
            continue
        rows = frame[
            (frame.index.date > date.fromisoformat(as_of))
            & (frame.index.date <= date.fromisoformat(until))
        ]
        if rows.empty or entry <= 0:
            continue
        paths.append(rows["Close"] / entry)
    if not paths:
        return None
    equity = pd.concat(paths, axis=1).ffill().mean(axis=1).dropna()
    if equity.empty:
        return None
    return float((equity / equity.cummax() - 1.0).min())


def worst_drawdown(cohorts: Sequence[Cohort]) -> float | None:
    """The deepest fall any cohort's book took. The worst case measured, not the mean."""
    falls = [c.max_drawdown for c in cohorts if c.max_drawdown is not None]
    return min(falls) if falls else None


def sizing_for_floor(drawdown: float, floor: float = PORTFOLIO_LOSS_FLOOR) -> float:
    """Share of capital in these names that would have kept the book inside ``floor``.

    ``floor`` and ``drawdown`` are both magnitudes of loss. The remainder is
    assumed to hold its value, which is true of cash and of nothing else.
    """
    if drawdown >= 0.0:
        return 1.0
    return min(1.0, floor / abs(drawdown))


class BenchmarkError(RuntimeError):
    """The benchmark cannot be measured on the same basis as the names. Never fudged."""


def check_benchmark(conn: sqlite3.Connection, benchmark: str, as_of: str) -> None:
    """Refuse to compare a total return against a price-only one.

    ``total_return`` reads its dividends from the ``dividends`` table, and an
    uncached name yields an empty list — indistinguishable, at that layer, from a
    name that paid nothing. The benchmark is the one place where that ambiguity
    is guaranteed to bite: an index ETF has no 10-K facts, so the cache warmer
    never reaches it, and the first live run duly measured the names with their
    dividends and SPY without. The whole excess was overstated by the benchmark's
    own yield.

    So this is a hard stop rather than a caveat. A quietly price-only benchmark
    is the "confidently wrong figure" the module's error rule exists to prevent.
    """
    if not store.as_of(conn, benchmark, as_of):
        raise BenchmarkError(
            f"no cached dividend history for the benchmark {benchmark}: its return "
            "would be price-only while every name it is compared against includes "
            "dividends. Cache it first, or run with --no-prices."
        )


def run(
    conn: sqlite3.Connection,
    dates: Sequence[str],
    *,
    tickers: Sequence[str] | None = None,
    horizon: int = DEFAULT_HORIZON,
    years: int = config.HISTORY_YEARS,
    payout_grid: Sequence[float] = DEFAULT_PAYOUT_GRID,
    payout_max: float = config.PAYOUT_MAX,
    hist: prices.History | None = None,
    benchmark: str = BACKTEST_BENCHMARK,
    today: str | None = None,
) -> dict[float, list[Cohort]]:
    """One list of cohorts per payout limit. Every cell sees the same names."""
    limits = tuple(dict.fromkeys((*payout_grid, payout_max)))
    universe = list(tickers) if tickers is not None else screenable(conn)
    cutoff = today or date.today().isoformat()
    if hist is not None and dates:
        check_benchmark(conn, benchmark, horizon_end(dates[-1], horizon))
    out: dict[float, list[Cohort]] = {limit: [] for limit in limits}

    for as_of in dates:
        until = horizon_end(as_of, horizon)
        if until > cutoff:
            # The forward window has not elapsed. Truncating it to today would
            # score a three-year outcome as a five-year one.
            continue

        rows: dict[float, list[NameResult]] = {limit: [] for limit in limits}
        unpriced = 0
        for ticker in universe:
            passed = verdicts(conn, ticker, as_of, years=years, payout_grid=limits)
            if passed is None:
                continue
            cut = was_cut(conn, ticker, as_of, horizon)
            if cut is None:
                continue
            forward = (
                total_return(conn, hist, ticker, as_of, until) if hist is not None else None
            )
            if hist is not None and forward is None:
                unpriced += 1
            for limit in limits:
                rows[limit].append(NameResult(ticker, passed[limit], cut, forward))

        bench = (
            total_return(conn, hist, benchmark, as_of, until) if hist is not None else None
        )
        for limit in limits:
            results = tuple(rows[limit])
            fall = (
                book_drawdown(
                    hist, [r.ticker for r in results if r.passed], as_of, until
                )
                if hist is not None
                else None
            )
            out[limit].append(Cohort(as_of, until, results, bench, unpriced, fall))
    return out


def cut_gap(cohorts: Sequence[Cohort]) -> float | None:
    """Pooled reject cut rate minus pooled pass cut rate. Positive means the screen works."""
    passers = [r for c in cohorts for r in c.passers]
    rejects = [r for c in cohorts for r in c.rejects]
    if not passers or not rejects:
        return None
    return _rate(rejects) - _rate(passers)


def excess_return(cohorts: Sequence[Cohort]) -> float | None:
    """Mean cohort return of the passers, less the benchmark's over the same window."""
    gaps = []
    for cohort in cohorts:
        priced = [r.total_return for r in cohort.passers if r.total_return is not None]
        if priced and cohort.benchmark_return is not None:
            gaps.append(sum(priced) / len(priced) - cohort.benchmark_return)
    return sum(gaps) / len(gaps) if gaps else None


def _rate(results: Sequence[NameResult]) -> float:
    return sum(1 for r in results if r.cut) / len(results)


def interval(
    cohorts: Sequence[Cohort],
    measure: Callable[[Sequence[Cohort]], float | None],
    *,
    samples: int = stats.BOOTSTRAP_SAMPLES,
    seed: int = 0,
    confidence: float = CONFIDENCE,
) -> stats.Interval | None:
    """Cluster bootstrap over cohort dates, resampling whole dates with replacement.

    Whole dates, not individual names: two payers screened on the same morning
    share a market, and resampling them independently would treat one correlated
    decade as a few hundred trials. That is the phase-4 error — reading a verdict
    off a sample that cannot resolve it — and the cost of avoiding it is that the
    interval is wide. A wide honest interval is the finding.

    ``None`` when there are too few usable cohorts to resample.
    """
    usable = [c for c in cohorts if c.usable]
    point = measure(usable)
    if len(usable) < 4 or samples < 2 or point is None:
        return None

    rng = random.Random(seed)
    draws = []
    for _ in range(samples):
        picks = [usable[rng.randrange(len(usable))] for _ in usable]
        drawn = measure(picks)
        if drawn is not None:
            draws.append(drawn)
    if not draws:
        return None

    tail = (1 - confidence) / 2
    return stats.Interval(
        point=point, low=_percentile(draws, tail), high=_percentile(draws, 1 - tail)
    )


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    index = int(round(q * (len(ordered) - 1)))
    return ordered[min(max(index, 0), len(ordered) - 1)]


def render(
    grid: dict[float, list[Cohort]],
    *,
    payout_max: float,
    horizon: int,
    benchmark: str,
    samples: int = stats.BOOTSTRAP_SAMPLES,
    seed: int = 0,
    floor: float = PORTFOLIO_LOSS_FLOOR,
) -> list[str]:
    """The report: the verdict at the configured level, then the grid around it."""
    cohorts = grid[payout_max]
    usable = [c for c in cohorts if c.usable]
    lines = [CRITERION, ""]

    if not usable:
        return lines + [
            "no cohort had names on both sides of the screen — no comparison, and "
            "therefore no verdict. Widen the window or warm the dividend cache."
        ]

    names = sum(len(c.results) for c in usable)
    lines.append(
        f"{len(usable)} cohorts, {names} name-dates, {horizon}-year forward window, "
        f"payout_max {payout_max:.2f}"
    )
    for cohort in usable:
        lines.append(
            f"  {cohort.as_of} -> {cohort.until}  "
            f"pass {len(cohort.passers):>3} (cut {_rate(cohort.passers):>5.0%})  "
            f"reject {len(cohort.rejects):>4} (cut {_rate(cohort.rejects):>5.0%})"
            + (f"  unpriced {cohort.unpriced}" if cohort.unpriced else "")
        )

    gap = interval(cohorts, cut_gap, samples=samples, seed=seed)
    lines.append("")
    if gap is None:
        lines.append(
            f"too few usable cohorts ({len(usable)}) to resample — no interval, and "
            "therefore no verdict."
        )
    else:
        lines.append(f"cut rate, reject arm less pass arm: {gap.text()}")
        passes = gap.low > 0.0
        lines.append("")
        lines.append(
            f"VERDICT: {'pass' if passes else 'fail'} against the pre-registered criterion"
        )
        if not passes:
            lines.append(
                f"  - the CI ({gap.text()}) does not sit entirely above zero: the screen "
                "is not separated from picking a decade-long payer at random"
            )

    lines.extend(_returns_block(cohorts, benchmark, samples, seed))
    lines.extend(_drawdown_block(cohorts, floor))
    lines.extend(_grid_block(grid, payout_max, samples, seed))
    return lines


def _returns_block(
    cohorts: Sequence[Cohort], benchmark: str, samples: int, seed: int
) -> list[str]:
    """Descriptive, and labelled as such every time it is printed."""
    excess = interval(cohorts, excess_return, samples=samples, seed=seed)
    unpriced = sum(c.unpriced for c in cohorts)
    lines = ["", "descriptive, gates nothing — equal-weighted buy-and-hold cohorts, "
             "no costs, no rebalancing, dividends collected and not reinvested. The "
             "mean of individual multi-year returns runs above a cap-weighted index "
             "mechanically, so read the sign, not the size:"]
    if excess is None:
        lines.append(f"  cohort return vs {benchmark}: not priced (run without --no-prices)")
    else:
        lines.append(f"  cohort return vs {benchmark}, per holding window: {excess.text()}")
    if unpriced:
        lines.append(
            f"  {unpriced} name-dates had no price series and are absent from the "
            "return figure only — the survivorship hole, unrepairable on free data"
        )
    return lines


def _drawdown_block(cohorts: Sequence[Cohort], floor: float) -> list[str]:
    """What the book would have fallen to, and what that implies for sizing.

    Separate from the return block because it answers a different question. The
    return line asks whether these names went anywhere; this one asks how far
    down the road went, which is the only honest answer to "keep the portfolio
    inside 5%" -- a screen cannot bound a drawdown, an allocation can.
    """
    measured = [c for c in cohorts if c.max_drawdown is not None]
    if not measured:
        return ["", "drawdown: not priced (run without --no-prices)"]

    lines = ["", "worst peak-to-trough fall of an equal-weight book of the names that "
             "passed, held to the end of the window, price only -- dividends are spent, "
             "not reinvested, so they do not cushion it:"]
    for cohort in measured:
        lines.append(f"  {cohort.as_of} -> {cohort.until}  {cohort.max_drawdown:>7.1%}")

    worst = worst_drawdown(measured)
    share = sizing_for_floor(worst, floor)
    lines.append("")
    lines.append(f"worst across cohorts: {worst:.1%}")
    lines.append(
        f"holding the whole portfolio inside {floor:.0%} would have needed at most "
        f"{share:.0%} of capital in these names, the rest in something that does not "
        "fall. Arithmetic on the worst window measured -- not a recommendation, and "
        "not a bound on the next one, which can be worse than anything in this sample."
    )
    return lines


def _grid_block(
    grid: dict[float, list[Cohort]], configured: float, samples: int, seed: int
) -> list[str]:
    """The payout sweep. A display, never the place a verdict is read from."""
    lines = ["", "payout_max sensitivity — a display, not evidence. Reading the best "
             "cell as the answer is the error the criterion above exists to prevent:"]
    for limit in sorted(grid):
        cohorts = grid[limit]
        passers = sum(len(c.passers) for c in cohorts if c.usable)
        gap = interval(cohorts, cut_gap, samples=samples, seed=seed)
        mark = " <- configured" if limit == configured else ""
        text = gap.text() if gap else "no interval"
        lines.append(f"  {limit:.2f}: {passers:>4} passes, cut-rate gap {text}{mark}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="override the store path")
    parser.add_argument("--start", type=int, default=2012, help="first cohort year")
    parser.add_argument("--end", type=int, default=2019, help="last cohort year")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                        help="forward years the outcome is measured over")
    parser.add_argument("--years", type=int, default=config.HISTORY_YEARS)
    parser.add_argument("--payout-max", type=float, default=config.PAYOUT_MAX,
                        help="the level the verdict is read at")
    parser.add_argument("--benchmark", default=BACKTEST_BENCHMARK)
    parser.add_argument("--no-prices", action="store_true",
                        help="skip the return block entirely; the verdict needs no price")
    parser.add_argument("--bootstrap", type=int, default=stats.BOOTSTRAP_SAMPLES,
                        help="resamples; 0 disables the interval and the verdict with it")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--loss-floor", type=float, default=PORTFOLIO_LOSS_FLOOR,
                        help="the loss the whole portfolio must stay inside; the "
                             "drawdown block reports the sizing that would have held it")
    args = parser.parse_args(argv)

    dates = cohort_dates(args.start, args.end)
    if not dates:
        print("--start must not be after --end", file=sys.stderr)
        return 2

    conn = store.connect(args.db)
    try:
        universe = screenable(conn)
        if not universe:
            print("no name has both 10-K facts and a cached dividend history; "
                  "run the dividend screen first", file=sys.stderr)
            return 2

        hist = None
        if not args.no_prices:
            hist = price_history(dates, args.horizon)
            # The benchmark carries no 10-K facts, so no cache warmer reaches it.
            # Fetching it here is one call, and the alternative is the stop in
            # ``check_benchmark``.
            try:
                history.refresh(conn, args.benchmark)
            except history.DividendError as exc:
                print(f"benchmark dividend history unavailable: {exc}", file=sys.stderr)
                return 1

        grid = run(
            conn, dates,
            tickers=universe,
            horizon=args.horizon,
            years=args.years,
            payout_max=args.payout_max,
            hist=hist,
            benchmark=args.benchmark,
        )
        for line in render(
            grid,
            payout_max=args.payout_max,
            horizon=args.horizon,
            benchmark=args.benchmark,
            samples=args.bootstrap,
            seed=args.seed,
            floor=args.loss_floor,
        ):
            print(line)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
