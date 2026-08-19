"""The tier-1 backtest: replay the screen over a decade, for free.

    python -m tradingagents.value.backtest.numeric --start 2016-01-01 --end 2026-01-01
    python -m tradingagents.value.backtest.numeric --tickers AAPL,MSFT,KO --quiet

Phase 4 of the plan is a real stop: if this shows no edge, the project ends here
having spent nothing on tokens. So the report is built to be *disbelieved* —
every number it prints comes with the count of names behind it, and the two
biases that would flatter it are printed whether or not anyone asks.

Costs $0 in LLM tokens. The whole run is SQLite plus one price fetch per name
that clears the criteria.
"""

import argparse
import calendar
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import (
    BACKTEST_BENCHMARK,
    BACKTEST_COMMISSION,
    BACKTEST_START_CASH,
    HISTORY_YEARS,
    MARGIN_OF_SAFETY_MIN,
    VIOLATION_TOLERANCE,
)
from ..screen import runner
from ..screen.market import PriceError
from ..store import db
from . import portfolio
from .prices import History

DEFAULT_MOS_GRID = (0.20, 0.30, 0.40)


@dataclass(frozen=True)
class Snapshot:
    """What the screen concluded on one rebalance date."""

    as_of: str
    screened: int
    passed: int
    valued: tuple[tuple[str, float], ...]  # (ticker, margin of safety)
    error: str = ""

    def triggered(self, minimum: float) -> tuple[str, ...]:
        return tuple(ticker for ticker, mos in self.valued if mos >= minimum)


def quarter_ends(start: str, end: str) -> list[str]:
    """Quarter-end dates within ``[start, end]``.

    Quarterly, not daily: a value screen built on annual filings cannot change
    its mind meaningfully between quarters, and a daily replay would multiply
    the work by sixty for the same answer.
    """
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    dates = []
    for year in range(first.year, last.year + 1):
        for month in (3, 6, 9, 12):
            day = date(year, month, calendar.monthrange(year, month)[1])
            if first <= day <= last:
                dates.append(day.isoformat())
    return dates


def snapshots(
    conn: sqlite3.Connection,
    dates: list[str],
    prices: History,
    *,
    tickers: list[str] | None = None,
    years: int = HISTORY_YEARS,
    tolerance: int = VIOLATION_TOLERANCE,
) -> list[Snapshot]:
    """Run the screen once per rebalance date, storing nothing.

    ``record=False`` matters: a backtest that wrote into ``screen_results``
    would leave the live daily job reading ten-year-old verdicts.
    """
    results = []
    for as_of in dates:
        try:
            outcomes = runner.run(
                conn,
                as_of,
                tickers=tickers,
                years=years,
                tolerance=tolerance,
                prices=prices,
                record=False,
            )
        except PriceError as exc:
            # No Treasury yield for that date: every valuation on it would be
            # discounted at an invented rate. Skip the date, say so, keep going.
            results.append(Snapshot(as_of, 0, 0, (), error=str(exc)))
            continue

        screened = [o for o in outcomes if not o.excluded]
        passed = [o for o in screened if o.passed]
        valued = tuple(
            (o.ticker, o.valuation.margin_of_safety) for o in passed if o.valuation is not None
        )
        results.append(Snapshot(as_of, len(screened), len(passed), valued))
    return results


def schedule_for(snaps: list[Snapshot], minimum: float) -> list[tuple[str, tuple[str, ...]]]:
    """Turn snapshots into ``(date, holdings)`` at one trigger level."""
    return [(snap.as_of, snap.triggered(minimum)) for snap in snaps if not snap.error]


def benchmark_cagr(frame: Any, start: str, end: str) -> float | None:
    """Buy-and-hold annualised return of the benchmark over the same window."""
    window = frame[
        (frame.index.date >= date.fromisoformat(start))
        & (frame.index.date <= date.fromisoformat(end))
    ]
    if len(window) < 2:
        return None
    first, last = float(window["Close"].iloc[0]), float(window["Close"].iloc[-1])
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days / 365.25
    if first <= 0 or span <= 0:
        return None
    return (last / first) ** (1 / span) - 1


