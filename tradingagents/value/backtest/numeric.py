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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import (
    BACKTEST_BENCHMARK,
    BACKTEST_COMMISSION,
    BACKTEST_MAX_POSITIONS,
    BACKTEST_MIN_POSITIONS,
    BACKTEST_POSITION_CAP,
    BACKTEST_START_CASH,
    BACKTEST_TOP_N,
    HISTORY_YEARS,
    MARGIN_OF_SAFETY_MIN,
    MEMBERSHIP_PATH,
    VIOLATION_TOLERANCE,
)
from ..edgar import tickermap
from ..edgar.client import SecClient
from ..screen import identity, runner
from ..screen.market import PriceError
from ..store import db
from . import membership, portfolio, stats
from .prices import History

DEFAULT_MOS_GRID = (0.20, 0.30, 0.40)
DEFAULT_RANK_GRID = (5, 10, 15, 20)


@dataclass(frozen=True)
class Snapshot:
    """What the screen concluded on one rebalance date."""

    as_of: str
    screened: int
    passed: int
    valued: tuple[tuple[str, float], ...]  # (ticker, margin of safety)
    # Index members on the date, when a point-in-time universe is in use.
    universe: int = 0
    # Of those, the ones the store holds no facts for at all. This is the
    # residual survivorship exposure (phase 4b step 2) and is deliberately kept
    # apart from ``universe - screened``, which also contains ordinary
    # exclusions: too little history, tag coverage below the floor, no revenue.
    # Names, not a count, because "which ones" is the actionable half — a report
    # that says only "18 missing" cannot be worked from.
    absent_names: tuple[str, ...] = ()
    # ticker -> quality score, for the rank-based selection (phase 4b step 4).
    # Kept beside ``valued`` rather than folded into it so the trigger path — and
    # every caller that builds a Snapshot by hand — stays untouched.
    quality: tuple[tuple[str, float], ...] = ()
    # The names behind ``passed``. Step 5 sells on a *quality* exit — a holding
    # leaves the book when it stops clearing the criteria, not when its margin of
    # safety closes — and that test needs the names, not the count. Kept separate
    # from ``valued`` because a name can pass and still fail to be valued (no
    # price at the date), and dropping a holding for a missing quote would be a
    # price exit wearing a quality exit's clothes.
    passed_names: tuple[str, ...] = ()
    error: str = ""

    @property
    def absent(self) -> int:
        return len(self.absent_names)

    def triggered(self, minimum: float) -> tuple[str, ...]:
        return tuple(ticker for ticker, mos in self.valued if mos >= minimum)

    @property
    def still_passing(self) -> frozenset[str]:
        """Names that cleared the criteria on this date — the quality-exit test.

        Union rather than a choice between the two fields: a name that reached a
        valuation necessarily passed, and snapshots built by hand (the tests, and
        every caller predating step 5) carry only ``valued``.
        """
        return frozenset(self.passed_names) | {ticker for ticker, _ in self.valued}

    def top_ranked(self, count: int) -> tuple[str, ...]:
        """The ``count`` best names by quality, margin of safety breaking ties.

        Phase 4b step 4. Item 8's own 0%-trigger row beat the configured 30% row,
        and item 4 showed the one-sided terminal-P/E cap leaves the margin of
        safety largely noise — so gating on it selects noise. Ranking on quality
        and letting the margin of safety break ties spends the same capital on
        the same names in a different order, and holds ~10 rather than whatever
        happened to clear a threshold.

        A name with no quality score sorts last: absent evidence is not evidence.
        """
        scores = dict(self.quality)
        ranked = sorted(
            self.valued,
            key=lambda pair: (-scores.get(pair[0], 0.0), -pair[1], pair[0]),
        )
        return tuple(ticker for ticker, _ in ranked[:count])


class UniverseError(RuntimeError):
    """The point-in-time universe could not be resolved.

    Raised rather than fallen back from: screening the store's full ticker list
    instead would silently restore the survivorship bias phase 4b removed, and a
    biased run that says nothing about being biased is the item-8 defect.
    """


