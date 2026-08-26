"""D6 -- do the price filters predict anything, or do they only describe the past?

    python -m tradingagents.value.dividend.forward --start 2012 --end 2020
    python -m tradingagents.value.dividend.forward --reps 400 --size 15

D5 selects names whose price sat still over a trailing window. That is a
description of what already happened, and D4 is the precedent for what a
description is worth before it is replayed: the dividend criteria were reasoned
too, and reasoning is what phases 4b and 6 each established is not evidence.

So the question here is pre-registered and deliberately narrow:

    a book chosen on trailing volatility and drawdown as of date X -- does it
    fall less over the next N years than a book of the same size drawn at
    random from the same pass list?

**The baseline is random selection, not the whole pass list.** Picking 15 names
out of 150 changes a drawdown all by itself, in either direction, and a filter
compared against the 150-name book would be credited with an effect that
concentration produced. Phase 6 had to learn this as a separate noise-floor
measurement after the fact; here it is the baseline from the start.

The yield floor is held **off**. It is a requirement the operator states, not a
claim about the future, and mixing it in would shrink both arms to test something
nobody asserted. What is on trial is the two price limits and nothing else.

The forward window is look-ahead **on purpose** -- it is the outcome being
scored. Everything on the decision side goes through the same point-in-time
filters the live screen uses: the dividend history is read as of the cohort
date, the 10-K facts are read as of it, and ``trailing()`` slices the price frame
to bars at or before it. ``test_dividend_forward.py`` pins that last one, because
it is the only place in this module where a price frame that already holds the
answer is in scope at all.
"""

import argparse
import random
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..backtest import prices, stats
from ..store import db
from . import backtest, config, history, stability
from .criteria import evaluate

# Pre-registered before the run, printed verbatim, and not to be edited once a
# result is known -- the discipline phase 4b step 3 had to invent the hard way.
CRITERION = (
    "pre-registered pass criterion: the price filters earn their place only if the "
    "95% cluster-bootstrap CI for (filtered book's forward max drawdown - the median "
    "forward max drawdown of random books of the same size on the same date), pooled "
    "over cohort dates, is entirely above zero, at the configured limits, never at a "
    "best-of-grid cell. Positive means the filtered book fell less. Forward return is "
    "descriptive and gates nothing."
)

DEFAULT_HORIZON = 5
DEFAULT_REPS = 200
CONFIDENCE = 0.95

# Same reason as ``backtest.ENTRY_LOOKBACK_DAYS``: a cohort date landing on a
# market holiday has no bar *at* it, and the trailing window needs a lead-in
# anyway. Fetching from before the first trailing window covers both.
FRAME_LEAD_DAYS = 45


@dataclass(frozen=True)
class Cohort:
    """One screen date: what the filter chose, and what the next N years did to it."""

    as_of: str
    until: str
    universe: int
    chosen: tuple[str, ...]
    filtered_drawdown: float | None = None
    random_drawdown: float | None = None
    unfiltered_drawdown: float | None = None
    filtered_return: float | None = None
    random_return: float | None = None

    @property
    def effect(self) -> float | None:
        """Filtered minus random. Both are negative; positive means it fell less."""
        if self.filtered_drawdown is None or self.random_drawdown is None:
            return None
        return self.filtered_drawdown - self.random_drawdown

    @property
    def usable(self) -> bool:
        """A date carries a comparison only when both books could be priced.

        ``backtest.interval`` reads this. A cohort where the filter chose nothing,
        or where no price series survived, is dropped rather than scored as a
        zero effect -- a date with no measurement is not a date with no effect.
        """
        return self.effect is not None


def trailing(
    hist: prices.History, ticker: str, as_of: str, years: int
) -> stability.Stability | None:
    """``stability.measure`` over bars **at or before** ``as_of``. The whole point-in-time line.

    The frame this reads spans the forward window too -- it has to, the same
    cache prices the outcome -- so the slice here is the only thing standing
    between the decision and the answer. Everything else in this module is
    downstream of it being right.
    """
    try:
        frame = hist.frame(ticker)
    except prices.PriceError:
        return None
    start = date.fromisoformat(as_of) - timedelta(days=round(365.25 * years))
    rows = frame[(frame.index.date >= start) & (frame.index.date <= date.fromisoformat(as_of))]
    if rows.empty:
        return None
    return stability.measure(ticker, rows["Close"].dropna(), start.isoformat())


