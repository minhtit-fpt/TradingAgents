"""Phase 6: the sampled tier-3 backtest — does reading the filing earn its cost?

    # what it would spend, before spending it
    python -m tradingagents.value.backtest.llm_sample --start 2014-01-01 --end 2026-06-30 --dry-run

    # the run itself
    python -m tradingagents.value.backtest.llm_sample --start 2014-01-01 --end 2026-06-30 --sample 60

Phase 6 asks one question: does an LLM veto over the numeric screen's own picks
improve the outcome **over the numeric screen alone**? That is a *paired*
comparison — the same dates, the same universe, the same construction, one book
filtered and one not — so what has to be beaten is not SPY. It is zero.

Three things this module refuses to let the answer be read off noise:

- **The noise floor is measured, not assumed.** A veto applied at random has no
  skill by construction, so whatever effect it produces at the same veto rate is
  the minimum detectable effect. It is printed beside the measured effect, and
  the verdict requires clearing it. Phase 4b step 6 established that this floor
  is the binding constraint here, not the sample size.
- **The criterion is pre-registered** below, before any run, exactly as phase 4b
  step 3 does for the numeric gate.
- **The cost is on the face of the report.** "Earns its cost" is the question, so
  a run that improved nothing and a run that improved something for $40 are both
  answers, and neither is legible without the dollars.

Point-in-time throughout: the 10-K read for a name held on date D is the most
recent one **filed on or before D** (``edgar.filings.fetch_10k``), and the
numeric summary handed to the analyst is the screen's own verdict at D. Nothing
in the prompt knows what happened next.

What this costs: one DeepSeek call per (ticker, fiscal year) event, cached on
disk, charged against ``llm.budget``. The random arm and every simulation are $0.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..analyst import value_analyst
from ..analyst.schemas import ValueAssessment, Verdict
from ..config import (
    ANALYST_MODEL,
    BACKTEST_BENCHMARK,
    BACKTEST_COMMISSION,
    BACKTEST_MAX_POSITIONS,
    BACKTEST_MIN_POSITIONS,
    BACKTEST_POSITION_CAP,
    BACKTEST_START_CASH,
    BACKTEST_TOP_N,
    HISTORY_YEARS,
    MARGIN_OF_SAFETY_MIN,
    RUN_BUDGET_USD,
    SECTION_TOKEN_BUDGET,
    VIOLATION_TOLERANCE,
)
from ..edgar import client as client_module, filings
from ..edgar.client import SecClient
from ..llm import budget as budget_module
from ..llm.budget import Budget
from ..screen.market import PriceError
from ..store import db
from . import numeric, portfolio, stats
from .prices import History

Schedule = list[tuple[str, tuple[str, ...]]]

# Pre-registered before any run, in the spirit of phase 4b step 3. Reproduced in
# the report so it cannot be cited after having been edited to fit the result.
CRITERION = (
    "pre-registered pass criterion (phase 6): tier 3 earns its cost only if the "
    "bootstrap CI for the filtered book's CAGR against the unfiltered book is entirely "
    "above zero AND the point estimate clears the measured noise floor — the effect a "
    "skill-free random veto of the same size produces on the same dates. Both at the "
    "configured settings. An effect inside the floor is the apparatus, not the analyst."
)

# What a verdict has to be for the name to be dropped. ``avoid`` is the narrow
# reading and the default: caution is explicitly defined in the schema as a
# concern that does *not* sink the thesis, so vetoing on it tests a different
# rule and is offered as one rather than folded into this one.
VETO_RULES: dict[str, tuple[Verdict, ...]] = {
    "avoid": (Verdict.AVOID,),
    "avoid+caution": (Verdict.AVOID, Verdict.CAUTION),
}

DEFAULT_SAMPLE = 60
# Reps of the random arm. Phase 4b step 6 ran 10 and noted the medians move a few
# tenths between seeds; that is the honest resolution of this floor, so the
# report prints the seed and the rep count rather than implying more.
DEFAULT_REPS = 10

# For --dry-run only. Three sections at their ceiling plus the system prompt and
# the numeric summary; the real number is whatever DeepSeek counts, and the
# budget charges that. An estimate that runs low would understate the decision
# the user is about to make, so it is rounded up.
EST_PROMPT_TOKENS = 3 * SECTION_TOKEN_BUDGET + 1_000
EST_COMPLETION_TOKENS = 700


@dataclass(frozen=True)
class Event:
    """One (company, fiscal year) the book held — the unit tier 3 is paid per.

    Not (company, rebalance date): a 10-K covers a year, so every quarterly date
    inside one year reads the same filing and produces a byte-identical prompt.
    Keying the event on the year makes that explicit rather than leaving it to
    the prompt cache to notice, and makes ``--sample`` a count of *calls* rather
    than a count of dates that mostly collapse onto each other.
    """

    ticker: str
    year: int
    # The first rebalance date in that year the name was held: the as-of date the
    # filing is fetched at, so nothing filed later than the decision is visible.
    as_of: str


@dataclass(frozen=True)
class Arm:
    """One simulated book and its excess over the unfiltered one."""

    label: str
    result: portfolio.Result
    excess: stats.Interval | None


def events(schedule: Schedule) -> tuple[Event, ...]:
    """Every (ticker, year) the schedule holds, at the earliest date it holds it."""
    first: dict[tuple[str, int], str] = {}
    for as_of, names in schedule:
        year = int(as_of[:4])
        for ticker in names:
            first.setdefault((ticker, year), as_of)
    return tuple(Event(ticker, year, as_of) for (ticker, year), as_of in sorted(first.items()))


def sample(candidates: Sequence[Event], count: int, seed: int = 0) -> tuple[Event, ...]:
    """``count`` events drawn without replacement; all of them when count <= 0."""
    if count <= 0 or count >= len(candidates):
        return tuple(candidates)
    rng = random.Random(seed)
    return tuple(sorted(rng.sample(list(candidates), count), key=lambda e: (e.as_of, e.ticker)))


def numeric_summary(snap: numeric.Snapshot, ticker: str) -> str:
    """What the screen concluded about this name on this date, in one line.

    Handed to the analyst so it argues with the numeric case rather than
    re-deriving it. Point-in-time by construction: every figure comes from the
    snapshot for ``snap.as_of``.
    """
    mos = dict(snap.valued).get(ticker)
    quality = dict(snap.quality).get(ticker)
    parts = [f"{ticker} cleared the thirteen-criterion screen as of {snap.as_of}"]
    if mos is not None:
        parts.append(f"margin of safety {mos:+.1%} against the computed intrinsic value")
    if quality is not None:
        parts.append(f"criteria-clean share over the ten-year window {quality:.0%}")
    parts.append(f"{snap.passed} of {snap.screened} screened names passed on the same date")
    return "; ".join(parts) + "."


def assess_events(
    chosen: Sequence[Event],
    summaries: dict[Event, str],
    *,
    conn: sqlite3.Connection,
    client: Any,
    budget: Budget | None = None,
    cache_dir: Any = None,
    model: str = ANALYST_MODEL,
    sections_for: Callable[..., Any] = filings.sections_for,
    assess: Callable[..., ValueAssessment] = value_analyst.assess,
) -> tuple[dict[Event, ValueAssessment], dict[Event, str]]:
    """Run tier 3 over the sampled events. Returns ``(assessments, failures)``.

    A failure is per-event and named — no filing on EDGAR, no CIK in the store, a
    response that would not parse. It leaves that event **unvetoed**, which is the
    conservative direction: an unread name enters the book exactly as it would
    have without tier 3, so a broken fetch cannot manufacture an effect. The
    count is reported, because an effect measured over 40 of 60 events is an
    effect over 40 events.

    ``BudgetExceeded`` is deliberately not caught. A cap that logs and continues
    is not a cap.
    """
    assessments: dict[Event, ValueAssessment] = {}
    failures: dict[Event, str] = {}
    for event in chosen:
        cik = db.cik_for(conn, event.ticker)
        if cik is None:
            failures[event] = "no CIK in the store for this ticker"
            continue
        try:
            _filing, sections = sections_for(client, cik, event.as_of)
            assessments[event] = assess(
                event.ticker,
                sections,
                summaries[event],
                model=model,
                budget=budget,
                cache_dir=cache_dir,
            )
        except (
            filings.FilingNotFound,
            client_module.SecRequestError,
            value_analyst.ValueAnalystError,
        ) as exc:
            # Narrow on purpose. A corrupt cache entry or a breached budget are
            # bugs in the apparatus, not facts about a company, and recording
            # them per event would hide them behind a coverage number.
            failures[event] = f"{type(exc).__name__}: {exc}"
    return assessments, failures


def vetoed(assessments: dict[Event, ValueAssessment], rule: str) -> frozenset[Event]:
    """The events the rule drops."""
    blocking = VETO_RULES[rule]
    return frozenset(event for event, a in assessments.items() if a.verdict in blocking)


def apply_veto(schedule: Schedule, veto: frozenset[Event]) -> Schedule:
    """The same schedule with vetoed (ticker, year) pairs removed from every date.

    Every date in the year, not only the date the event was sampled at: the
    verdict is about a filing, and the filing does not change between quarters.
    Vetoing the entry alone would let the name back in next quarter on the same
    unread-since evidence.
    """
    blocked = {(event.ticker, event.year) for event in veto}
    return [
        (as_of, tuple(t for t in names if (t, int(as_of[:4])) not in blocked))
        for as_of, names in schedule
    ]


def random_veto(
    candidates: Sequence[Event], count: int, rng: random.Random
) -> frozenset[Event]:
    """``count`` events vetoed at random — the skill-free arm."""
    if count <= 0:
        return frozenset()
    if count >= len(candidates):
        return frozenset(candidates)
    return frozenset(rng.sample(list(candidates), count))


def book_cut(baseline: Schedule, filtered: Schedule) -> float:
    """Share of held (date, name) slots the veto removed.

    The leverage number. Phase 4b step 6 found a 25% veto cutting 5% of the book
    under the quality exit, which is why that combination cannot answer anything:
    an entry filter over a book of incumbents barely touches what is held.
    """
    held = sum(len(names) for _, names in baseline)
    kept = sum(len(names) for _, names in filtered)
    return (held - kept) / held if held else 0.0


def paired_excess(
    baseline: portfolio.Result,
    other: portfolio.Result,
    dates: Sequence[str],
    *,
    samples: int,
    seed: int,
) -> stats.Interval | None:
    """CI for ``other``'s CAGR minus ``baseline``'s over the same rebalance periods.

    The unfiltered book stands where the benchmark stands in the phase-4 gate:
    the question is not whether either beats the market, it is whether the filter
    beat the thing it filtered.
    """
    ours, theirs = stats.paired_returns(other.curve, baseline.curve, list(dates))
    if not ours:
        return None
    excess, _ = stats.bootstrap(
        ours, theirs, years=other.years, samples=samples, seed=seed
    )
    return excess


def noise_floor(
    baseline: portfolio.Result,
    schedule: Schedule,
    candidates: Sequence[Event],
    count: int,
    simulate: Callable[[Schedule], portfolio.Result],
    dates: Sequence[str],
    *,
    reps: int,
    samples: int,
    seed: int,
) -> tuple[float, float] | None:
    """``(median point estimate, median CI upper bound)`` for a random veto.

    The upper bound is the minimum detectable effect: a real veto whose effect
    sits below it is indistinguishable from vetoing at random, whatever its
    point estimate says. Costs nothing but wall-clock — no tokens are spent here.
    """
    points, highs = [], []
    for rep in range(reps):
        rng = random.Random(seed + rep)
        arm = simulate(apply_veto(schedule, random_veto(candidates, count, rng)))
        excess = paired_excess(baseline, arm, dates, samples=samples, seed=seed + rep)
        if excess is None:
            continue
        points.append(excess.point)
        highs.append(excess.high)
    if not highs:
        return None
    return statistics.median(points), statistics.median(highs)


def judge(
    excess: stats.Interval | None, floor: tuple[float, float] | None
) -> stats.Verdict:
    """Apply ``CRITERION``. Both halves must hold."""
    reasons = []
    if excess is None:
        return stats.Verdict(
            passes=False,
            reasons=("too few priced rebalance periods to resample — no interval, and "
                     "therefore no verdict",),
        )
    if not excess.excludes_zero:
        reasons.append(
            f"the CI for the filtered book against the unfiltered one ({excess.text()}) "
            "contains zero — the veto is not separated from doing nothing"
        )
    elif excess.high < 0:
        reasons.append(
            f"the CI ({excess.text()}) is entirely below zero — the veto removed return"
        )
    if floor is None:
        reasons.append("the random-veto floor could not be measured, so no effect here "
                       "can be told apart from the apparatus")
    elif excess.point <= floor[1]:
        reasons.append(
            f"the effect {excess.point:+.2%} does not clear the noise floor "
            f"{floor[1]:+.2%} — a skill-free veto of the same size does as well"
        )
    return stats.Verdict(passes=not reasons, reasons=tuple(reasons))


def report(
    *,
    baseline: portfolio.Result,
    filtered: Arm,
    floor: tuple[float, float] | None,
    verdict: stats.Verdict,
    all_events: Sequence[Event],
    chosen: Sequence[Event],
    assessments: dict[Event, ValueAssessment],
    failures: dict[Event, str],
    veto: frozenset[Event],
    cut: float,
    spend_usd: float,
    start: str,
    end: str,
    setting: str,
    exit_rule: str,
    reps: int,
    seed: int,
    quiet: bool = False,
) -> list[str]:
    """The deliverable. Cost and coverage are part of the answer, not a footnote."""
    covered = len(chosen) / len(all_events) if all_events else 0.0
    lines = [
        f"phase 6 — sampled tier-3 backtest {start} -> {end}, at {setting}",
        "",
        f"events (ticker, fiscal year) the unfiltered book held: {len(all_events)}; "
        f"sampled {len(chosen)} ({covered:.0%}); assessed {len(assessments)}; "
        f"failed {len(failures)}",
        f"veto rule dropped {len(veto)} of {len(assessments)} assessed events, "
        f"cutting {cut:.1%} of held (date, name) slots",
        f"LLM spend this run: ${spend_usd:.2f} "
        "(cache hits cost nothing now but were paid for once)",
        "",
        CRITERION,
        "",
    ]
    base_cagr = "n/a" if baseline.cagr is None else f"{baseline.cagr:+.2%}"
    filt_cagr = "n/a" if filtered.result.cagr is None else f"{filtered.result.cagr:+.2%}"
    lines.extend([
        f"unfiltered book: CAGR {base_cagr}, max DD {baseline.max_drawdown:.1%}, "
        f"{baseline.trades} closed trades",
        f"filtered book:   CAGR {filt_cagr}, max DD {filtered.result.max_drawdown:.1%}, "
        f"{filtered.result.trades} closed trades",
    ])
    if filtered.excess is not None:
        lines.append(f"filtered minus unfiltered CAGR: {filtered.excess.text()}")
    if floor is None:
        lines.append("noise floor: not measurable on this run")
    else:
        point, high = floor
        lines.append(
            f"noise floor ({reps} random vetoes of the same size, seed {seed}): median "
            f"effect {point:+.2%}, median CI upper bound {high:+.2%} — the minimum "
            "detectable effect at this veto rate"
        )
    lines.append("")
    lines.append(
        f"VERDICT: tier 3 {'earns' if verdict.passes else 'does not earn'} its cost "
        "against the pre-registered criterion"
    )
    lines.extend(f"  - {reason}" for reason in verdict.reasons)

    lines.append("")
    lines.extend(_caveats(exit_rule, covered, failures, cut))

    if not quiet and assessments:
        lines.append("")
        lines.append("assessments, most recent first:")
        for event in sorted(assessments, key=lambda e: (e.as_of, e.ticker), reverse=True):
            a = assessments[event]
            mark = "VETO" if event in veto else "keep"
            lines.append(
                f"  {event.as_of} {event.ticker:<6} {mark}  {a.verdict.value:<8} "
                f"moat {a.moat.value}/{a.moat_trend.value}, confidence {a.confidence.value}"
                + (f", flags: {'; '.join(a.accounting_flags)}" if a.accounting_flags else "")
            )
    if failures:
        lines.append("")
        lines.append(f"events that could not be assessed ({len(failures)}), left unvetoed:")
        for event, why in sorted(failures.items(), key=lambda p: (p[0].as_of, p[0].ticker)):
            lines.append(f"  {event.as_of} {event.ticker}: {why}")
    return lines


def _caveats(exit_rule: str, covered: float, failures: dict, cut: float) -> list[str]:
    """What this measurement cannot see, stated rather than buried."""
    lines = ["caveats — read before believing the verdict above:"]
    if exit_rule == "quality":
        lines.append(
            "  LEVERAGE: run under the quality exit, where incumbents carry forward and an "
            "entry-time veto barely changes what is held. Phase 4b step 6 measured a 25% "
            "veto cutting 5% of the book here, below the noise floor even for a perfect "
            "veto. Re-run with --exit rebalance before reading anything into this number."
        )
    if covered < 1.0:
        lines.append(
            f"  coverage: {covered:.0%} of events were sampled. Unsampled names entered the "
            "book unread, so the measured effect is the effect of vetoing that share, not "
            "of vetoing everything. Raise --sample to close the gap — it costs tokens."
        )
    if failures:
        lines.append(
            f"  {len(failures)} events failed to assess and were left in the book. That "
            "biases the effect toward zero, never away from it."
        )
    lines.extend([
        f"  the veto is applied at the filing, so it removes {cut:.1%} of held slots and "
        "never times an exit. A tier 3 that could also sell would measure higher than this.",
        "  restatements: the numeric summary in each prompt comes from companyfacts, which "
        "serves restated figures. The filing text itself is as filed.",
        "  the analyst's own horizon is five to ten years and this window is twelve, so a "
        "single verdict is judged against roughly one horizon of evidence.",
    ])
    return lines


def _picks(select: str, level: float) -> Callable[[numeric.Snapshot], tuple[str, ...]]:
    if select == "rank":
        return lambda snap: snap.top_ranked(int(level))
    return lambda snap: snap.triggered(level)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default=f"{date.today().year - 10}-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--tickers", help="comma-separated; narrows whatever the "
                        "universe yields, for a cheap smoke run")
    parser.add_argument("--universe", choices=("index", "store"), default="index",
                        help="index: point-in-time S&P 500 membership per date (default)")
    parser.add_argument("--membership", default=None, help="membership CSV override")
    parser.add_argument("--years", type=int, default=HISTORY_YEARS)
    parser.add_argument("--tolerance", type=int, default=VIOLATION_TOLERANCE)
    parser.add_argument("--select", choices=("trigger", "rank"), default="trigger",
                        help="which numeric selection the veto sits on top of")
    parser.add_argument("--construct", choices=("equal", "capped"), default="equal")
    parser.add_argument(
        "--exit", dest="exit_rule", choices=("rebalance", "quality"), default="rebalance",
        help="rebalance (default): the book is rebuilt each date, so an entry veto has "
             "leverage. quality: incumbents carry forward — phase 4b step 6 showed the "
             "veto then changes almost nothing, and the report says so",
    )
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                        help="events to assess; 0 assesses every one of them")
    parser.add_argument("--veto", choices=tuple(VETO_RULES), default="avoid")
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS,
                        help="random-veto repetitions behind the noise floor")
    parser.add_argument("--bootstrap", type=int, default=stats.BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model", default=ANALYST_MODEL)
    parser.add_argument("--budget", type=float, default=RUN_BUDGET_USD,
                        help="USD ceiling for this run; it fails closed when breached")
    parser.add_argument("--dry-run", action="store_true",
                        help="count the events and project the spend; call nothing")
    parser.add_argument("--benchmark", default=BACKTEST_BENCHMARK,
                        help="not traded — it supplies the calendar both books run on")
    parser.add_argument("--cash", type=float, default=BACKTEST_START_CASH)
    parser.add_argument("--commission", type=float, default=BACKTEST_COMMISSION)
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--db", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    dates = numeric.quarter_ends(args.start, args.end)
    if not dates:
        print(f"error: no quarter end between {args.start} and {args.end}", file=sys.stderr)
        return 2

    try:
        universe, notes = numeric.resolve_universe(
            args.universe, dates, membership_path=args.membership, db_path=args.db
        )
    except numeric.UniverseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(note)

    lookback = f"{date.fromisoformat(args.start).year - args.years}-01-01"
    history = History(lookback, args.end, interval=args.interval)
    try:
        clock = history.frame(args.benchmark)
    except PriceError as exc:
        print(f"error: benchmark {args.benchmark}: {exc}", file=sys.stderr)
        return 2

    conn = db.connect(args.db)
    try:
        snaps = numeric.snapshots(
            conn, dates, history,
            tickers=[t.strip().upper() for t in args.tickers.split(",")] if args.tickers
            else None,
            universe=universe, years=args.years, tolerance=args.tolerance,
        )
    finally:
        conn.close()

    live = [snap for snap in snaps if not snap.error]
    if not any(snap.valued for snap in live):
        print("no name reached a valuation on any rebalance date — nothing to filter",
              file=sys.stderr)
        return 1

    ranked = args.select == "rank"
    level = float(BACKTEST_TOP_N) if ranked else MARGIN_OF_SAFETY_MIN
    capped = args.construct == "capped"
    baseline_schedule = numeric.construct(
        live, _picks(args.select, level),
        minimum=BACKTEST_MIN_POSITIONS if capped else 0,
        maximum=BACKTEST_MAX_POSITIONS if capped else len(live) + 1,
        quality_exit=args.exit_rule == "quality",
    )

    all_events = events(baseline_schedule)
    if not all_events:
        print("the unfiltered book never held anything — nothing to veto", file=sys.stderr)
        return 1
    chosen = sample(all_events, args.sample, args.seed)

    if args.dry_run:
        each = budget_module.cost_usd(args.model, EST_PROMPT_TOKENS, EST_COMPLETION_TOKENS)
        print(f"{len(all_events)} events held; {len(chosen)} would be assessed with "
              f"{args.model}")
        print(f"projected worst case: {len(chosen)} x ${each:.3f} = "
              f"${each * len(chosen):.2f} (cached events cost nothing to repeat), "
              f"against a --budget of ${args.budget:.2f}")
        for event in chosen:
            print(f"  {event.as_of} {event.ticker} FY{event.year}")
        return 0

    by_date = {snap.as_of: snap for snap in live}
    summaries = {
        event: numeric_summary(by_date[event.as_of], event.ticker) for event in chosen
    }
    budget = Budget(run_cap_usd=args.budget)
    conn = db.connect(args.db)
    try:
        assessments, failures = assess_events(
            chosen, summaries, conn=conn, client=SecClient(),
            budget=budget, model=args.model,
        )
    except budget_module.BudgetError as exc:
        # Fail closed and say what was already spent: the cache keeps the paid
        # answers, so a re-run with a wider cap resumes rather than re-buys.
        print(f"error: {exc}. Assessments already made are cached; widen --budget to "
              "resume without paying for them again.", file=sys.stderr)
        return 2
    finally:
        conn.close()

    veto = vetoed(assessments, args.veto)
    filtered_schedule = apply_veto(baseline_schedule, veto)

    frames = {t: history.frame(t) for t in history.fetched}

    def run(schedule: Schedule) -> portfolio.Result:
        return portfolio.simulate(
            frames, schedule, clock,
            start=args.start, end=args.end,
            start_cash=args.cash, commission=args.commission,
            position_cap=BACKTEST_POSITION_CAP if capped else 1.0,
        )

    rebalance_dates = [snap.as_of for snap in live]
    baseline = run(baseline_schedule)
    filtered = run(filtered_schedule)
    excess = paired_excess(
        baseline, filtered, rebalance_dates, samples=args.bootstrap, seed=args.seed
    )
    floor = noise_floor(
        baseline, baseline_schedule, chosen, len(veto), run, rebalance_dates,
        reps=args.reps, samples=args.bootstrap, seed=args.seed,
    ) if veto else None

    print("\n".join(report(
        baseline=baseline,
        filtered=Arm("filtered", filtered, excess),
        floor=floor,
        verdict=judge(excess, floor),
        all_events=all_events,
        chosen=chosen,
        assessments=assessments,
        failures=failures,
        veto=veto,
        cut=book_cut(baseline_schedule, filtered_schedule),
        spend_usd=budget.run_spend_usd,
        start=args.start,
        end=args.end,
        setting=(f"top {int(level)} by quality rank" if ranked
                 else f"the configured trigger {level:.0%}"),
        exit_rule=args.exit_rule,
        reps=args.reps,
        seed=args.seed,
        quiet=args.quiet,
    )))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