def resolve_universe(
    kind: str,
    dates: Sequence[str],
    *,
    membership_path: str | None = None,
    db_path: str | None = None,
) -> tuple[Callable[[str], Sequence[str]] | None, list[str]]:
    """The per-date universe callable for ``snapshots``, plus lines to print.

    ``None`` for ``kind="store"`` — the survivorship-biased static list, kept
    only to reproduce the pre-4b runs. Shared with the phase-6 sampled backtest
    so both replays screen the identical universe; two definitions of "the
    universe" would make the two runs incomparable in a way nothing prints.
    """
    if kind != "index":
        return None, []

    try:
        members = membership.load(membership_path)
    except membership.MembershipError as exc:
        raise UniverseError(str(exc)) from exc
    uncovered = [d for d in dates if not members.covers(d)]
    if uncovered:
        raise UniverseError(
            f"membership data ({members.span[0]} .. {members.span[1]}) does not "
            f"cover {len(uncovered)} rebalance dates, first {uncovered[0]}; "
            "narrow --start/--end or refresh the file"
        )

    # Rewrite historical tickers onto whatever ticker the store holds that CIK
    # under (BK -> BNY, GOOGL -> GOOG). Without this a rename reads as a missing
    # company and rejoins the survivorship hole it never left.
    try:
        mapping = tickermap.load(SecClient())
    except (ValueError, OSError) as exc:
        raise UniverseError(
            f"cannot resolve ticker identity: {exc}. Aliasing changes which names "
            "are screened, so it is not skipped silently — set VALUE_SEC_USER_AGENT, "
            "or run --universe store to reproduce the pre-4b behaviour."
        ) from exc

    conn = db.connect(db_path)
    try:
        alias_map = identity.aliases(conn, mapping)
    finally:
        conn.close()
    if not alias_map:
        return members.as_of, []

    def universe(day: str) -> tuple[str, ...]:
        return identity.apply(members.as_of(day), alias_map)

    return universe, [
        f"identity: {len(alias_map)} historical tickers aliased onto the "
        "tickers the store holds them under"
    ]


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
    universe: Callable[[str], Sequence[str]] | None = None,
    years: int = HISTORY_YEARS,
    tolerance: int = VIOLATION_TOLERANCE,
) -> list[Snapshot]:
    """Run the screen once per rebalance date, storing nothing.

    ``record=False`` matters: a backtest that wrote into ``screen_results``
    would leave the live daily job reading ten-year-old verdicts.

    ``universe`` resolves the candidate list *per date* — pass
    ``Membership.as_of`` to screen the index as it stood then. Without it every
    date is screened against one static list, which is the defect that produced
    the item-8 and item-9 verdicts. ``tickers`` still narrows whatever the
    universe yields, so a quick run over three names stays possible.
    """
    wanted = [t.upper() for t in tickers] if tickers else None
    ingested = set(db.tickers(conn)) if universe is not None else set()
    results = []
    for as_of in dates:
        members = 0
        absent: tuple[str, ...] = ()
        candidates = wanted
        if universe is not None:
            try:
                on_the_day = [t.upper() for t in universe(as_of)]
            except membership.MembershipError as exc:
                # No universe for the date. Screening the store's full list
                # instead would silently reintroduce the survivorship bias this
                # parameter exists to remove, and an empty list would read as
                # "nothing qualified" in the report.
                results.append(Snapshot(as_of, 0, 0, (), error=str(exc)))
                continue
            members = len(on_the_day)
            absent = tuple(t for t in on_the_day if t not in ingested)
            candidates = (
                [t for t in on_the_day if t in set(wanted)] if wanted else on_the_day
            )

        try:
            outcomes = runner.run(
                conn,
                as_of,
                tickers=candidates,
                years=years,
                tolerance=tolerance,
                prices=prices,
                record=False,
            )
        except PriceError as exc:
            # No Treasury yield for that date: every valuation on it would be
            # discounted at an invented rate. Skip the date, say so, keep going.
            results.append(
                Snapshot(as_of, 0, 0, (), universe=members, absent_names=absent, error=str(exc))
            )
            continue

        screened = [o for o in outcomes if not o.excluded]
        passed = [o for o in screened if o.passed]
        priced = [o for o in passed if o.valuation is not None]
        valued = tuple((o.ticker, o.valuation.margin_of_safety) for o in priced)
        quality = tuple(
            (o.ticker, o.screen.quality) for o in priced if o.screen is not None
        )
        results.append(
            Snapshot(as_of, len(screened), len(passed), valued,
                     universe=members, absent_names=absent, quality=quality,
                     passed_names=tuple(o.ticker for o in passed))
        )
    return results


