"""Composing what gets sent. Pure — no network, no store, no clock.

Two shapes, deliberately unalike so the eye can tell them apart in a phone's
notification list: the heartbeat is one line, a trigger alert is a block. If they
ever start to look the same, make the heartbeat quieter — never the alert louder.

The filing read is a briefing, not a gate. Phase 6 measured the gate on 44
filings and re-measured the same 44 after fixing the section extractor: zero
``avoid`` both times, and ``caution`` on 31 of 44 after the fix — 70% of this
screen's picks land on one label, which carries almost no information. What the
read did produce that was specific and checkable were the flags, risks and gaps —
so those lead, and ``verdict`` renders last with the 70% beside it.
"""

from ..analyst.schemas import ValueAssessment
from ..config import (
    BACKTEST_MIN_POSITIONS,
    BACKTEST_POSITION_CAP,
    MARGIN_OF_SAFETY_MIN,
)
from ..screen.runner import Outcome

# Phase 4b step 5. Stated as what it is — a measurement over 2014-2026 of a
# strategy that did not beat SPY — because a bare "hold 15%" would read as advice.
SIZING_NOTE = (
    f"Measured in backtest, not advice: {BACKTEST_POSITION_CAP:.0%} max per name, "
    f"{BACKTEST_MIN_POSITIONS} names minimum. Phase 4b step 5 — that cap alone cut max "
    "drawdown 48.6% to 18.9% without costing CAGR. Same backtest did not beat SPY."
)

DISCLAIMER = "Evidence for your decision. Not a recommendation, not an order, not sized."


def briefing(assessment: ValueAssessment) -> list[str]:
    """The filing read, most informative field first, verdict demoted to last."""
    lines = []
    for title, items in (
        ("accounting flags", assessment.accounting_flags),
        ("key risks", assessment.key_risks),
        ("evidence gaps", assessment.evidence_gaps),
    ):
        lines.append(f"{title}: {'; '.join(items) if items else 'none stated'}")
    lines.append(f"thesis: {assessment.thesis}")
    lines.append(
        f"moat {assessment.moat.value}, trend {assessment.moat_trend.value} | "
        f"customer concentration {assessment.customer_concentration.value} | "
        f"confidence {assessment.confidence.value}"
    )
    lines.append(
        f"verdict: {assessment.verdict.value} — phase 6: this label covers 70% of "
        "this screen's picks, so read the lines above it, not this one"
    )
    return lines


def trigger_alert(
    outcome: Outcome,
    trigger_date: str,
    assessment: ValueAssessment | None = None,
    note: str = "",
) -> str:
    """One name has reached the margin-of-safety trigger. What the operator sees."""
    valuation = outcome.valuation
    if valuation is None:  # pragma: no cover - triggered implies a valuation
        raise ValueError(f"{outcome.ticker} has no valuation; nothing to alert about")

    lines = [
        f"{outcome.ticker} at {MARGIN_OF_SAFETY_MIN:.0%} MoS — {trigger_date}",
        f"price {valuation.price:,.2f} vs intrinsic {valuation.intrinsic_value:,.2f} "
        f"(MoS {valuation.margin_of_safety:+.1%})",
    ]
    if outcome.screen is not None:
        blocking = [c for c in outcome.screen.criteria if c.blocking]
        passed = sum(1 for c in blocking if c.passed)
        lines.append(
            f"screen: {'PASSED' if outcome.screen.passed else 'FAILED'} "
            f"{passed}/{len(blocking)} blocking, criteria-clean {outcome.screen.quality:.0%}"
        )
    if valuation.graham_disagrees:
        lines.append("[!] Graham number and the DCF differ by more than 3x — read the inputs")

    lines.append("")
    if assessment is not None:
        lines.extend(briefing(assessment))
    else:
        lines.append(f"filing: not read{f' — {note}' if note else ''}")

    lines.extend(
        [
            "",
            SIZING_NOTE,
            DISCLAIMER,
            f"Full dossier: python -m tradingagents.value.report --ticker {outcome.ticker}",
            f"Log what you decide: python -m tradingagents.value.decisions record "
            f"--ticker {outcome.ticker} --action <buy|pass|watch> --why '...'",
        ]
    )
    return "\n".join(lines)


def heartbeat(outcomes: list[Outcome], as_of: str, notes: list[str] | None = None) -> str:
    """Proof the cron is alive. Mandatory: silence is this screen's normal state.

    A dead job and a quiet market produce exactly the same inbox, which is why
    this goes out on days when nothing happened — those are most days.
    """
    screened = [o for o in outcomes if not o.excluded]
    passed = [o for o in screened if o.passed]
    triggered = [o for o in passed if o.triggered]
    valued = [o for o in passed if o.valuation is not None]

    line = (
        f"{as_of} ok — screened {len(screened)}, passed {len(passed)}, "
        f"triggered {len(triggered)}"
    )
    if valued:
        best = max(valued, key=lambda o: o.valuation.margin_of_safety)
        line += f", closest {best.ticker} {best.valuation.margin_of_safety:+.1%} MoS"
    for note in notes or []:
        line += f" | {note}"
    return line
