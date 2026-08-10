"""XBRL concept resolution: the same economic number, tagged a dozen ways.

Companies tag the same line item differently from each other and from their own
past filings — the ASC 606 revenue change alone splits most issuers' history
across two tags. So resolution happens **per fiscal year**, not per company: for
each year we take the first tag in the chain that actually has data for that
year. A company that used a deprecated tag for a decade resolves; a company that
switched mid-history resolves on both sides of the switch.

Two facts about the companyfacts payload drive the rest of this module, both
verified against a live filing:

- ``fy`` in a row is the fiscal year of the *filing*, not of the period. Apple's
  FY2007 income appears in rows carrying ``fy: 2009``. Fiscal year is therefore
  derived from ``end``.
- The same period appears under several ``accn`` values as later filings restate
  it. Apple's FY2008 net income was 4,834M as filed in 2009 and 6,119M as
  restated in 2010. **Every version is kept**, each with its own ``filed`` date,
  so a point-in-time query can ask what was knowable on a given day.
"""

from dataclasses import dataclass
from datetime import date
from enum import Enum

TAXONOMY = "us-gaap"

# A fiscal year runs 52 or 53 weeks; transition periods and odd calendars widen
# it a little. Anything outside this window is a quarter or a stub, not a year.
_MIN_ANNUAL_DAYS = 330
_MAX_ANNUAL_DAYS = 400

# A fiscal year ending in Jan–May is conventionally named for the previous
# calendar year: a retailer closing 2025-01-31 calls that fiscal 2024.
_FY_ROLLOVER_MONTH = 5


class Kind(str, Enum):
    """Whether a concept measures a period (flow) or a moment (balance)."""

    DURATION = "duration"
    INSTANT = "instant"


@dataclass(frozen=True)
class Concept:
    """One economic concept and the ordered tag chain that may carry it."""

    name: str
    kind: Kind
    tags: tuple[str, ...]
    unit: str = "USD"
    # Some concepts are filed with an inconsistent sign across companies (capex
    # as a negative cash flow in one filing, positive in the next). For those the
    # magnitude is what the ratios need, so it is normalised on write.
    absolute: bool = False


@dataclass(frozen=True)
class Fact:
    """One resolved number, as reported in one specific filing."""

    concept: str
    fiscal_year: int
    period_end: str
    filed: str
    value: float
    unit: str
    source_tag: str
    accn: str


CONCEPTS: tuple[Concept, ...] = (
    Concept("Revenue", Kind.DURATION, (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    )),
    Concept("CostOfRevenue", Kind.DURATION, (
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    )),
    Concept("GrossProfit", Kind.DURATION, ("GrossProfit",)),
    Concept("SGA", Kind.DURATION, ("SellingGeneralAndAdministrativeExpense",)),
    # An absent R&D line is legitimate — plenty of excellent businesses do none.
    Concept("RnD", Kind.DURATION, ("ResearchAndDevelopmentExpense",)),
    Concept("DepreciationAmortization", Kind.DURATION, (
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "Depreciation",
    )),
    Concept("OperatingIncome", Kind.DURATION, ("OperatingIncomeLoss",)),
    # InterestIncomeExpenseNet is deliberately *not* in this chain. It can be net
    # income rather than expense, and taking its magnitude would turn interest
    # earned into interest paid — criterion 6 would then read backwards.
    Concept("InterestExpense", Kind.DURATION, (
        "InterestExpense",
        "InterestExpenseDebt",
    ), absolute=True),
    Concept("IncomeTax", Kind.DURATION, ("IncomeTaxExpenseBenefit",)),
    Concept("NetIncome", Kind.DURATION, ("NetIncomeLoss", "ProfitLoss")),
    Concept("Assets", Kind.INSTANT, ("Assets",)),
    Concept("AssetsCurrent", Kind.INSTANT, ("AssetsCurrent",)),
    Concept("Liabilities", Kind.INSTANT, ("Liabilities",)),
    Concept("LiabilitiesCurrent", Kind.INSTANT, ("LiabilitiesCurrent",)),
    Concept("Equity", Kind.INSTANT, (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    )),
    Concept("LongTermDebt", Kind.INSTANT, (
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
    )),
    Concept("RetainedEarnings", Kind.INSTANT, ("RetainedEarningsAccumulatedDeficit",)),
    Concept("TreasuryStock", Kind.INSTANT, (
        "TreasuryStockValue",
        "TreasuryStockCommonValue",
    ), absolute=True),
    Concept("OperatingCashFlow", Kind.DURATION, (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    )),
    Concept("Capex", Kind.DURATION, (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ), absolute=True),
    Concept("DilutedShares", Kind.DURATION, (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ), unit="shares"),
    Concept("DividendsPaid", Kind.DURATION, (
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
    ), absolute=True),
)

CONCEPTS_BY_NAME = {concept.name: concept for concept in CONCEPTS}
CONCEPT_NAMES = tuple(concept.name for concept in CONCEPTS)

# Subtractions used only when the tag chain found nothing for that year. Both
# are accounting identities, so they cannot invent a number that disagrees with
# the filing. OperatingIncome is *not* derived from expense lines: assembling it
# from partial components is how a plausible, wrong figure reaches the screen.
_DERIVATIONS: tuple[tuple[str, str, str], ...] = (
    ("GrossProfit", "Revenue", "CostOfRevenue"),
    ("Liabilities", "Assets", "Equity"),
)

# Concepts a company may report split across two tags, summed when the single
# combined tag is absent for that year.
_TAG_SUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SGA", ("GeneralAndAdministrativeExpense", "SellingAndMarketingExpense")),
)


def fiscal_year_for(period_end: date) -> int:
    """Fiscal year a period belongs to, named by the convention issuers use."""
    return period_end.year - 1 if period_end.month <= _FY_ROLLOVER_MONTH else period_end.year