def schedule_for(snaps: list[Snapshot], minimum: float) -> list[tuple[str, tuple[str, ...]]]:
    """Turn snapshots into ``(date, holdings)`` at one trigger level."""
    return [(snap.as_of, snap.triggered(minimum)) for snap in snaps if not snap.error]


def schedule_ranked(snaps: list[Snapshot], count: int) -> list[tuple[str, tuple[str, ...]]]:
    """``(date, holdings)`` holding the ``count`` best-ranked names per date."""
    return [(snap.as_of, snap.top_ranked(count)) for snap in snaps if not snap.error]


def construct(
    snaps: list[Snapshot],
    picks: Callable[[Snapshot], tuple[str, ...]],
    *,
    minimum: int,
    maximum: int,
    quality_exit: bool,
) -> list[tuple[str, tuple[str, ...]]]:
    """Apply phase 4b step 5's construction rules to a per-date pick list.

    ``picks`` is whichever selection is running — the margin-of-safety trigger or
    the quality rank. This layer sits above it and decides what the *book* does
    with those picks:

    * **quality exit.** An incumbent that still clears the criteria keeps its
      slot even if its margin of safety has closed or it has fallen out of the
      top N. F1 in the deferred list, and the highest-fidelity single change
      available: Buffett's return is the not-selling, and a quarterly rebalance
      that ejects a compounder the moment a conservative DCF calls it rich is
      the opposite of that. Without this flag the book is rebuilt from ``picks``
      every date, as before.
    * **minimum.** Below it the strategy does not *open* a book at all — an idea
      count that thin is the item-7 failure mode, not a portfolio. It never
      forces a liquidation: a book that has decayed to three names decayed
      through quality exits, and selling those three would be the very trade the
      rule above exists to prevent.
    * **maximum.** Incumbents are listed first, so the bound turns away new ideas
      rather than selling held ones.
    """
    book: tuple[str, ...] = ()
    schedule = []
    for snap in snaps:
        if snap.error:
            continue
        still = snap.still_passing
        kept = [t for t in book if t in still] if quality_exit else []
        fresh = [t for t in picks(snap) if t not in kept]
        # Flat and too few ideas to open on: stay flat. A non-empty ``kept``
        # short-circuits this, which is what stops the minimum from ever forcing
        # a liquidation.
        thin = not kept and len(fresh) < minimum
        book = () if thin else tuple((kept + fresh)[:maximum])
        schedule.append((snap.as_of, book))
    return schedule


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
    power: Sequence[str] = (),
    selection: str = "trigger",
    construction: str = "equal",
    exit_rule: str = "rebalance",
    configured: float = MARGIN_OF_SAFETY_MIN,
    quiet: bool = False,
) -> list[str]:
    """The deliverable. The caveats are part of it, not a footnote."""
    live = [s for s in snaps if not s.error]
    skipped = [s for s in snaps if s.error]
    lines = [
        f"value screen backtest {start} -> {end}, {len(snaps)} quarterly rebalance dates",
    ]
    point_in_time = any(s.universe for s in snaps)
    if live:
        prefix = ""
        if point_in_time:
            prefix = f"in the index {_mean(s.universe for s in live):.0f}, "
        lines.append(
            f"per date, on average: {prefix}"
            f"screened {_mean(s.screened for s in live):.0f}, "
            f"passed the criteria {_mean(s.passed for s in live):.1f}, "
            f"valued {_mean(len(s.valued) for s in live):.1f}"
        )
        if point_in_time:
            absent = _mean(s.absent for s in live)
            other = _mean(s.universe - s.screened - s.absent for s in live)
            lines.append(
                "universe: point-in-time index membership. Per date, "
                f"{absent:.0f} members were absent from the store — that number, not the "
                "one beside it, is the remaining survivorship exposure (phase 4b step 2). "
                f"A further {other:.0f} were present but excluded on their merits: too "
                "little history, tag coverage below the floor, or no revenue."
            )
        else:
            lines.append(
                "universe: STATIC — today's ticker list applied to every date. Every "
                "company since acquired, taken private or demoted was never a candidate, "
                "so the returns below are biased upward. Run with --universe index."
            )
    if skipped:
        lines.append(f"{len(skipped)} dates skipped (no discount rate): "
                     + ", ".join(s.as_of for s in skipped[:5]))

    bench = "n/a" if benchmark_return is None else f"{benchmark_return:+.2%}"
    lines.append(f"benchmark {benchmark} buy-and-hold CAGR: {bench}")

    # Construction is a strategy change, so it goes on the face of the report
    # rather than in the caveats: two runs whose grids differ only because one
    # was capped are not comparable, and nothing else here would say so.
    if construction == "capped" or exit_rule == "quality":
        rules = []
        if construction == "capped":
            rules.append(
                f"a position is capped at {BACKTEST_POSITION_CAP:.0%} of NAV (the excess "
                f"stays in cash), no book is opened thinner than {BACKTEST_MIN_POSITIONS} "
                f"names, and at most {BACKTEST_MAX_POSITIONS} are held"
            )
        if exit_rule == "quality":
            rules.append(
                "a holding is kept while it still clears the criteria, whatever its "
                "margin of safety has done since — so an exit here is a quality exit"
            )
        lines.append("construction (phase 4b step 5): " + "; ".join(rules) + ".")
    else:
        lines.append(
            "construction: equal weight across every pick, uncapped, rebuilt each "
            "rebalance — the pre-step-5 behaviour."
        )
    ranked = selection == "rank"
    lines.append("")
    axis = "position count" if ranked else "margin-of-safety trigger"
    lines.append(f"sensitivity display — one row per {axis}, holding nearly the same names. "
                 "Not evidence: the grid's own spread is the interval, and reading a best "
                 "cell out of it is what produced the item-8 verdict.")
    lines.append(
        ("quality rank  " if ranked else "MoS trigger   ")
        + "CAGR      vs bench   max DD    trades  hit rate  avg bars held"
    )
    for level, result in runs:
        cagr = "n/a" if result.cagr is None else f"{result.cagr:+.2%}"
        excess = (
            "n/a"
            if result.cagr is None or benchmark_return is None
            else f"{result.cagr - benchmark_return:+.2%}"
        )
        hit = "n/a" if result.hit_rate is None else f"{result.hit_rate:.0%}"
        marker = " *" if abs(level - configured) < 1e-9 else "  "
        cell = f"    top {int(level):>3}" if ranked else f"{level:>10.0%}"
        lines.append(
            f"{cell}{marker} {cagr:>8}  {excess:>9}  "
            f"{result.max_drawdown:>7.1%}  {result.trades:>6}  {hit:>8}  "
            f"{result.average_bars_held:>13.0f}"
        )
    lines.append(
        "* the configured count (VALUE_BACKTEST_TOP_N); the margin of safety is a "
        "tie-break here, not a gate"
        if ranked else
        "* the configured trigger (VALUE_MOS_MIN)"
    )
    if point_in_time:
        never = sorted({t for s in snaps for t in s.absent_names})
        if never:
            lines.append("")
            lines.append(
                f"index members the store cannot screen at all ({len(never)}) — the "
                "residual survivorship exposure, named so it can be worked from:"
            )
            lines.append("  " + ", ".join(never))
    bounced = sum(result.rejected for _, result in runs)
    if bounced:
        # A refused order is a rebalance that did not happen, and it shows up as
        # a flatter curve rather than as an error. Say so on the face of it.
        lines.append(f"warning: {bounced} orders were refused by the broker "
                     "(insufficient cash at the fill) — those rebalances did not happen")

    if power:
        lines.append("")
        lines.extend(power)

    lines.append("")
    lines.extend(_caveats(missing, live, point_in_time=point_in_time))
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


