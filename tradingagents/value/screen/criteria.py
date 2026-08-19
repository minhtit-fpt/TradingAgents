"""The thirteen numeric criteria, judged over a decade rather than a year.

Pure functions over ``{fiscal_year: {concept: value}}``. No I/O, no LLM, so the
whole screen can be replayed for every threshold change in phase 3's backtest.

Two rules shape everything here:

- **The binding constraint is "sustained", not the level.** One 40% gross margin
  is common; ten consecutive are rare, and that is what narrows ~3,000 filers to
  a double-digit list. Each criterion therefore returns the years that violated
  it, and passes only if the count stays inside ``VIOLATION_TOLERANCE``.
- **A year we cannot evaluate is not a year that passed.** A missing input makes
  the year count against the criterion exactly like a violation. The alternative
  — skipping it — is how a company with two years of data scores a perfect ten.
"""

import math
from dataclasses import dataclass

from ..config import (
    CAPEX_TO_NET_INCOME_MAX,
    DA_TO_GROSS_PROFIT_MAX,
    DEBT_TO_EQUITY_MAX,
    GROSS_MARGIN_MIN,
    HISTORY_YEARS,
    INTEREST_TO_OPERATING_INCOME_MAX,
    LONG_TERM_DEBT_TO_NET_INCOME_MAX,
    NET_MARGIN_MIN,
    RND_TO_GROSS_PROFIT_MAX,
    ROE_MIN,
    SGA_TO_GROSS_PROFIT_MAX,
    VIOLATION_TOLERANCE,
)

# fiscal year -> concept name -> value, as ``store.db.series_as_of`` returns it.
Financials = dict[int, dict[str, float]]

# Concepts whose absence is a legitimate zero rather than a hole in the data: a
# business with no debt files no interest expense, and plenty of excellent ones
# do no R&D. Every other missing concept makes the year unevaluable.
_ABSENT_MEANS_ZERO = frozenset({"RnD", "InterestExpense", "LongTermDebt", "TreasuryStock"})


@dataclass(frozen=True)
class CriterionResult:
    """One criterion's verdict, with the evidence that produced it."""

    number: int
    name: str
    passed: bool
    violation_years: tuple[int, ...]
    missing_years: tuple[int, ...]
    values: tuple[tuple[int, float], ...]
    blocking: bool = True

    @property
    def bad_years(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.violation_years) | set(self.missing_years)))


@dataclass(frozen=True)
class ScreenResult:
    """The whole numeric screen for one company as of one date."""

    passed: bool
    criteria: tuple[CriterionResult, ...]
    years: tuple[int, ...]
    years_required: int

    @property
    def quality(self) -> float:
        """Share of (blocking criterion, year) checks that came out clean.

        1.0 is a decade clean on every criterion. This is the module's own thesis
        expressed as a number — "sustained" is what narrows three thousand filers
        to a double-digit list, so *how* sustained is the natural ranking key, and
        phase 4b step 4 ranks on it instead of gating on margin of safety.

        A year that could not be evaluated counts against the score exactly as a
        violation does, for the reason stated at the top of this module: a company
        with two years of data must not score a perfect ten.
        """
        checks = [c for c in self.criteria if c.blocking]
        slots = len(checks) * self.years_required
        if not slots:
            return 0.0
        bad = sum(len(c.bad_years) for c in checks)
        # Years short of the requirement are unevaluated years, not clean ones.
        bad += len(checks) * max(self.years_required - len(self.years), 0)
        return max(1.0 - bad / slots, 0.0)

    @property
    def failed_criteria(self) -> tuple[str, ...]:
        """Blocking criteria that failed — the reason line for the screen log."""
        failures = tuple(c.name for c in self.criteria if c.blocking and not c.passed)
        if len(self.years) < self.years_required:
            return ("InsufficientHistory",) + failures
        return failures


@dataclass(frozen=True)
class RatioRule:
    """A ratio criterion: ``sum(numerator) / sum(denominator)`` against a limit."""

    number: int
    name: str
    numerator: tuple[str, ...]
    denominator: tuple[str, ...]
    limit: float
    at_most: bool


RATIO_RULES: tuple[RatioRule, ...] = (
    RatioRule(1, "GrossMargin", ("GrossProfit",), ("Revenue",), GROSS_MARGIN_MIN, at_most=False),
    RatioRule(2, "NetMargin", ("NetIncome",), ("Revenue",), NET_MARGIN_MIN, at_most=False),
    RatioRule(3, "SGAToGrossProfit", ("SGA",), ("GrossProfit",),
              SGA_TO_GROSS_PROFIT_MAX, at_most=True),
    RatioRule(4, "RnDToGrossProfit", ("RnD",), ("GrossProfit",),
              RND_TO_GROSS_PROFIT_MAX, at_most=True),
    RatioRule(5, "DAToGrossProfit", ("DepreciationAmortization",), ("GrossProfit",),
              DA_TO_GROSS_PROFIT_MAX, at_most=True),
    RatioRule(6, "InterestToOperatingIncome", ("InterestExpense",), ("OperatingIncome",),
              INTEREST_TO_OPERATING_INCOME_MAX, at_most=True),
    RatioRule(7, "LongTermDebtToNetIncome", ("LongTermDebt",), ("NetIncome",),
              LONG_TERM_DEBT_TO_NET_INCOME_MAX, at_most=True),
    # Buffett adds treasury stock back to equity: buying back your own shares
    # shrinks book equity but does not make the business more leveraged.
    RatioRule(8, "DebtToEquity", ("Liabilities",), ("Equity", "TreasuryStock"),
              DEBT_TO_EQUITY_MAX, at_most=True),
    RatioRule(9, "ReturnOnEquity", ("NetIncome",), ("Equity",), ROE_MIN, at_most=False),
    RatioRule(11, "CapexToNetIncome", ("Capex",), ("NetIncome",),
              CAPEX_TO_NET_INCOME_MAX, at_most=True),
)


