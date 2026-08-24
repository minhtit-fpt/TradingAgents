"""Run the dividend screen over the store and print what it found.

    python -m tradingagents.value.dividend --tickers PG,KO,JNJ
    python -m tradingagents.value.dividend --all --as-of 2026-01-02 --offline

This surface proposes; it does not hold anything. It names no action, sizes no
position and writes to no ledger — the same line phase 8 draws for price alerts,
for the same reason phases 4b and 6 drew it: the module produces evidence and
the operator decides. Recording what you actually did with a name is
``tradingagents.value.decisions``, and it is a separate command on purpose.

Ordering is the cost model. The numeric criteria are free, so they run over
everything; the only network call is the dividend history, cached by ex-date and
skipped entirely under ``--offline``.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date

from ..screen.criteria import ScreenResult
from ..store import db
from . import config, history, store
from .criteria import evaluate


@dataclass(frozen=True)
class Outcome:
    """One name's dividend verdict, or the reason there isn't one."""

    ticker: str
    result: ScreenResult | None = None
    error: str | None = None

    @property
    def latest_dps(self) -> float | None:
        if self.result is None:
            return None
        paid = self.result.criteria[0].values
        return paid[-1][1] if paid else None


def screen_one(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    *,
    offline: bool = False,
    years: int = config.HISTORY_YEARS,
) -> Outcome:
    """Screen one name. A failure to get data is reported, never scored as a pass."""
    try:
        if not offline:
            history.refresh(conn, ticker)
    except history.DividendError as exc:
        return Outcome(ticker, error=str(exc))

    window = history.window_years(as_of, years)
    dps = history.annual(conn, ticker, as_of, window)
    if not any(dps.values()):
        return Outcome(ticker, error="no dividends on record inside the window")

    financials = db.series_as_of(conn, ticker, as_of, years)
    if not financials:
        return Outcome(ticker, error="no 10-K facts in the store")

    return Outcome(ticker, result=evaluate(dps, financials, years_required=years))


def screen(
    conn: sqlite3.Connection,
    tickers: list[str],
    as_of: str,
    *,
    offline: bool = False,
    years: int = config.HISTORY_YEARS,
) -> list[Outcome]:
    """Screen every name, best first. Quality ranks; it does not gate."""
    outcomes = [
        screen_one(conn, ticker, as_of, offline=offline, years=years) for ticker in tickers
    ]
    return sorted(
        outcomes,
        key=lambda outcome: (
            outcome.result is not None and outcome.result.passed,
            outcome.result.quality if outcome.result else -1.0,
        ),
        reverse=True,
    )


def render(outcomes: list[Outcome], as_of: str) -> list[str]:
    """One line per name, plus the failed criteria that explain a rejection."""
    passed = [o for o in outcomes if o.result is not None and o.result.passed]
    lines = [
        f"dividend screen as of {as_of}: {len(passed)} of {len(outcomes)} pass",
        "",
    ]
    for outcome in outcomes:
        if outcome.error is not None:
            lines.append(f"{outcome.ticker:<6} —      no verdict: {outcome.error}")
            continue

        result = outcome.result
        verdict = "PASS" if result.passed else "fail"
        dps = outcome.latest_dps
        latest = f"last full year {dps:,.2f}/share" if dps else "no payment last year"
        lines.append(
            f"{outcome.ticker:<6} {verdict:<6} quality {result.quality:.0%}, {latest}"
        )
        if not result.passed:
            for criterion in result.criteria:
                if criterion.blocking and not criterion.passed:
                    bad = ", ".join(str(year) for year in criterion.bad_years)
                    lines.append(f"        {criterion.name}: {bad}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="override the store path")
    parser.add_argument("--tickers", default=None, help="comma-separated; default --all")
    parser.add_argument("--all", action="store_true", help="every name with 10-K facts")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--years", type=int, default=config.HISTORY_YEARS)
    parser.add_argument("--offline", action="store_true",
                        help="use the cached dividend history only, fetch nothing")
    args = parser.parse_args(argv)

    conn = store.connect(args.db)
    try:
        if args.tickers:
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        elif args.all:
            tickers = db.tickers(conn)
        else:
            print("give --tickers or --all", file=sys.stderr)
            return 2
        if not tickers:
            print("no tickers to screen; ingest some 10-K facts first", file=sys.stderr)
            return 2

        for line in render(screen(conn, tickers, args.as_of,
                                  offline=args.offline, years=args.years), args.as_of):
            print(line)
        return 0
    finally:
        conn.close()