def _caveats(
    missing: dict[str, str], live: list[Snapshot], *, point_in_time: bool = False
) -> list[str]:
    """The three ways this backtest lies, quantified where possible (plan 10)."""
    valued_names = {ticker for snap in live for ticker, _ in snap.valued}
    total = len(valued_names) + len(missing)
    share = f"{len(missing) / total:.0%}" if total else "0%"
    entry = (
        f"  survivorship: {len(missing)} of {total} names ({share}) that reached valuation had "
        "no price series and are absent from these returns. Delisted companies keep their "
        "EDGAR filings but lose their prices, so the survivors are over-represented and the "
        "CAGR above is biased upward."
    )
    if point_in_time:
        # This counter is close to zero by construction and must not be read as
        # reassurance: it counts only names that reached a valuation and then had
        # no prices. A delisted company usually never gets that far — EDGAR has
        # no companyfacts for it (FRC, SBNY return 404) or it fell below the
        # coverage floor — so it is missing from the universe, not from this set.
        absent = sorted({t for snap in live for t in snap.absent_names})
        entry += (
            f" Read this number with the {len(absent)} unscreenable index members listed "
            "above, not instead of them: a name with no prices is usually also a name with "
            "no usable filings, so it never reaches this counter. The listed members are "
            "the residual bias, and yfinance serves no history for a delisted ticker, so "
            "no amount of further EDGAR ingest closes it — only a bound does."
        )
    return [
        "caveats — read before believing any number above:",
        entry,
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
    parser.add_argument("--tickers", help="comma-separated; narrows the universe")
    parser.add_argument(
        "--universe", choices=("index", "store"), default="index",
        help="index: point-in-time S&P 500 membership per date (default). "
             "store: today's ingested ticker list applied to every date — "
             "survivorship-biased, kept only to reproduce the pre-4b runs",
    )
    parser.add_argument(
        "--membership", default=None,
        help=f"membership CSV; default {MEMBERSHIP_PATH} (VALUE_MEMBERSHIP_PATH)",
    )
    parser.add_argument("--years", type=int, default=HISTORY_YEARS)
    parser.add_argument("--tolerance", type=int, default=VIOLATION_TOLERANCE)
    parser.add_argument("--mos-grid", default=",".join(str(m) for m in DEFAULT_MOS_GRID),
                        help="margin-of-safety triggers to compare, comma-separated")
    parser.add_argument(
        "--select", choices=("trigger", "rank"), default="trigger",
        help="trigger: hold every name at or above the margin-of-safety threshold "
             "(the pre-4b behaviour). rank: hold the top N by quality with the margin "
             "of safety breaking ties (phase 4b step 4)",
    )
    parser.add_argument("--rank-grid", default=",".join(str(n) for n in DEFAULT_RANK_GRID),
                        help="position counts to compare under --select rank")
    # argparse %-expands help strings, so a literal percent sign has to be
    # doubled or add_argument raises "badly formed help string".
    cap_text = f"{BACKTEST_POSITION_CAP:.0%}".replace("%", "%%")
    parser.add_argument(
        "--construct", choices=("equal", "capped"), default="equal",
        help="equal: hold every pick at equal weight, no cap (the pre-4b-step-5 "
             f"behaviour). capped: cap a position at {cap_text} of NAV, "
             f"open no book thinner than {BACKTEST_MIN_POSITIONS} names, hold at most "
             f"{BACKTEST_MAX_POSITIONS} (phase 4b step 5)",
    )
    parser.add_argument(
        "--exit", dest="exit_rule", choices=("rebalance", "quality"), default="rebalance",
        help="rebalance: the book is whatever the selection picks today (the "
             "pre-4b-step-5 behaviour). quality: a holding is kept while it still "
             "clears the criteria, even once its margin of safety has closed (F1)",
    )
    parser.add_argument(
        "--bootstrap", type=int, default=stats.BOOTSTRAP_SAMPLES,
        help="resamples behind the confidence intervals; 0 disables them (and with "
             "them the pre-registered verdict)",
    )
    parser.add_argument("--seed", type=int, default=0, help="bootstrap seed")
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
    # The pre-registered criterion is judged at the configured setting, so that
    # cell is always simulated whatever the grid says.
    ranked = args.select == "rank"
    configured = float(BACKTEST_TOP_N) if ranked else MARGIN_OF_SAFETY_MIN
    cells = args.rank_grid if ranked else args.mos_grid
    grid = sorted(
        {float(cell) for cell in cells.split(",") if cell.strip()} | {configured}
    )

    dates = quarter_ends(args.start, args.end)
    if not dates:
        print(f"error: no quarter end between {args.start} and {args.end}", file=sys.stderr)
        return 2

    # Resolve the universe before any screening: a missing membership file an
    # hour into the run is an hour wasted, and falling back to the static list
    # would quietly restore the bias phase 4b exists to remove.
    try:
        as_of_universe, notes = resolve_universe(
            args.universe, dates, membership_path=args.membership, db_path=args.db
        )
    except UniverseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(note)

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
            tickers=wanted, universe=as_of_universe,
            years=args.years, tolerance=args.tolerance,
        )
    finally:
        conn.close()

    if not any(snap.valued for snap in snaps):
        print("no name reached a valuation on any rebalance date — nothing to simulate; "
              "run tradingagents.value.jobs.bootstrap first, or widen the window",
              file=sys.stderr)
        return 1

    capped = args.construct == "capped"
    quality_exit = args.exit_rule == "quality"

    def holdings(level: float) -> list[tuple[str, tuple[str, ...]]]:
        if not capped and not quality_exit:
            return schedule_ranked(snaps, int(level)) if ranked else schedule_for(snaps, level)
        picks = (
            (lambda snap: snap.top_ranked(int(level)))
            if ranked else (lambda snap: snap.triggered(level))
        )
        return construct(
            snaps, picks,
            minimum=BACKTEST_MIN_POSITIONS if capped else 0,
            maximum=BACKTEST_MAX_POSITIONS if capped else len(snaps) + 1,
            quality_exit=quality_exit,
        )

    runs = [
        (
            level,
            portfolio.simulate(
                {t: history.frame(t) for t in history.fetched},
                holdings(level),
                clock,
                start=args.start,
                end=args.end,
                start_cash=args.cash,
                commission=args.commission,
                position_cap=BACKTEST_POSITION_CAP if capped else 1.0,
            ),
        )
        for level in grid
    ]

    judged = next(
        (result for level, result in runs if abs(level - configured) < 1e-9), None
    )
    power: list[str] = []
    if judged is not None:
        power = stats.summary(
            [snap for snap in snaps if not snap.error],
            judged.curve,
            stats.curve_of(clock),
            held={day: len(names) for day, names in holdings(configured)},
            setting=(
                f"top {int(configured)} by quality rank"
                if ranked else f"the configured trigger {configured:.0%}"
            ),
            years=judged.years,
            benchmark=args.benchmark,
            bars_held=judged.average_bars_held,
            samples=args.bootstrap,
            seed=args.seed,
        )

    print("\n".join(report(
        snaps, runs,
        start=args.start, end=args.end,
        benchmark=args.benchmark,
        benchmark_return=benchmark_cagr(clock, args.start, args.end),
        missing=history.missing,
        power=power,
        selection=args.select,
        construction=args.construct,
        exit_rule=args.exit_rule,
        configured=configured,
        quiet=args.quiet,
    )))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
