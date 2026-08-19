"""Statistical power for the replay: intervals, one pre-registered criterion, and
a bound on the part of the universe that cannot be priced at all.

Phase 4b step 3 exists because three phase-4 verdicts in a row were read off a
grid that could not resolve the effect being looked for. 24 closed trades cannot
rank adjacent trigger levels, and the non-monotonic grid *is* the interval. So:

* every headline number carries a bootstrap interval over resampled rebalance
  dates, rather than standing alone;
* the pass criterion is fixed in the plan **before** the run and printed here
  verbatim, so no cell can be chosen after the fact;
* the residual survivorship hole — index members with no price series, which
  step 2b established is unrepairable on free data — is swept over three stub
  terminal returns instead of being waved at in a caveat.

Resampling is over rebalance periods, paired strategy-and-benchmark, with
replacement. That destroys the ordering, which is exactly the null being tested:
if the excess return survives reshuffling the quarters it did not come from one.
"""

import random
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

BOOTSTRAP_SAMPLES = 2000
CONFIDENCE = 0.95

# Total loss, dead money, a typical acquisition premium. The three ways an
# unpriceable position could have ended, per phase 4b step 3.
STUB_RETURNS = (-1.0, 0.0, 0.25)

# Pre-registered in .claude/plans/phase4b-clean-universe.md before the run.
# Reproduced here so the report cannot cite a criterion that was edited later.
CRITERION = (
    "pre-registered pass criterion (phase 4b step 3): the gate passes only if the "
    "bootstrap CI for CAGR vs the benchmark is entirely above zero AND max drawdown does "
    "not exceed the benchmark's — both at the configured settings, never at a best-of-grid "
    "cell. The trigger grid below is a sensitivity display, not evidence."
)

Curve = Sequence[tuple[str, float]]


@dataclass(frozen=True)
class Interval:
    """A point estimate and the interval the resamples put around it."""

    point: float
    low: float
    high: float

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0.0 or self.high < 0.0

    def text(self, *, sign: bool = True) -> str:
        fmt = "+.2%" if sign else ".1%"
        return (
            f"{self.point:{fmt}} "
            f"[{self.low:{fmt}}, {self.high:{fmt}}]"
        )


@dataclass(frozen=True)
class Verdict:
    passes: bool
    reasons: tuple[str, ...]


def curve_of(frame: Any, column: str = "Close") -> tuple[tuple[str, float], ...]:
    """A price frame as ``(date, value)`` pairs — the shape everything here reads."""
    return tuple(
        (day.isoformat(), float(value))
        for day, value in zip(frame.index.date, frame[column], strict=False)
    )


def value_at(curve: Curve, day: str) -> float | None:
    """The value at or before ``day``, or None if the curve starts after it.

    At or before, never the nearest: the same look-ahead rule the fundamentals
    path enforces. A date the curve cannot answer is dropped rather than
    interpolated, because an invented equity value reads as a real quarter.
    """
    days = [d for d, _ in curve]
    index = bisect_right(days, day) - 1
    return None if index < 0 else curve[index][1]


def usable_dates(strategy: Curve, benchmark: Curve, dates: Sequence[str]) -> list[str]:
    """The rebalance dates both curves can price."""
    return [
        day for day in dates
        if value_at(strategy, day) is not None and value_at(benchmark, day) is not None
    ]


def paired_returns(
    strategy: Curve, benchmark: Curve, dates: Sequence[str]
) -> tuple[list[float], list[float]]:
    """Period returns of both curves over the dates both can price."""
    kept = usable_dates(strategy, benchmark, dates)
    ours = [value_at(strategy, day) for day in kept]
    theirs = [value_at(benchmark, day) for day in kept]
    return _returns(ours), _returns(theirs)


def _returns(values: Sequence[float]) -> list[float]:
    return [
        values[i] / values[i - 1] - 1
        for i in range(1, len(values))
        if values[i - 1] > 0
    ]


def cagr(returns: Sequence[float], years: float) -> float:
    """Annualised return of a compounded period series.

    A wipeout returns −1.0 rather than raising: with a −100% stub in the sweep it
    is a real outcome, not a degenerate input.
    """
    if years <= 0:
        return 0.0
    total = 1.0
    for r in returns:
        total *= 1 + r
    if total <= 0:
        return -1.0
    return total ** (1 / years) - 1