def passes(conn: sqlite3.Connection, ticker: str, as_of: str, years: int) -> bool:
    """The D1 dividend screen as of ``as_of``. A name with no inputs is not a pass."""
    window = history.window_years(as_of, years)
    dps = history.annual(conn, ticker, as_of, window)
    if not any(dps.values()):
        return False
    financials = db.series_as_of(conn, ticker, as_of, years)
    if not financials:
        return False
    return evaluate(dps, financials, years_required=years).passed


def book_return(hist: prices.History, tickers: Sequence[str], as_of: str, until: str) -> float | None:
    """Equal-weight price return, entry to exit, never rebalanced. Dividends excluded.

    Same basis and same exclusion as ``backtest.book_drawdown``, which this sits
    beside: the drawdown is the number on trial and the return is here so that a
    filter cannot win by holding nothing that moves.
    """
    legs = []
    for ticker in tickers:
        try:
            entry = backtest._close(hist, ticker, as_of)
            exit_price = backtest._close(hist, ticker, until)
        except prices.PriceError:
            continue
        if entry > 0:
            legs.append(exit_price / entry - 1.0)
    return sum(legs) / len(legs) if legs else None


def cohort(
    conn: sqlite3.Connection,
    hist: prices.History,
    as_of: str,
    *,
    horizon: int = DEFAULT_HORIZON,
    years: int = config.HISTORY_YEARS,
    stability_years: int = config.STABILITY_YEARS,
    max_volatility: float = config.MAX_VOLATILITY,
    max_drawdown: float = config.MAX_DRAWDOWN,
    size: int = config.BASKET_SIZE,
    reps: int = DEFAULT_REPS,
    seed: int = 0,
) -> Cohort:
    """Screen, filter, and price both books plus the random baseline, on one date."""
    until = backtest.horizon_end(as_of, horizon)
    scored = [
        row
        for row in (
            trailing(hist, ticker, as_of, stability_years)
            for ticker in backtest.screenable(conn)
            if passes(conn, ticker, as_of, years)
        )
        if row is not None
    ]
    universe = [row.ticker for row in scored]

    # Yields forced to zero against a zero floor: the yield filter is switched
    # off here on purpose (see the module docstring), and saying so in the call
    # is clearer than a default that happens to be permissive.
    chosen, _ = stability.select(
        scored,
        dict.fromkeys(universe, 0.0),
        min_yield=0.0,
        max_volatility=max_volatility,
        max_drawdown=max_drawdown,
        size=size,
    )
    picks = tuple(row.ticker for row in chosen)
    if not picks or len(universe) <= len(picks):
        # Nothing chosen, or the filter kept everything -- either way there is no
        # contrast to draw against a random subset of the same size.
        return Cohort(as_of=as_of, until=until, universe=len(universe), chosen=picks)

    rng = random.Random(f"{seed}:{as_of}")
    draws = [
        drawn
        for drawn in (
            backtest.book_drawdown(hist, rng.sample(universe, len(picks)), as_of, until)
            for _ in range(reps)
        )
        if drawn is not None
    ]
    returns = [
        drawn
        for drawn in (
            book_return(hist, rng.sample(universe, len(picks)), as_of, until)
            for _ in range(reps)
        )
        if drawn is not None
    ]

    return Cohort(
        as_of=as_of,
        until=until,
        universe=len(universe),
        chosen=picks,
        filtered_drawdown=backtest.book_drawdown(hist, picks, as_of, until),
        random_drawdown=_median(draws),
        unfiltered_drawdown=backtest.book_drawdown(hist, universe, as_of, until),
        filtered_return=book_return(hist, picks, as_of, until),
        random_return=_median(returns),
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def mean_effect(cohorts: Sequence[Cohort]) -> float | None:
    """Mean of the per-date effects. The measure ``backtest.interval`` resamples."""
    effects = [c.effect for c in cohorts if c.effect is not None]
    return sum(effects) / len(effects) if effects else None


def verdict(effect: stats.Interval | None) -> tuple[bool, str]:
    """Apply ``CRITERION``. One sentence, and it says which half failed."""
    if effect is None:
        return False, "not enough usable cohort dates to resample — no verdict"
    if not effect.excludes_zero:
        return False, (
            f"the CI for the drawdown effect ({effect.text()}) contains zero — "
            "trailing calm is not separated from picking at random"
        )
    if effect.low <= 0:
        return False, (
            f"the CI for the drawdown effect ({effect.text()}) is below zero — "
            "the filtered book fell further than a random one of the same size"
        )
    return True, f"the filtered book fell less than random selection ({effect.text()})"


def render(cohorts: Sequence[Cohort], effect: stats.Interval | None, *, size: int) -> list[str]:
    lines = [CRITERION, "", f"FORWARD TEST  filter picks {size}, baseline picks {size} at random", ""]
    lines.append(f"{'from':<12}{'to':<12}{'pool':>6}{'filtered':>10}{'random':>9}{'effect':>9}{'all':>9}")
    for c in cohorts:
        if c.effect is None:
            lines.append(f"{c.as_of:<12}{c.until:<12}{c.universe:>6}{'  no comparison':>37}")
            continue
        lines.append(
            f"{c.as_of:<12}{c.until:<12}{c.universe:>6}{c.filtered_drawdown:>10.1%}"
            f"{c.random_drawdown:>9.1%}{c.effect:>+9.1%}{c.unfiltered_drawdown:>9.1%}"
        )

    usable = [c for c in cohorts if c.usable]
    lines.append("")
    if usable:
        gains = [c.filtered_return - c.random_return for c in usable
                 if c.filtered_return is not None and c.random_return is not None]
        if gains:
            lines.append(
                f"forward return, filtered minus random: {sum(gains) / len(gains):>+.1%} "
                "mean over dates (descriptive, gates nothing)"
            )
    passed, why = verdict(effect)
    lines.append("")
    lines.append(f"VERDICT: {'the price filters earn their place' if passed else 'fail'} — {why}")
    return lines


def run(
    conn: sqlite3.Connection,
    *,
    start_year: int,
    end_year: int,
    horizon: int = DEFAULT_HORIZON,
    years: int = config.HISTORY_YEARS,
    stability_years: int = config.STABILITY_YEARS,
    max_volatility: float = config.MAX_VOLATILITY,
    max_drawdown: float = config.MAX_DRAWDOWN,
    size: int = config.BASKET_SIZE,
    reps: int = DEFAULT_REPS,
    samples: int = stats.BOOTSTRAP_SAMPLES,
    seed: int = 0,
    hist: prices.History | None = None,
) -> list[str]:
    dates = backtest.cohort_dates(start_year, end_year)
    if hist is None:
        first = date.fromisoformat(dates[0]) - timedelta(
            days=round(365.25 * stability_years) + FRAME_LEAD_DAYS
        )
        hist = prices.History(first.isoformat(), backtest.horizon_end(dates[-1], horizon))

    cohorts = [
        cohort(
            conn,
            hist,
            as_of,
            horizon=horizon,
            years=years,
            stability_years=stability_years,
            max_volatility=max_volatility,
            max_drawdown=max_drawdown,
            size=size,
            reps=reps,
            seed=seed,
        )
        for as_of in dates
    ]
    effect = backtest.interval(cohorts, mean_effect, samples=samples, seed=seed,
                               confidence=CONFIDENCE)
    return render(cohorts, effect, size=size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the D5 price filters forward")
    parser.add_argument("--start", type=int, default=2012, help="first cohort year")
    parser.add_argument("--end", type=int, default=2020, help="last cohort year")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--years", type=int, default=config.HISTORY_YEARS)
    parser.add_argument("--stability-years", type=int, default=config.STABILITY_YEARS)
    parser.add_argument("--max-volatility", type=float, default=config.MAX_VOLATILITY)
    parser.add_argument("--max-drawdown", type=float, default=config.MAX_DRAWDOWN)
    parser.add_argument("--size", type=int, default=config.BASKET_SIZE)
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS,
                        help="random books drawn per cohort date — the noise floor")
    parser.add_argument("--bootstrap", type=int, default=stats.BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        lines = run(
            conn,
            start_year=args.start,
            end_year=args.end,
            horizon=args.horizon,
            years=args.years,
            stability_years=args.stability_years,
            max_volatility=args.max_volatility,
            max_drawdown=args.max_drawdown,
            size=args.size,
            reps=args.reps,
            samples=args.bootstrap,
            seed=args.seed,
        )
    except stability.StabilityError as exc:
        print(f"no verdict: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
