"""The four dividend criteria, judged over a decade rather than a year.

Pure functions. Same two rules as ``screen/criteria.py``, for the same reasons:
sustained is the binding constraint, and a year that cannot be evaluated is not
a year that passed.

They split across two calendars on purpose, and the split is not cosmetic:

- 1 and 2 read the **per-share** history by calendar year of ex-date. That is
  the series a holder actually received, and the only one on a single share
  basis (``history.py``).
- 3 and 4 read **EDGAR** facts by fiscal year. Coverage is an accounting
  question — did the year's profit and the year's free cash flow fund the year's
  payout — and it has to be asked on the year the accounts were drawn for.

Mixing them would pair a calendar-year dividend with a June fiscal year and call
the difference a finding.
"""

from ..screen.criteria import CriterionResult, Financials, ScreenResult
from . import config


def evaluate(
    dps: dict[int, float],
    financials: Financials,
    *,
    years_required: int = config.HISTORY_YEARS,
    payout_max: float = config.PAYOUT_MAX,
    cut_tolerance: int = config.CUT_TOLERANCE,
    tolerance: int = config.VIOLATION_TOLERANCE,
) -> ScreenResult:
    """Run all four. ``dps`` must already be dense over the window (``history.annual``)."""
    years = tuple(sorted(dps))
    results = (
        _paid_every_year(dps),
        _never_cut(dps, cut_tolerance),
        _payout_ratio(financials, payout_max, tolerance),
        _free_cash_flow_cover(financials, tolerance),
    )
    passed = len(years) >= years_required and all(
        result.passed for result in results if result.blocking
    )
    return ScreenResult(
        passed=passed,
        criteria=results,
        years=years,
        years_required=years_required,
    )


def _paid_every_year(dps: dict[int, float]) -> CriterionResult:
    """Criterion 1: a payment in every year of the window.

    No tolerance, and none configurable. A skipped year is not a bad year for a
    dividend screen, it is the end of the record the screen exists to find.
    """
    values = [(year, dps[year]) for year in sorted(dps)]
    violations = [year for year, amount in values if amount <= 0]
    return CriterionResult(
        number=1,
        name="PaidEveryYear",
        passed=not violations,
        violation_years=tuple(violations),
        missing_years=(),
        values=tuple(values),
    )


def _never_cut(dps: dict[int, float], tolerance: int) -> CriterionResult:
    """Criterion 2: the per-share dividend never falls year on year.

    Flat is not a cut. A board that holds the dividend through a bad year is
    doing the thing this screen is looking for; one that reduces it has told you
    something no ratio will.
    """
    values = [(year, dps[year]) for year in sorted(dps)]
    violations = [
        year
        for (_, previous), (year, current) in zip(values, values[1:], strict=False)
        if current < previous
    ]
    return CriterionResult(
        number=2,
        name="DividendNeverCut",
        passed=len(violations) <= tolerance,
        violation_years=tuple(violations),
        missing_years=(),
        values=tuple(values),
    )


def _payout_ratio(financials: Financials, limit: float, tolerance: int) -> CriterionResult:
    """Criterion 3: dividends stay inside the year's earnings.

    A loss year is a violation, not missing data — paying a dividend out of a
    loss is precisely the behaviour the limit is drawn to catch.
    """
    violations: list[int] = []
    missing: list[int] = []
    values: list[tuple[int, float]] = []

    for year in sorted(financials):
        facts = financials[year]
        paid, net_income = facts.get("DividendsPaid"), facts.get("NetIncome")
        if paid is None or net_income is None:
            missing.append(year)
            continue
        if net_income <= 0:
            values.append((year, float("inf")))
            violations.append(year)
            continue
        ratio = paid / net_income
        values.append((year, ratio))
        if ratio > limit:
            violations.append(year)

    return _result(3, "PayoutRatio", violations, missing, values, tolerance)


def _free_cash_flow_cover(financials: Financials, tolerance: int) -> CriterionResult:
    """Criterion 4: free cash flow covers the payout.

    Earnings are an opinion about timing; the cheque is not. A payer whose
    dividend clears criterion 3 but not this one is funding it from the balance
    sheet, and does that for exactly as long as the balance sheet allows.

    Reported as a coverage multiple, so 1.0 is the line and the values column
    shows how much room each year had.
    """
    violations: list[int] = []
    missing: list[int] = []
    values: list[tuple[int, float]] = []

    for year in sorted(financials):
        facts = financials[year]
        operating, capex = facts.get("OperatingCashFlow"), facts.get("Capex")
        paid = facts.get("DividendsPaid")
        if operating is None or capex is None or paid is None:
            missing.append(year)
            continue
        if paid <= 0:
            # Nothing to cover. Criterion 1 owns whether that is acceptable.
            continue
        cover = (operating - capex) / paid
        values.append((year, cover))
        if cover < 1.0:
            violations.append(year)

    return _result(4, "FreeCashFlowCover", violations, missing, values, tolerance)


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
