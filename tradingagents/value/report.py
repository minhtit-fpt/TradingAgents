"""One company, everything the screen knows, and the price the trigger fires at.

    python -m tradingagents.value.report --ticker PG
    python -m tradingagents.value.report --ticker PG --as-of 2024-06-30 --read-filing

Phases 4b and 6 closed the automated path: the numeric strategy does not beat
SPY, and an LLM veto applied at entry cannot be separated from a random veto of
the same size. Both failures are about *automation deciding when to buy*, so
this module changes who decides. Subsystems 1 and 2 already work this way — the
operator names a ticker and a date, the machine assembles evidence, the operator
decides. Nothing here alerts, sizes, or executes.

The verdict alone would not be evidence, so every criterion reports the years
that failed it and the values it saw. Section 3 is the phase's actual answer: the
price at which the margin of safety reaches the trigger, which is a number the
operator can act on rather than an alert to wait for.

The 10-K read is opt-in and priced (``--read-filing``, ~$0.05). It is reading
material, never a veto: phase 6 measured the veto and it does not earn its cost.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date

from .analyst import value_analyst
from .analyst.schemas import ValueAssessment
from .config import (
    ANALYST_MODEL,
    HISTORY_YEARS,
    MARGIN_OF_SAFETY_MIN,
    RUN_BUDGET_USD,
    VIOLATION_TOLERANCE,
)
from .edgar import filings, tickermap
from .edgar.client import SecClient, SecRequestError
from .jobs.bootstrap import ingest
from .llm.budget import Budget, BudgetError
from .screen import market
from .screen.runner import Outcome, screen_one
from .store import db


@dataclass(frozen=True)
class Dossier:
    """Assembled evidence about one name on one date. Rendering is separate."""

    ticker: str
    as_of: str
    outcome: Outcome
    assessment: ValueAssessment | None = None
    filing_label: str = ""
    filing_url: str = ""
    sections_missing: tuple[str, ...] = ()
    spend_usd: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def trigger_price(self) -> float | None:
        """Price at which the margin of safety reaches the configured trigger."""
        valuation = self.outcome.valuation
        if valuation is None:
            return None
        return valuation.intrinsic_value * (1 - MARGIN_OF_SAFETY_MIN)


def numeric_summary(outcome: Outcome) -> str:
    """What the numbers concluded, in one line, for the analyst to argue with."""
    screen = outcome.screen
    if screen is None:
        return f"{outcome.ticker} was not screened."
    verdict = "cleared" if screen.passed else "failed"
    parts = [
        f"{outcome.ticker} {verdict} the thirteen-criterion screen",
        f"criteria-clean share over the {screen.years_required}-year window "
        f"{screen.quality:.0%}",
    ]
    if not screen.passed:
        parts.append("failed on " + ", ".join(screen.failed_criteria))
    if outcome.valuation is not None:
        parts.append(
            f"margin of safety {outcome.valuation.margin_of_safety:+.1%} against the "
            "computed intrinsic value"
        )
    return "; ".join(parts) + "."


def ensure_facts(conn: sqlite3.Connection, ticker: str, *, client=None) -> str:
    """Ingest the company once if the store has never seen it. Returns a note.

    A dossier for a name that was never bootstrapped is otherwise a dead end, and
    one companyfacts fetch is the whole difference. Nothing is guessed: no CIK in
    either SEC ticker file raises rather than screening an empty history.
    """
    if db.cik_for(conn, ticker) is not None:
        return ""
    client = client or SecClient()
    cik = tickermap.load(client).get(ticker)
    if cik is None:
        raise LookupError(f"no CIK in either SEC ticker file for {ticker}")
    rows = ingest(conn, client, ticker, cik)
    return f"first look at {ticker}: ingested {rows} annual facts from EDGAR"


def read_filing(
    conn: sqlite3.Connection,
    outcome: Outcome,
    as_of: str,
    *,
    client=None,
    budget: Budget | None = None,
    llm=None,
    model: str = ANALYST_MODEL,
) -> tuple[ValueAssessment | None, str, str, tuple[str, ...], str]:
    """Read Items 1/1A/7 of the newest 10-K filed on or before ``as_of``.

    Returns ``(assessment, label, url, missing_sections, note)``. A filing that
    cannot be reached or read yields a note rather than a substituted verdict —
    an absent read is information; an invented one is not.
    """
    cik = db.cik_for(conn, outcome.ticker)
    if cik is None:
        return None, "", "", (), f"no CIK on file for {outcome.ticker}; filing not read"
    try:
        filing, sections = filings.sections_for(client or SecClient(), cik, as_of)
    except (filings.FilingNotFound, SecRequestError, ValueError) as exc:
        return None, "", "", (), f"filing not read: {exc}"

    label = f"10-K filed {filing.filed} (accession {filing.accession})"
    try:
        assessment = value_analyst.assess(
            outcome.ticker,
            sections,
            numeric_summary(outcome),
            llm=llm,
            model=model,
            budget=budget,
        )
    except (value_analyst.ValueAnalystError, BudgetError) as exc:
        return None, label, filing.url, sections.missing, f"filing not assessed: {exc}"
    return assessment, label, filing.url, sections.missing, ""


def build(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    *,
    years: int = HISTORY_YEARS,
    tolerance: int = VIOLATION_TOLERANCE,
    discount_rate: float | None = None,
    prices=market,
    with_filing: bool = False,
    client=None,
    budget: Budget | None = None,
    llm=None,
    model: str = ANALYST_MODEL,
    ingest_missing: bool = True,
) -> Dossier:
    """Assemble the dossier. No printing, no ordering, no decision."""
    notes: list[str] = []
    if ingest_missing:
        note = ensure_facts(conn, ticker, client=client)
        if note:
            notes.append(note)

    rate = discount_rate if discount_rate is not None else prices.risk_free_rate(as_of)
    outcome = screen_one(
        conn, ticker, as_of, rate, years=years, tolerance=tolerance,
        prices=prices, record=False,
    )

    assessment = None
    label = url = ""
    missing: tuple[str, ...] = ()
    if with_filing:
        assessment, label, url, missing, note = read_filing(
            conn, outcome, as_of, client=client, budget=budget, llm=llm, model=model
        )
        if note:
            notes.append(note)

    return Dossier(
        ticker=ticker,
        as_of=as_of,
        outcome=outcome,
        assessment=assessment,
        filing_label=label,
        filing_url=url,
        sections_missing=missing,
        spend_usd=budget.run_spend_usd if budget is not None else 0.0,
        notes=tuple(notes),
    )


def _criterion_line(criterion) -> str:
    latest = f"{criterion.values[-1][1]:,.2f}" if criterion.values else "—"
    marker = "pass" if criterion.passed else "FAIL"
    detail = []
    if criterion.violation_years:
        detail.append("violated " + ", ".join(str(y) for y in criterion.violation_years))
    if criterion.missing_years:
        detail.append("missing " + ", ".join(str(y) for y in criterion.missing_years))
    if not detail:
        detail.append("clean")
    advisory = "" if criterion.blocking else " (advisory)"
    return (
        f"    [{marker}] {criterion.number:>2} {criterion.name:<26} "
        f"latest {latest:>10}  {'; '.join(detail)}{advisory}"
    )


def render(dossier: Dossier) -> list[str]:
    """The deliverable. Every line says what it is; none of them says to buy."""
    outcome = dossier.outcome
    lines = [
        f"{dossier.ticker} — value dossier as of {dossier.as_of}",
        "Evidence for your decision. Not a recommendation, not an order, not sized.",
    ]
    lines.extend(f"note: {n}" for n in dossier.notes)

    lines.append("")
    lines.append("1. QUALITY — the thirteen criteria")
    if outcome.excluded:
        lines.append(f"    excluded from the screen — {outcome.excluded}")
    elif outcome.screen is None:
        lines.append("    not screened")
    else:
        screen = outcome.screen
        blocking = [c for c in screen.criteria if c.blocking]
        passed = sum(1 for c in blocking if c.passed)
        lines.append(
            f"    {'PASSED' if screen.passed else 'FAILED'} — {passed}/{len(blocking)} "
            f"blocking criteria, {len(screen.years)}/{screen.years_required} years, "
            f"tolerance {VIOLATION_TOLERANCE} violation-years, "
            f"criteria-clean share {screen.quality:.0%}"
        )
        lines.extend(_criterion_line(c) for c in screen.criteria)

    lines.append("")
    lines.append("2. PRICE — intrinsic value against the market")
    valuation = outcome.valuation
    if valuation is None:
        lines.append(f"    not valued — {outcome.error or 'the screen stopped earlier'}")
    else:
        lines.append(
            f"    price {valuation.price:,.2f} vs intrinsic {valuation.intrinsic_value:,.2f}"
            f"  →  MoS {valuation.margin_of_safety:+.1%} (trigger {MARGIN_OF_SAFETY_MIN:.0%})"
        )
        lines.append(
            f"    inputs: growth {valuation.growth_rate:.1%}"
            f"{' (capped)' if valuation.growth_capped else ''}, "
            f"discount {valuation.discount_rate:.2%}"
            f"{' (floored)' if valuation.discount_floored else ''}, "
            f"terminal PE {valuation.terminal_pe:.1f}, "
            f"dividends {valuation.dividend_value:,.2f} of the value"
        )
        graham = (
            f"{valuation.graham_number:,.2f}" if valuation.graham_number else "not computable"
        )
        owner = (
            f"{valuation.owner_earnings_per_share:,.2f}"
            if valuation.owner_earnings_per_share
            else "not computable"
        )
        lines.append(f"    cross-checks: Graham number {graham}, owner earnings/share {owner}")
        if valuation.graham_disagrees:
            lines.append("    [!] the two methods differ by more than 3x — read the inputs")

    lines.append("")
    lines.append("3. ENTRY — where the trigger would fire")
    trigger = dossier.trigger_price
    if trigger is None or valuation is None:
        lines.append("    no valuation, so no trigger price")
    else:
        away = trigger / valuation.price - 1
        lines.append(
            f"    {MARGIN_OF_SAFETY_MIN:.0%} MoS needs price <= {trigger:,.2f}; "
            f"today {valuation.price:,.2f} ({away:+.1%} away)"
        )
        lines.append(
            "    Not a target price: it is where *this* model's discount reaches the "
            "trigger, and phase 4b showed the model does not beat SPY. You decide."
        )

    lines.append("")
    lines.append("4. FILING — Items 1, 1A and 7")
    assessment = dossier.assessment
    if not dossier.filing_label and assessment is None:
        lines.append("    not read (pass --read-filing; costs about $0.05)")
    else:
        if dossier.filing_label:
            lines.append(f"    {dossier.filing_label}")
        if dossier.filing_url:
            lines.append(f"    {dossier.filing_url}")
        if dossier.sections_missing:
            lines.append(
                "    [!] sections extraction could not find: "
                + ", ".join(dossier.sections_missing)
            )
        if assessment is not None:
            lines.append(
                f"    read: {assessment.verdict.value} | moat {assessment.moat.value}, "
                f"trend {assessment.moat_trend.value} | customer concentration "
                f"{assessment.customer_concentration.value} | confidence "
                f"{assessment.confidence.value}"
            )
            lines.append("    A read, not a veto — phase 6 measured the veto and it failed.")
            for title, items in (
                ("accounting flags", assessment.accounting_flags),
                ("key risks", assessment.key_risks),
                ("evidence gaps", assessment.evidence_gaps),
            ):
                lines.append(f"    {title}: {'; '.join(items) if items else 'none stated'}")
            lines.append(f"    thesis: {assessment.thesis}")
        lines.append(f"    spent ${dossier.spend_usd:.4f} of DeepSeek tokens")

    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--years", type=int, default=HISTORY_YEARS)
    parser.add_argument("--tolerance", type=int, default=VIOLATION_TOLERANCE)
    parser.add_argument("--discount-rate", type=float, default=None,
                        help="override the 10-year Treasury yield, e.g. 0.045")
    parser.add_argument("--read-filing", action="store_true",
                        help="also read Items 1/1A/7 of the newest 10-K (~$0.05)")
    parser.add_argument("--budget", type=float, default=RUN_BUDGET_USD,
                        help="USD cap for this run; fails closed")
    parser.add_argument("--model", default=ANALYST_MODEL)
    parser.add_argument("--no-ingest", action="store_true",
                        help="fail instead of fetching a name the store has never seen")
    parser.add_argument("--db", default=None, help="override the store path")
    args = parser.parse_args(argv)

    ticker = tickermap.normalise(args.ticker)
    conn = db.connect(args.db)
    try:
        dossier = build(
            conn,
            ticker,
            args.as_of,
            years=args.years,
            tolerance=args.tolerance,
            discount_rate=args.discount_rate,
            with_filing=args.read_filing,
            budget=Budget(run_cap_usd=args.budget) if args.read_filing else None,
            model=args.model,
            ingest_missing=not args.no_ingest,
        )
    except (LookupError, SecRequestError, market.PriceError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print("\n".join(render(dossier)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