def evaluate(
    financials: Financials,
    *,
    years_required: int = HISTORY_YEARS,
    tolerance: int = VIOLATION_TOLERANCE,
) -> ScreenResult:
    """Run all thirteen criteria. Passing needs both enough history and clean ones."""
    years = tuple(sorted(financials))
    results = [_evaluate_ratio(rule, financials, tolerance) for rule in RATIO_RULES]
    results.append(_rising(10, "RetainedEarningsRising", "RetainedEarnings",
                           financials, tolerance))
    results.append(_net_income_trend(financials, tolerance))
    results.append(_treasury_stock(financials))
    results.sort(key=lambda result: result.number)

    passed = len(years) >= years_required and all(
        result.passed for result in results if result.blocking
    )
    return ScreenResult(
        passed=passed,
        criteria=tuple(results),
        years=years,
        years_required=years_required,
    )


def _value(year_facts: dict[str, float], concepts: tuple[str, ...]) -> float | None:
    """Sum of ``concepts``; ``None`` if one is missing and absence means unknown."""
    total = 0.0
    for concept in concepts:
        if concept in year_facts:
            total += year_facts[concept]
        elif concept not in _ABSENT_MEANS_ZERO:
            return None
    return total


def _ratio(rule: RatioRule, year_facts: dict[str, float]) -> float | None:
    """The year's ratio, ``None`` when an input is missing.

    A non-positive denominator is not missing data — it is a bad year: negative
    equity, an operating loss, a gross loss. Scoring it as the worst possible
    value fails the year in the right direction, where dividing by it would flip
    the comparison and let a wrecked balance sheet through.
    """
    numerator = _value(year_facts, rule.numerator)
    denominator = _value(year_facts, rule.denominator)
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        return math.inf if rule.at_most else -math.inf
    return numerator / denominator


def _evaluate_ratio(rule: RatioRule, financials: Financials, tolerance: int) -> CriterionResult:
    violations: list[int] = []
    missing: list[int] = []
    values: list[tuple[int, float]] = []

    for year in sorted(financials):
        ratio = _ratio(rule, financials[year])
        if ratio is None:
            missing.append(year)
            continue
        values.append((year, ratio))
        within = ratio <= rule.limit if rule.at_most else ratio >= rule.limit
        if not within:
            violations.append(year)

    return _result(rule.number, rule.name, violations, missing, values, tolerance)


def _rising(
    number: int,
    name: str,
    concept: str,
    financials: Financials,
    tolerance: int,
) -> CriterionResult:
    """A balance that must climb: every year-on-year fall is a violation."""
    violations: list[int] = []
    missing: list[int] = []
    values: list[tuple[int, float]] = []

    previous: float | None = None
    for year in sorted(financials):
        current = financials[year].get(concept)
        if current is None:
            missing.append(year)
            previous = None  # a gap makes the next comparison meaningless
            continue
        values.append((year, current))
        if previous is not None and current < previous:
            violations.append(year)
        previous = current

    return _result(number, name, violations, missing, values, tolerance)


def _net_income_trend(financials: Financials, tolerance: int) -> CriterionResult:
    """Criterion 12: earnings rising, no loss years.

    Unlike retained earnings this is not required to be monotonic — a flat year
    inside a decade of growth is normal — so only losses, and a decade that ended
    no higher than it started, count against it.
    """
    violations: list[int] = []
    missing: list[int] = []
    values: list[tuple[int, float]] = []

    for year in sorted(financials):
        net_income = financials[year].get("NetIncome")
        if net_income is None:
            missing.append(year)
            continue
        values.append((year, net_income))
        if net_income <= 0:
            violations.append(year)

    if len(values) >= 2 and values[-1][1] <= values[0][1]:
        violations.append(values[-1][0])

    return _result(12, "NetIncomeTrend", violations, missing, values, tolerance)


def _treasury_stock(financials: Financials) -> CriterionResult:
    """Criterion 13: buybacks are a positive signal, not a requirement.

    Non-blocking on purpose. A business that has never repurchased a share can
    still be excellent, so this informs the report rather than rejecting names.
    """
    values = [
        (year, financials[year]["TreasuryStock"])
        for year in sorted(financials)
        if "TreasuryStock" in financials[year]
    ]
    return CriterionResult(
        number=13,
        name="TreasuryStockPresent",
        passed=any(value > 0 for _, value in values),
        violation_years=(),
        missing_years=(),
        values=tuple(values),
        blocking=False,
    )


def _result(
    number: int,
    name: str,
    violations: list[int],
    missing: list[int],
    values: list[tuple[int, float]],
    tolerance: int,
) -> CriterionResult:
    bad = set(violations) | set(missing)
    return CriterionResult(
        number=number,
        name=name,
        passed=len(bad) <= tolerance,
        violation_years=tuple(sorted(set(violations))),
        missing_years=tuple(sorted(set(missing))),
        values=tuple(values),
    )
