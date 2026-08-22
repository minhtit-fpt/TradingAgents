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
from datetime import date, timedelta
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
    # Smallest value that can be a real figure rather than a filer scale error.
    # A row below it is dropped, not rescaled: inferring the intended scale is
    # guessing, and a guessed denominator produces a confidently wrong EPS.
    minimum: float | None = None


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
    # Combined totals only. A filer that splits products from services reports
    # CostOfGoodsSold and CostOfServices as two halves of one line, so those —
    # and the "excluding depreciation" variants that rank below them — are
    # resolved in ``_TAG_SUMS`` rather than raced here. First-tag-wins would take
    # the goods half alone and report a gross profit too high by the whole cost
    # of the service business.
    Concept("CostOfRevenue", Kind.DURATION, (
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
    )),
    Concept("GrossProfit", Kind.DURATION, ("GrossProfit",)),
    Concept("SGA", Kind.DURATION, ("SellingGeneralAndAdministrativeExpense",)),
    # An absent R&D line is legitimate — plenty of excellent businesses do none.
    Concept("RnD", Kind.DURATION, (
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        "ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost",
    )),
    Concept("DepreciationAmortization", Kind.DURATION, (
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
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
    # A handful of filers tag the share count in millions while declaring the
    # unit as shares — Bruker files 156.6 for 156,600,000 — and the error is
    # invisible downstream because it only shows up as an EPS a million times
    # too large. No SEC registrant in this universe has under a million diluted
    # shares, so anything below that is the scale error, not a small company.
    Concept("DilutedShares", Kind.DURATION, (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ), unit="shares", minimum=1_000_000.0),
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

# The two halves of a split cost of revenue. Thermo Fisher, Northrop and Intuit
# all report "cost of product revenues" and "cost of service revenues" as
# separate lines with no combined total, and the pair-first ordering below is the
# same guard SG&A needs: taking the goods half alone understates cost, and an
# understated cost base is a gross margin that clears criterion 1 on arithmetic
# the filing does not support.
_GOODS_COST_TAGS = ("CostOfGoodsSold",)
_SERVICE_COST_TAGS = (
    "CostOfServices",
    "CostOfServicesExcludingDepreciationDepletionAndAmortization",
)

# Last resort. A cost base with D&A stripped out is the filer's own presentation
# and is not on the same footing as one that includes it, so a company reporting
# any inclusive line keeps it.
_EXCLUDING_DDA_COST_TAGS = (
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    "CostOfRevenueExcludingDepreciationDepletionAndAmortization",
)

# The selling half of SG&A, however the filer named it. First one with data for
# the year wins; they are alternatives, never summed with each other.
_SELLING_TAGS = (
    "SellingAndMarketingExpense",
    "SellingExpense",
    "MarketingExpense",
    "MarketingAndAdvertisingExpense",
)

# Ways to fill a concept the tag chain missed, in order, each stage seeing only
# the years still uncovered. Every element of a stage is a slot, and every slot
# is a set of alternative tags — all slots must be filled from the same filing or
# the stage produces nothing for that year.
#
# The order is what keeps the number honest. A filer that splits selling from
# administrative is summed; only a filer with no selling line anywhere in the
# year falls through to administrative alone. Reversed, every REIT and bank
# would still resolve, and every retailer would silently lose its selling costs
# — an expensive business reading as a disciplined one.
_TAG_SUMS: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    ("SGA", (("GeneralAndAdministrativeExpense",), _SELLING_TAGS)),
    ("SGA", (("GeneralAndAdministrativeExpense",),)),
    ("CostOfRevenue", (_GOODS_COST_TAGS, _SERVICE_COST_TAGS)),
    ("CostOfRevenue", (_GOODS_COST_TAGS,)),
    ("CostOfRevenue", (_SERVICE_COST_TAGS,)),
    ("CostOfRevenue", (_EXCLUDING_DDA_COST_TAGS,)),
)


# Concepts allowed to be rebuilt from the quarterly columns of a 10-K. Since the
# 2018 taxonomy change a number of filers — Thermo Fisher, Northrop, Intuit —
# publish no dimensionless annual cost-of-revenue line at all, only the quarterly
# note, which left four criteria unevaluable for the whole modern half of their
# history. Quarters summing to their own fiscal year is an identity, so this
# recovers the figure without inventing one. Deliberately not applied to every
# concept: the tag chains resolve annually for the rest, and a wider rollup would
# change numbers that are not broken.
_QUARTERLY_ROLLUP = ("CostOfRevenue", "GrossProfit")

# A fiscal quarter is 13 weeks; 4-4-5 calendars and transition quarters stretch
# it. Anything outside this is a half-year, a stub, or a full year.
_MIN_QUARTER_DAYS = 75
_MAX_QUARTER_DAYS = 115
_QUARTERS_PER_YEAR = 4


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
    facts.extend(_quarterly_rollups(tags, facts))
    facts.extend(_derived_facts(facts))
    return sorted(facts, key=lambda f: (f.concept, f.fiscal_year, f.filed))


def _rows_by_year(tags: dict, concept: Concept) -> dict[int, tuple[str, list[dict]]]:
    """Per fiscal year, the first tag in the chain that has data, and its rows."""
    resolved: dict[int, tuple[str, list[dict]]] = {}
    for tag in concept.tags:
        for row in tags.get(tag, {}).get("units", {}).get(concept.unit, []):
            if not _is_annual_10k(row, concept.kind) or _is_scale_error(row, concept):
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


def _is_scale_error(row: dict, concept: Concept) -> bool:
    """True when the value is too small to be the figure the tag claims to be."""
    return concept.minimum is not None and abs(float(row["val"])) < concept.minimum


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
    """Fill a concept from its component tags, for the years still missing it."""
    summed: list[Fact] = []
    for name, slots in _TAG_SUMS:
        concept = CONCEPTS_BY_NAME[name]
        covered = {fact.fiscal_year for fact in existing if fact.concept == name}
        covered |= {fact.fiscal_year for fact in summed if fact.concept == name}

        # {(fiscal year, filing): {slot index: (tag, row)}}
        parts: dict[tuple[int, str], dict[int, tuple[str, dict]]] = {}
        for index, alternatives in enumerate(slots):
            for tag in alternatives:
                for row in tags.get(tag, {}).get("units", {}).get(concept.unit, []):
                    if not _is_annual_10k(row, concept.kind):
                        continue
                    fiscal_year = fiscal_year_for(date.fromisoformat(row["end"]))
                    if fiscal_year in covered:
                        continue
                    filled = parts.setdefault((fiscal_year, row.get("accn", "")), {})
                    filled.setdefault(index, (tag, row))  # earlier alternative wins

        for filled in parts.values():
            if len(filled) != len(slots):
                continue  # a partial sum is a wrong number, not a smaller one
            found = [filled[index] for index in range(len(slots))]
            total = dict(found[0][1])
            total["val"] = sum(float(row["val"]) for _, row in found)
            source = "+".join(tag for tag, _ in found)
            summed.append(_to_fact(concept, "", total, source_tag=source))
    return summed


def _quarterly_rollups(tags: dict, existing: list[Fact]) -> list[Fact]:
    """Rebuild a year from the 10-K's quarterly columns, for the years still missing it.

    Guarded by the filer's own year end. Four contiguous quarters always span
    about a year, so a chain starting at Q2 tiles a rolling twelve months that is
    not a fiscal year at all — Intuit's FY2020 filing yields one starting in
    February that ends in January. Requiring the chain to finish on a period end
    the filer already reported annually for some other concept is what separates
    the fiscal year from the rolling window.
    """
    year_ends: dict[int, set[str]] = {}
    for fact in existing:
        if CONCEPTS_BY_NAME[fact.concept].kind is Kind.DURATION:
            year_ends.setdefault(fact.fiscal_year, set()).add(fact.period_end)

    rolled: list[Fact] = []
    for name in _QUARTERLY_ROLLUP:
        concept = CONCEPTS_BY_NAME[name]
        covered = {fact.fiscal_year for fact in existing if fact.concept == name}
        for tag in concept.tags:
            for accn, rows in _quarters_by_filing(tags, concept, tag).items():
                for chain in _fiscal_year_chains(rows, year_ends):
                    fiscal_year = fiscal_year_for(date.fromisoformat(chain[-1]["end"]))
                    if fiscal_year in covered:
                        continue
                    covered.add(fiscal_year)
                    total = dict(chain[-1])
                    total["val"] = sum(float(row["val"]) for row in chain)
                    total["accn"] = accn
                    rolled.append(_to_fact(concept, "", total, source_tag=f"{tag}:4Q"))
    return rolled


def _quarters_by_filing(tags: dict, concept: Concept, tag: str) -> dict[str, list[dict]]:
    """Quarter-length 10-K rows for one tag, grouped by the filing they came from."""
    by_filing: dict[str, list[dict]] = {}
    for row in tags.get(tag, {}).get("units", {}).get(concept.unit, []):
        if not str(row.get("form", "")).startswith("10-K"):
            continue
        if not {"start", "end", "val", "filed"} <= row.keys():
            continue
        span = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
        if _MIN_QUARTER_DAYS <= span <= _MAX_QUARTER_DAYS:
            by_filing.setdefault(row.get("accn", ""), []).append(row)
    return by_filing


def _fiscal_year_chains(
    rows: list[dict],
    year_ends: dict[int, set[str]],
) -> list[list[dict]]:
    """Runs of contiguous quarters that close on a known fiscal year end."""
    # One row per start date; the same quarter appears twice in some filings.
    first_by_start: dict[str, dict] = {}
    for row in sorted(rows, key=lambda row: (row["start"], row["end"])):
        first_by_start.setdefault(row["start"], row)

    chains: list[list[dict]] = []
    for start in sorted(first_by_start):
        chain: list[dict] = []
        cursor = start
        while cursor in first_by_start and len(chain) < _QUARTERS_PER_YEAR:
            row = first_by_start[cursor]
            chain.append(row)
            cursor = (date.fromisoformat(row["end"]) + timedelta(days=1)).isoformat()

        if len(chain) < _QUARTERS_PER_YEAR:
            continue
        end = chain[-1]["end"]
        span = (date.fromisoformat(end) - date.fromisoformat(start)).days
        if not _MIN_ANNUAL_DAYS <= span <= _MAX_ANNUAL_DAYS:
            continue
        if end in year_ends.get(fiscal_year_for(date.fromisoformat(end)), set()):
            chains.append(chain)
    return chains


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