def max_drawdown(returns: Sequence[float]) -> float:
    """Deepest peak-to-trough fall of the compounded path, as a positive share."""
    value, peak, worst = 1.0, 1.0, 0.0
    for r in returns:
        value *= 1 + r
        peak = max(peak, value)
        worst = max(worst, 0.0 if peak <= 0 else 1 - value / peak)
    return worst


def bootstrap(
    strategy: Sequence[float],
    benchmark: Sequence[float],
    *,
    years: float,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = 0,
    confidence: float = CONFIDENCE,
) -> tuple[Interval | None, Interval | None]:
    """Intervals for excess CAGR and for max drawdown, or ``(None, None)``.

    None when there are too few periods to resample. Printing an interval from
    two quarters would be the phase-4 error in a new costume.
    """
    n = min(len(strategy), len(benchmark))
    if n < 4 or samples < 2:
        return None, None

    rng = random.Random(seed)
    excesses, drawdowns = [], []
    for _ in range(samples):
        picks = [rng.randrange(n) for _ in range(n)]
        ours = [strategy[i] for i in picks]
        theirs = [benchmark[i] for i in picks]
        excesses.append(cagr(ours, years) - cagr(theirs, years))
        drawdowns.append(max_drawdown(ours))

    tail = (1 - confidence) / 2
    return (
        Interval(
            point=cagr(strategy, years) - cagr(benchmark, years),
            low=_percentile(excesses, tail),
            high=_percentile(excesses, 1 - tail),
        ),
        Interval(
            point=max_drawdown(strategy),
            low=_percentile(drawdowns, tail),
            high=_percentile(drawdowns, 1 - tail),
        ),
    )


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    index = int(round(q * (len(ordered) - 1)))
    return ordered[min(max(index, 0), len(ordered) - 1)]


def judge(excess: Interval, *, drawdown: float, benchmark_drawdown: float) -> Verdict:
    """Apply ``CRITERION``. Both halves must hold; neither is negotiable here."""
    reasons = []
    if not excess.excludes_zero:
        reasons.append(
            f"the CI for CAGR vs the benchmark ({excess.text()}) contains zero — "
            "the excess is not separated from noise"
        )
    elif excess.low <= 0:
        reasons.append(
            f"the CI for CAGR vs the benchmark ({excess.text()}) is below zero — "
            "the strategy trails the benchmark"
        )
    if drawdown > benchmark_drawdown:
        reasons.append(
            f"max drawdown {drawdown:.1%} exceeds the benchmark's {benchmark_drawdown:.1%} — "
            "the screen fails on the axis it exists to win"
        )
    return Verdict(passes=not reasons, reasons=tuple(reasons))


def phantom_weight(snap: Any, held: int) -> float:
    """Share of one rebalance's holdings that unpriceable names would have taken.

    Two populations, both invisible in the returns:

    * names that **passed** the criteria and then had no price series — known
      passers, counted as ``passed - valued``;
    * index members the store holds no facts for at all — unknowable, so
      estimated at the pass rate the priced population showed on the same date.

    Both are then scaled by the observed selection rate — ``held`` out of the
    priced names that qualified — since a name with no price can be neither
    checked against the margin-of-safety trigger nor ranked against the others.
    That assumption is the bound's one soft edge and is stated in the report.

    The rate is capped at 1.0. Under step 5's quality exit the book carries
    incumbents forward, so ``held`` counts names selected on *earlier* dates and
    can exceed the names valued today — an uncapped ratio then claims more
    phantom positions than there are phantom names, inflating the very bound it
    is meant to make honest. The cap is a no-op for the trigger and rank
    schedules, where the book is always a subset of ``valued``, so no earlier
    verdict moves under it.
    """
    valued = len(snap.valued)
    unpriceable = max(snap.passed - valued, 0)
    if snap.screened:
        unpriceable += snap.absent * snap.passed / snap.screened
    selection_rate = min(held / valued, 1.0) if valued else 0.0
    phantom = unpriceable * selection_rate
    total = held + phantom
    return phantom / total if total else 0.0


def blend(
    returns: Sequence[float],
    weights: Sequence[float],
    stub: float,
    *,
    holding_periods: float = 1.0,
) -> list[float]:
    """Period returns as they would read if the phantom share earned ``stub``.

    ``stub`` is a **terminal** return — what the position was ultimately worth —
    and a position is held ``holding_periods`` rebalance periods, not one. So it
    is amortised across the holding span before being blended in. Charging it
    every period instead is a 4x error in either direction: "+25% once per 1.1
    years" becomes +25% per quarter, and "goes to zero" becomes goes to zero
    four times over.

    Amortisation is arithmetic, not geometric, because a total loss has no
    geometric per-period equivalent — ``(1-1)**(1/k)`` is 0 for any k, i.e. a
    wipeout every period. Linear decay to zero over the holding span is the
    honest reading of "this position ended at zero".
    """
    span = max(holding_periods, 1.0)
    per_period = stub / span
    return [
        (1 - w) * r + w * per_period
        for r, w in zip(returns, weights, strict=False)
    ]