def report(
    snaps: list[Snapshot],
    runs: list[tuple[float, portfolio.Result]],
    *,
    start: str,
    end: str,
    benchmark: str,
    benchmark_return: float | None,
    missing: dict[str, str],
    quiet: bool = False,
) -> list[str]:
    """The deliverable. The caveats are part of it, not a footnote."""
    live = [s for s in snaps if not s.error]
    skipped = [s for s in snaps if s.error]
    lines = [
        f"value screen backtest {start} -> {end}, {len(snaps)} quarterly rebalance dates",
    ]
    if live:
        lines.append(
            f"per date, on average: screened {_mean(s.screened for s in live):.0f}, "
            f"passed the criteria {_mean(s.passed for s in live):.1f}, "
            f"valued {_mean(len(s.valued) for s in live):.1f}"
        )
    if skipped:
        lines.append(f"{len(skipped)} dates skipped (no discount rate): "
                     + ", ".join(s.as_of for s in skipped[:5]))

    bench = "n/a" if benchmark_return is None else f"{benchmark_return:+.2%}"
    lines.append(f"benchmark {benchmark} buy-and-hold CAGR: {bench}")
    lines.append("")
    lines.append("MoS trigger   CAGR      vs bench   max DD    trades  hit rate  avg bars held")
    for level, result in runs:
        cagr = "n/a" if result.cagr is None else f"{result.cagr:+.2%}"
        excess = (
            "n/a"
            if result.cagr is None or benchmark_return is None
            else f"{result.cagr - benchmark_return:+.2%}"
        )
        hit = "n/a" if result.hit_rate is None else f"{result.hit_rate:.0%}"
        marker = " *" if abs(level - MARGIN_OF_SAFETY_MIN) < 1e-9 else "  "
        lines.append(
            f"{level:>10.0%}{marker} {cagr:>8}  {excess:>9}  "
            f"{result.max_drawdown:>7.1%}  {result.trades:>6}  {hit:>8}  "
            f"{result.average_bars_held:>13.0f}"
        )
    lines.append("* the configured trigger (VALUE_MOS_MIN)")
    bounced = sum(result.rejected for _, result in runs)
    if bounced:
        # A refused order is a rebalance that did not happen, and it shows up as
        # a flatter curve rather than as an error. Say so on the face of it.
        lines.append(f"warning: {bounced} orders were refused by the broker "
                     "(insufficient cash at the fill) — those rebalances did not happen")

    lines.append("")
    lines.extend(_caveats(missing, live))
    if not quiet:
        lines.append("")
        for snap in snaps:
            if snap.error:
                lines.append(f"{snap.as_of}: skipped — {snap.error}")
                continue
            names = ", ".join(
                f"{t} {m:+.0%}" for t, m in sorted(snap.valued, key=lambda p: -p[1])
            )
            lines.append(
                f"{snap.as_of}: screened {snap.screened}, passed {snap.passed}"
                + (f" — {names}" if names else "")
            )
    return lines


def _caveats(missing: dict[str, str], live: list[Snapshot]) -> list[str]:
    """The three ways this backtest lies, quantified where possible (plan 10)."""
    valued_names = {ticker for snap in live for ticker, _ in snap.valued}
    total = len(valued_names) + len(missing)
    share = f"{len(missing) / total:.0%}" if total else "0%"
    return [
        "caveats — read before believing any number above:",
        f"  survivorship: {len(missing)} of {total} names ({share}) that reached valuation had "
        "no price series and are absent from these returns. Delisted companies keep their "
        "EDGAR filings but lose their prices, so the survivors are over-represented and the "
        "CAGR above is biased upward.",
        "  restatements: companyfacts serves restated figures. Facts are filtered on the "
        "filed date, so nothing filed after a rebalance is visible — but a figure restated "
        "later reads here as if it had always said that.",
        "  criteria thresholds are not swept here; only the margin-of-safety trigger is. "
        "Re-run with the VALUE_* threshold vars to test the rest.",
    ]


def _mean(values) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default=f"{date.today().year - 10}-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--tickers", help="comma-separated; default: everything ingested")
    parser.add_argument("--years", type=int, default=HISTORY_YEARS)
    parser.add_argument("--tolerance", type=int, default=VIOLATION_TOLERANCE)
    parser.add_argument("--mos-grid", default=",".join(str(m) for m in DEFAULT_MOS_GRID),
                        help="margin-of-safety triggers to compare, comma-separated")
    parser.add_argument("--benchmark", default=BACKTEST_BENCHMARK)
    parser.add_argument("--cash", type=float, default=BACKTEST_START_CASH)
    parser.add_argument("--commission", type=float, default=BACKTEST_COMMISSION)
    parser.add_argument("--interval", default="1d", help="price bar size, e.g. 1d or 1mo")
    parser.add_argument("--db", default=None, help="override the store path")
    parser.add_argument("--quiet", action="store_true", help="summary only, no per-date detail")
    args = parser.parse_args(argv)

    wanted = None
    if args.tickers:
        wanted = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    grid = sorted({float(level) for level in args.mos_grid.split(",") if level.strip()})

    dates = quarter_ends(args.start, args.end)
    if not dates:
        print(f"error: no quarter end between {args.start} and {args.end}", file=sys.stderr)
        return 2

    # Prices must reach back a decade before the first rebalance: the valuation
    # wants ten years of year-end closes at that date, not at the last one.
    lookback = f"{date.fromisoformat(args.start).year - args.years}-01-01"
    history = History(lookback, args.end, interval=args.interval)

    # Fetch the benchmark first. It drives the calendar, so failing on it after
    # an hour of screening would waste the whole run.
    try:
        clock = history.frame(args.benchmark)
    except PriceError as exc:
        print(f"error: benchmark {args.benchmark}: {exc}", file=sys.stderr)
        return 2

    conn = db.connect(args.db)
    try:
        snaps = snapshots(
            conn, dates, history,
            tickers=wanted, years=args.years, tolerance=args.tolerance,
        )
    finally:
        conn.close()

    if not any(snap.valued for snap in snaps):
        print("no name reached a valuation on any rebalance date — nothing to simulate; "
              "run tradingagents.value.jobs.bootstrap first, or widen the window",
              file=sys.stderr)
        return 1

    runs = [
        (
            level,
            portfolio.simulate(
                {t: history.frame(t) for t in history.fetched},
                schedule_for(snaps, level),
                clock,
                start=args.start,
                end=args.end,
                start_cash=args.cash,
                commission=args.commission,
            ),
        )
        for level in grid
    ]

    print("\n".join(report(
        snaps, runs,
        start=args.start, end=args.end,
        benchmark=args.benchmark,
        benchmark_return=benchmark_cagr(clock, args.start, args.end),
        missing=history.missing,
        quiet=args.quiet,
    )))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