def annual_facts(payload: dict) -> list[Fact]:
    """Resolve every concept, for every fiscal year, from a companyfacts payload.

    Returns one ``Fact`` per (concept, fiscal year, filing), restatements
    included — filtering to a point in time is the store's job, not this one's.
    """
    tags = payload.get("facts", {}).get(TAXONOMY, {})
    facts: list[Fact] = []
    for concept in CONCEPTS:
        for tag, rows in _rows_by_year(tags, concept).values():
            facts.extend(_to_fact(concept, tag, row) for row in rows)

    facts.extend(_summed_facts(tags, facts))
    facts.extend(_derived_facts(facts))
    return sorted(facts, key=lambda f: (f.concept, f.fiscal_year, f.filed))


def _rows_by_year(tags: dict, concept: Concept) -> dict[int, tuple[str, list[dict]]]:
    """Per fiscal year, the first tag in the chain that has data, and its rows."""
    resolved: dict[int, tuple[str, list[dict]]] = {}
    for tag in concept.tags:
        for row in tags.get(tag, {}).get("units", {}).get(concept.unit, []):
            if not _is_annual_10k(row, concept.kind):
                continue
            fiscal_year = fiscal_year_for(date.fromisoformat(row["end"]))
            if fiscal_year not in resolved:
                resolved[fiscal_year] = (tag, [])
            elif resolved[fiscal_year][0] != tag:
                continue  # an earlier tag in the chain already owns this year
            resolved[fiscal_year][1].append(row)

    if concept.kind is Kind.INSTANT:
        for fiscal_year, (tag, rows) in resolved.items():
            resolved[fiscal_year] = (tag, _closing_balances(rows))
    return resolved


def _closing_balances(rows: list[dict]) -> list[dict]:
    """Keep the year's closing balance, discarding the next year's opening one.

    Adopting a new accounting standard posts a cumulative-effect adjustment dated
    the first day of the following year. Arista's equity is 1,190,803k at
    2018-12-31 and 1,194,505k at 2019-01-01, both in the same 10-K, and the
    fiscal-year rollover that (correctly) files a January year-end retailer under
    the previous year pulls that opening balance in alongside the close.

    An opening balance is always dated after the close it follows, and a genuine
    close carries the same date across every filing that restates it, so the
    earliest date in a fiscal year is the close and anything later is an opening.
    A 52/53-week filer whose year really ends on 2 January reports only that one
    instant, and is unaffected.
    """
    if not rows:
        return rows
    closing_date = min(row["end"] for row in rows)
    return [row for row in rows if row["end"] == closing_date]


def _is_annual_10k(row: dict, kind: Kind) -> bool:
    """True for a full-year figure taken from a 10-K (or its amendment)."""
    if not str(row.get("form", "")).startswith("10-K"):
        return False
    if "end" not in row or "val" not in row or "filed" not in row:
        return False
    if kind is Kind.INSTANT:
        return "start" not in row
    if "start" not in row:
        return False
    span = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
    return _MIN_ANNUAL_DAYS <= span <= _MAX_ANNUAL_DAYS


def _to_fact(concept: Concept, tag: str, row: dict, source_tag: str | None = None) -> Fact:
    value = float(row["val"])
    return Fact(
        concept=concept.name,
        fiscal_year=fiscal_year_for(date.fromisoformat(row["end"])),
        period_end=row["end"],
        filed=row["filed"],
        value=abs(value) if concept.absolute else value,
        unit=concept.unit,
        source_tag=source_tag or tag,
        accn=row.get("accn", ""),
    )


def _summed_facts(tags: dict, existing: list[Fact]) -> list[Fact]:
    """Fill a concept from the sum of two component tags, where it is missing."""
    summed: list[Fact] = []
    for name, components in _TAG_SUMS:
        concept = CONCEPTS_BY_NAME[name]
        covered = {fact.fiscal_year for fact in existing if fact.concept == name}
        parts: dict[tuple[int, str], list[dict]] = {}
        for tag in components:
            for row in tags.get(tag, {}).get("units", {}).get(concept.unit, []):
                if not _is_annual_10k(row, concept.kind):
                    continue
                fiscal_year = fiscal_year_for(date.fromisoformat(row["end"]))
                if fiscal_year in covered:
                    continue
                parts.setdefault((fiscal_year, row.get("accn", "")), []).append(row)

        for rows in parts.values():
            if len(rows) != len(components):
                continue  # a partial sum is a wrong number, not a smaller one
            total = dict(rows[0])
            total["val"] = sum(float(row["val"]) for row in rows)
            summed.append(_to_fact(concept, "", total, source_tag="+".join(components)))
    return summed


def _derived_facts(existing: list[Fact]) -> list[Fact]:
    """Apply the accounting identities, joined within a single filing."""
    index = {(fact.concept, fact.fiscal_year, fact.accn): fact for fact in existing}
    covered = {(fact.concept, fact.fiscal_year) for fact in existing}

    derived: list[Fact] = []
    for target, minuend, subtrahend in _DERIVATIONS:
        for (concept, fiscal_year, accn), left in index.items():
            if concept != minuend or (target, fiscal_year) in covered:
                continue
            right = index.get((subtrahend, fiscal_year, accn))
            # Same filing and same period, or the difference is meaningless.
            if right is None or right.period_end != left.period_end:
                continue
            derived.append(Fact(
                concept=target,
                fiscal_year=fiscal_year,
                period_end=left.period_end,
                filed=max(left.filed, right.filed),
                value=left.value - right.value,
                unit=left.unit,
                source_tag=f"derived:{minuend}-{subtrahend}",
                accn=accn,
            ))
    return derived