def sweep(
    strategy: Sequence[float],
    benchmark: Sequence[float],
    weights: Sequence[float],
    *,
    years: float,
    holding_periods: float = 1.0,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = 0,
) -> list[tuple[float, Interval | None, Interval | None]]:
    """One (stub, excess, drawdown) row per stub terminal return."""
    return [
        (stub, *bootstrap(
            blend(strategy, weights, stub, holding_periods=holding_periods), benchmark,
            years=years, samples=samples, seed=seed,
        ))
        for stub in STUB_RETURNS
    ]


def summary(
    snaps: Sequence[Any],
    curve: Curve,
    benchmark_curve: Curve,
    *,
    held: dict[str, int],
    setting: str,
    years: float,
    benchmark: str,
    bars_held: float = 0.0,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = 0,
) -> list[str]:
    """The step-3 block of the report: intervals, verdict, residual sweep."""
    dates = [snap.as_of for snap in snaps]
    kept = usable_dates(curve, benchmark_curve, dates)
    ours, theirs = paired_returns(curve, benchmark_curve, dates)
    lines = [CRITERION, ""]
    if samples < 2:
        return lines + ["bootstrap disabled (--bootstrap 0) — no interval, and "
                        "therefore no verdict."]
    if not ours:
        return lines + ["too few priced rebalance periods to resample — no interval, "
                        "and therefore no verdict."]

    excess, drawdown = bootstrap(ours, theirs, years=years, samples=samples, seed=seed)
    if excess is None or drawdown is None:
        return lines + [
            f"too few rebalance periods ({len(ours)}) to resample — no interval, and "
            "therefore no verdict. Widen the window."
        ]

    bench_drawdown = max_drawdown(theirs)
    lines.extend([
        f"bootstrap over {len(ours)} rebalance periods, {samples} resamples, "
        f"{CONFIDENCE:.0%} CI, at {setting}:",
        f"  CAGR vs {benchmark}: {excess.text()}",
        f"  max drawdown:       {drawdown.text(sign=False)}  "
        f"(vs {benchmark} {bench_drawdown:.1%} over the same periods)",
    ])
    verdict = judge(excess, drawdown=drawdown.point, benchmark_drawdown=bench_drawdown)
    lines.append("")
    lines.append(f"VERDICT: {'pass' if verdict.passes else 'fail'} against the "
                 "pre-registered criterion")
    lines.extend(f"  - {reason}" for reason in verdict.reasons)

    by_date = {snap.as_of: snap for snap in snaps}
    weights = [
        phantom_weight(by_date[day], held.get(day, 0)) for day in kept[:-1]
    ][: len(ours)]
    bars_per_period = len(curve) / len(ours) if ours else 1.0
    span = max(bars_held / bars_per_period, 1.0) if bars_per_period else 1.0
    lines.append("")
    lines.append(
        "residual bound — the unpriceable half of the universe, swept rather than "
        f"caveated. Per rebalance it would have been {_mean(weights):.1%} of NAV "
        "(passers with no price series, plus members absent from the store estimated "
        "at the observed pass and selection rates). Each stub is a terminal return, "
        f"amortised over the {span:.1f}-period average holding span. Verdict at each "
        "assumed terminal return for that share:"
    )
    for stub, stub_excess, stub_drawdown in sweep(
        ours, theirs, weights,
        years=years, holding_periods=span, samples=samples, seed=seed,
    ):
        if stub_excess is None or stub_drawdown is None:
            continue
        stub_verdict = judge(
            stub_excess, drawdown=stub_drawdown.point, benchmark_drawdown=bench_drawdown
        )
        lines.append(
            f"  stub {stub:+.0%}: CAGR vs {benchmark} {stub_excess.text()}, "
            f"max DD {stub_drawdown.point:.1%} -> "
            f"{'pass' if stub_verdict.passes else 'fail'}"
        )
    lines.append(
        "  If the verdict holds at all three, the residual does not change the answer "
        "and is closed. If it flips, the honest result is that interval, not a number."
    )
    return lines


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
