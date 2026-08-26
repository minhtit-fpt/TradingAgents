"""D7 -- the filing read on the names D5 already chose.

    python -m tradingagents.value.dividend.brief --size 5
    python -m tradingagents.value.dividend.brief --tickers PG,KO --budget 1.00

D1 reads the payout, D5 reads the price, and neither reads a sentence of English.
This asks the analyst the part the statements cannot show -- whether the moat is
eroding, whether MD&A hides an accounting problem, whether a handful of customers
carry the revenue -- for the names that already cleared both.

**It is a briefing, not a gate.** Nothing here can add a name, remove one, or
reorder the basket: ``select`` has already run and its output is what gets
printed, with the read attached underneath. That is not caution for its own sake,
it is phase 6's measurement -- an LLM veto at entry could not be told apart from
a random one, and ``verdict`` landed on ``caution`` for 70% of picks. So the
verdict renders last here exactly as it does in ``alerts.message.briefing``,
which this reuses rather than reimplements.

The one asymmetry worth stating: a name the analyst dislikes stays on the list,
because the operator is the filter. What the read buys is the sentence the
operator would otherwise have to find in a 200-page filing themselves.

Cost, and why it is bounded three ways. The read is the only paid call in this
package, so it runs over the basket rather than the pass list (15 names, not
153), it is cached on the exact prompt like every other tier-3 call, and it is
charged against ``Budget`` -- which fails closed. ``--dry-run`` prints the names
and the estimated call count without spending anything.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date

from ..alerts import message
from ..analyst import value_analyst
from ..analyst.schemas import ValueAssessment
from ..config import ANALYST_MODEL, MONTH_BUDGET_USD, RUN_BUDGET_USD
from ..edgar import filings
from ..edgar.client import SecClient, SecRequestError
from ..llm.budget import Budget, BudgetError
from ..store import db
from . import config, stability


@dataclass(frozen=True)
class Read:
    """One name's filing read, or the reason there is not one.

    ``assessment is None`` with a ``note`` is a first-class outcome, not a
    failure to handle: an absent read is information and an invented one is not,
    which is the same rule ``report.read_filing`` states for the business screen.
    """

    ticker: str
    assessment: ValueAssessment | None = None
    label: str = ""
    url: str = ""
    missing_sections: tuple[str, ...] = ()
    note: str = ""


def numeric_summary(
    row: stability.Stability,
    outcome,
    forward_yield: float | None,
) -> str:
    """What the screen already established, in the analyst's ``## Numeric screen`` slot.

    The shared system prompt describes the *business* screen's thirteen criteria,
    because it is a byte-stable cache prefix and editing it would invalidate every
    assessment already paid for across both surfaces. So this block opens by
    naming which screen actually ran. The instruction the prompt gives -- do not
    re-derive the numbers, judge the moat and the language -- is the same either
    way, and it is the part that matters.
    """
    yield_text = f"{forward_yield:.2%}" if forward_yield is not None else "unknown"
    lines = [
        "This name cleared the DIVIDEND screen, not the thirteen-criterion business "
        "screen: four payout criteria over ten years, then two price limits. The "
        "criteria below are the ones that ran.",
        "",
        f"Dividend screen: PASS, criteria-clean {outcome.result.quality:.0%}",
    ]
    for criterion in outcome.result.criteria:
        state = "pass" if criterion.passed else "FAIL"
        detail = ""
        if criterion.violation_years:
            detail = " (violated " + ", ".join(str(y) for y in criterion.violation_years) + ")"
        elif criterion.missing_years:
            detail = " (no data " + ", ".join(str(y) for y in criterion.missing_years) + ")"
        lines.append(f"  {criterion.name}: {state}{detail}")

    dps = outcome.latest_dps
    lines.extend(
        (
            "",
            f"Last full year paid {dps:,.2f}/share" if dps else "No payment last full year",
            f"Forward yield at today's price: {yield_text}",
            f"Trailing {config.STABILITY_YEARS}y price: volatility {row.volatility:.1%}, "
            f"worst fall {row.max_drawdown:.1%}, return {row.annual_return:.1%}/yr",
            "",
            "The question this read answers: can the payout survive five to ten more "
            "years? Judge the business, not the last quarter.",
        )
    )
    return "\n".join(lines)


def read_one(
    conn: sqlite3.Connection,
    row: stability.Stability,
    outcome,
    forward_yield: float | None,
    as_of: str,
    *,
    client=None,
    budget: Budget | None = None,
    llm=None,
    model: str = ANALYST_MODEL,
) -> Read:
    """Read the newest 10-K filed on or before ``as_of`` for one chosen name."""
    cik = db.cik_for(conn, row.ticker)
    if cik is None:
        return Read(row.ticker, note=f"no CIK on file for {row.ticker}; filing not read")

    try:
        filing, sections = filings.sections_for(client or SecClient(), cik, as_of)
    except (filings.FilingNotFound, SecRequestError, ValueError) as exc:
        return Read(row.ticker, note=f"filing not read: {exc}")

    label = f"10-K filed {filing.filed} (accession {filing.accession})"
    # A geometry fault means the analyst was handed the wrong text even though
    # every section came back non-empty. It travels on ``note`` so it reaches the
    # operator, not only the model that was fed it.
    warning = (
        "extraction suspect (" + "; ".join(sections.suspect) + ")" if sections.suspect else ""
    )
    try:
        assessment = value_analyst.assess(
            row.ticker,
            sections,
            numeric_summary(row, outcome, forward_yield),
            llm=llm,
            model=model,
            budget=budget,
        )
    except (value_analyst.ValueAnalystError, BudgetError) as exc:
        return Read(
            row.ticker,
            label=label,
            url=filing.url,
            missing_sections=sections.missing,
            note=f"filing not assessed: {exc}",
        )
    return Read(row.ticker, assessment, label, filing.url, sections.missing, warning)


def render(picked: stability.Selection, reads: list[Read], as_of: str) -> list[str]:
    """The basket, each name carrying its read. Verdict last, per phase 6."""
    lines = [
        f"dividend briefing as of {as_of}: {len(picked.chosen)} names from D5, "
        f"{sum(1 for r in reads if r.assessment is not None)} filings read",
        "",
    ]
    by_ticker = {read.ticker: read for read in reads}
    for row in picked.chosen:
        forward_yield = picked.yields.get(row.ticker)
        yield_text = f"{forward_yield:.2%}" if forward_yield is not None else "  n/a"
        lines.append(
            f"{row.ticker:<6} yield {yield_text}, vol {row.volatility:.0%}, "
            f"worst {row.max_drawdown:.0%}, return {row.annual_return:+.1%}/yr"
        )
        read = by_ticker.get(row.ticker)
        if read is None:
            lines.extend(("    filing not read: not requested", ""))
            continue
        if read.label:
            lines.append(f"    {read.label}")
        if read.missing_sections:
            lines.append(
                "    [!] sections extraction could not find: "
                + ", ".join(read.missing_sections)
            )
        if read.note:
            lines.append(f"    [!] {read.note}")
        if read.assessment is not None:
            lines.extend(f"    {line}" for line in message.briefing(read.assessment))
        lines.append("")

    lines.extend(
        (
            "A briefing, not a gate: no name above was added, removed or reordered by "
            "the read. The list is D5's.",
            message.DISCLAIMER,
        )
    )
    return lines


def run(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    tickers: list[str] | None = None,
    size: int = config.BASKET_SIZE,
    min_yield: float = config.MIN_YIELD,
    max_volatility: float = config.MAX_VOLATILITY,
    max_drawdown: float = config.MAX_DRAWDOWN,
    years: int = config.STABILITY_YEARS,
    dry_run: bool = False,
    client=None,
    budget: Budget | None = None,
    llm=None,
    model: str = ANALYST_MODEL,
    fetch=stability.closes,
    last=None,
) -> list[str]:
    """Select with D5, then read the filing of each chosen name."""
    kwargs = {} if last is None else {"last": last}
    picked = stability.selection(
        conn,
        as_of,
        years=years,
        min_yield=min_yield,
        max_volatility=max_volatility,
        max_drawdown=max_drawdown,
        size=size,
        fetch=fetch,
        **kwargs,
    )

    wanted = picked.chosen
    if tickers:
        requested = {t.upper() for t in tickers}
        wanted = [row for row in picked.chosen if row.ticker in requested]

    if dry_run:
        names = ", ".join(row.ticker for row in wanted) or "none"
        return [
            f"dividend briefing as of {as_of}: dry run, nothing spent",
            f"would read {len(wanted)} filings: {names}",
        ]

    reads = [
        read_one(
            conn,
            row,
            picked.passes[row.ticker],
            picked.yields.get(row.ticker),
            as_of,
            client=client,
            budget=budget,
            llm=llm,
            model=model,
        )
        for row in wanted
        # A chosen name with no screened outcome behind it cannot happen through
        # ``selection``; skipping rather than raising keeps an injected Selection
        # in a test from being the thing that decides this surface's behaviour.
        if row.ticker in picked.passes
    ]
    return render(picked, reads, as_of)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read the 10-K of each name the dividend screen and D5 chose"
    )
    parser.add_argument("--db", default=None, help="override the store path")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--tickers", default=None, help="comma-separated subset of the basket")
    parser.add_argument("--size", type=int, default=config.BASKET_SIZE)
    parser.add_argument("--years", type=int, default=config.STABILITY_YEARS)
    parser.add_argument("--min-yield", type=float, default=config.MIN_YIELD)
    parser.add_argument("--max-volatility", type=float, default=config.MAX_VOLATILITY)
    parser.add_argument("--max-drawdown", type=float, default=config.MAX_DRAWDOWN)
    parser.add_argument("--model", default=ANALYST_MODEL)
    parser.add_argument("--budget", type=float, default=RUN_BUDGET_USD, help="run cap, USD")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="name the filings that would be read; spend nothing",
    )
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    try:
        lines = run(
            conn,
            args.as_of,
            tickers=args.tickers.split(",") if args.tickers else None,
            size=args.size,
            years=args.years,
            min_yield=args.min_yield,
            max_volatility=args.max_volatility,
            max_drawdown=args.max_drawdown,
            dry_run=args.dry_run,
            budget=None
            if args.dry_run
            else Budget(run_cap_usd=args.budget, month_cap_usd=MONTH_BUDGET_USD),
            model=args.model,
        )
    except (stability.StabilityError, BudgetError) as exc:
        print(f"briefing not produced: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
