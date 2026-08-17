"""The full-universe screening pass.

    python -m tradingagents.value.screen.runner
    python -m tradingagents.value.screen.runner --tickers AAPL,MSFT,KO --as-of 2024-06-30

Order matters, and it is the whole cost model: the thirteen numeric criteria run
first over local SQLite for every eligible company, and only the survivors —
tens of names, not thousands — trigger a price lookup. Valuing everything first
would give the same answer for thousands of network round-trips.

Nothing here sends an alert. This phase produces ``screen_results`` rows;
phase 7 reads them.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date

from ..config import HISTORY_YEARS, MARGIN_OF_SAFETY_MIN, VIOLATION_TOLERANCE
from ..store import db
from . import criteria, intrinsic, market, universe
from .criteria import ScreenResult
from .intrinsic import Valuation, ValuationError


@dataclass(frozen=True)
class Outcome:
    """What the screen concluded about one company, including why it stopped."""

    ticker: str
    screen: ScreenResult | None = None
    valuation: Valuation | None = None
    excluded: str = ""
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.screen is not None and self.screen.passed

    @property
    def triggered(self) -> bool:
        """Passed the criteria *and* trades at or below the required discount."""
        return (
            self.valuation is not None
            and self.valuation.margin_of_safety >= MARGIN_OF_SAFETY_MIN
        )

    @property
    def line(self) -> str:
        if self.excluded:
            return f"{self.ticker}: excluded — {self.excluded}"
        if self.error:
            return f"{self.ticker}: passed the criteria, not valued — {self.error}"
        if not self.passed:
            failed = ", ".join(self.screen.failed_criteria) if self.screen else "?"
            return f"{self.ticker}: failed — {failed}"

        valuation = self.valuation
        marker = "ALERT" if self.triggered else "watch"
        detail = (
            f"MoS {valuation.margin_of_safety:+.1%} "
            f"(price {valuation.price:,.2f} vs value {valuation.intrinsic_value:,.2f}, "
            f"g {valuation.growth_rate:.1%}{'*' if valuation.growth_capped else ''}, "
            f"r {valuation.discount_rate:.2%}, PE {valuation.terminal_pe:.1f})"
        )
        warning = "  [!] Graham number disagrees >3x" if valuation.graham_disagrees else ""
        return f"{self.ticker}: {marker} — {detail}{warning}"


def screen_one(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    discount_rate: float,
    *,
    years: int = HISTORY_YEARS,
    tolerance: int = VIOLATION_TOLERANCE,
    prices=market,
    record: bool = True,
) -> Outcome:
    """Screen and, if it passes, value one company as of ``as_of``."""
    series = db.series_as_of(conn, ticker, as_of, years)
    result = criteria.evaluate(series, years_required=years, tolerance=tolerance)

    if not result.passed:
        if record:
            db.record_screen(conn, ticker, as_of, False, result.failed_criteria)
        return Outcome(ticker=ticker, screen=result)

    try:
        price = prices.close(ticker, as_of)
        # Share counts first: everything per-share below — the EPS history, its
        # growth fit, the median P/E, the Graham number — reads a step in the
        # share basis as a business event unless the split is taken out first.
        filed = db.filed_as_of(conn, ticker, "DilutedShares", as_of, years)
        rebased = intrinsic.on_current_share_basis(
            series, prices.split_basis_factors(ticker, filed, as_of)
        )
        closes = prices.annual_closes(ticker, years, as_of)
        eps = intrinsic.eps_history(rebased)
        valuation = intrinsic.value(
            rebased,
            price,
            discount_rate,
            median_pe=market.median_pe(closes, eps),
        )
    except (market.PriceError, ValuationError) as exc:
        # It cleared thirteen criteria; that verdict is real and gets stored.
        # The valuation is simply absent, and says so rather than defaulting.
        if record:
            db.record_screen(conn, ticker, as_of, True, ())
        return Outcome(ticker=ticker, screen=result, error=str(exc))

    if record:
        db.record_screen(
            conn,
            ticker,
            as_of,
            True,
            (),
            intrinsic_value=valuation.intrinsic_value,
            price=valuation.price,
            mos_pct=valuation.margin_of_safety,
        )
    return Outcome(ticker=ticker, screen=result, valuation=valuation)


def run(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    tickers: list[str] | None = None,
    years: int = HISTORY_YEARS,
    tolerance: int = VIOLATION_TOLERANCE,
    discount_rate: float | None = None,
    prices=market,
    record: bool = True,
) -> list[Outcome]:
    """Screen every eligible company in the store. One rate lookup for the run."""
    eligible = universe.screenable(conn, as_of, tickers=tickers, years=years)
    outcomes = [Outcome(ticker=e.ticker, excluded=e.reason) for e in eligible.excluded]
    if not eligible.tickers:
        return outcomes

    rate = discount_rate if discount_rate is not None else prices.risk_free_rate(as_of)
    for ticker in eligible.tickers:
        outcomes.append(
            screen_one(
                conn,
                ticker,
                as_of,
                rate,
                years=years,
                tolerance=tolerance,
                prices=prices,
                record=record,
            )
        )
    return outcomes


def report(outcomes: list[Outcome], *, quiet: bool = False) -> list[str]:
    """The run's deliverable: the funnel, then the names that survived it."""
    screened = [o for o in outcomes if not o.excluded]
    passed = [o for o in screened if o.passed]
    triggered = [o for o in passed if o.triggered]
    valued = [o for o in passed if o.valuation is not None]

    lines = [
        f"screened {len(screened)}, excluded {len(outcomes) - len(screened)}, "
        f"passed {len(passed)}, at >= {MARGIN_OF_SAFETY_MIN:.0%} MoS {len(triggered)}"
    ]
    if valued:
        best = max(valued, key=lambda o: o.valuation.margin_of_safety)
        lines.append(
            f"closest candidate: {best.ticker} at {best.valuation.margin_of_safety:+.1%} MoS"
        )
    if not triggered:
        # Silence is the normal state of this screen, not a malfunction.
        lines.append("no name is at the trigger — expected in a normal market")

    ranked = sorted(valued, key=lambda o: -o.valuation.margin_of_safety)
    lines.extend(o.line for o in ranked)
    lines.extend(o.line for o in passed if o.valuation is None)
    if not quiet:
        lines.extend(o.line for o in screened if not o.passed)
        lines.extend(o.line for o in outcomes if o.excluded)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tickers", help="comma-separated; default: everything ingested")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--years", type=int, default=HISTORY_YEARS)
    parser.add_argument("--tolerance", type=int, default=VIOLATION_TOLERANCE,
                        help="violation-years allowed per criterion")
    parser.add_argument("--discount-rate", type=float, default=None,
                        help="override the 10-year Treasury yield, e.g. 0.045")
    parser.add_argument("--db", default=None, help="override the store path")
    parser.add_argument("--quiet", action="store_true",
                        help="survivors only, no rejection detail")
    args = parser.parse_args(argv)

    wanted = None
    if args.tickers:
        wanted = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    conn = db.connect(args.db)
    try:
        outcomes = run(
            conn,
            args.as_of,
            tickers=wanted,
            years=args.years,
            tolerance=args.tolerance,
            discount_rate=args.discount_rate,
        )
    except market.PriceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if not outcomes:
        print("nothing ingested yet — run tradingagents.value.jobs.bootstrap first",
              file=sys.stderr)
        return 1

    print("\n".join(report(outcomes, quiet=args.quiet)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
