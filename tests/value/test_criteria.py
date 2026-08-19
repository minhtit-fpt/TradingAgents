"""The thirteen criteria at their boundaries, and the tolerance that softens them."""

import unittest

from tradingagents.value.screen import criteria

from .factories import decade


def result_for(series, name, **kwargs):
    return next(c for c in criteria.evaluate(series, **kwargs).criteria if c.name == name)


class ExcellentCompanyTest(unittest.TestCase):
    def test_a_clean_decade_passes_every_criterion(self):
        result = criteria.evaluate(decade())

        self.assertTrue(result.passed)
        self.assertEqual(result.failed_criteria, ())
        self.assertEqual(len(result.criteria), 13)

    def test_nine_years_of_history_is_not_ten(self):
        result = criteria.evaluate(decade(years=9))

        self.assertFalse(result.passed)
        self.assertIn("InsufficientHistory", result.failed_criteria)


class ToleranceTest(unittest.TestCase):
    def test_one_bad_year_is_tolerated_by_default(self):
        series = decade()
        series[2018]["GrossProfit"] = 300.0  # 30% margin, below the 40% floor

        margin = result_for(series, "GrossMargin")

        self.assertEqual(margin.violation_years, (2018,))
        self.assertTrue(margin.passed)

    def test_two_bad_years_are_not(self):
        series = decade()
        series[2018]["GrossProfit"] = 300.0
        series[2019]["GrossProfit"] = 300.0

        margin = result_for(series, "GrossMargin")

        self.assertEqual(margin.violation_years, (2018, 2019))
        self.assertFalse(margin.passed)

    def test_zero_tolerance_rejects_the_single_bad_year(self):
        series = decade()
        series[2018]["GrossProfit"] = 300.0

        self.assertFalse(result_for(series, "GrossMargin", tolerance=0).passed)


class MissingDataTest(unittest.TestCase):
    def test_a_year_that_cannot_be_evaluated_counts_against_the_criterion(self):
        series = decade()
        for year in (2018, 2019):
            del series[year]["Revenue"]

        margin = result_for(series, "GrossMargin")

        self.assertEqual(margin.missing_years, (2018, 2019))
        self.assertFalse(margin.passed)

    def test_absent_research_spending_is_a_legitimate_zero(self):
        series = decade()
        for facts in series.values():
            del facts["RnD"]

        rnd = result_for(series, "RnDToGrossProfit")

        self.assertEqual(rnd.missing_years, ())
        self.assertTrue(rnd.passed)

    def test_absent_long_term_debt_is_a_legitimate_zero(self):
        series = decade()
        for facts in series.values():
            del facts["LongTermDebt"]

        self.assertTrue(result_for(series, "LongTermDebtToNetIncome").passed)


class BadDenominatorTest(unittest.TestCase):
    def test_negative_equity_fails_leverage_instead_of_flipping_it(self):
        series = decade()
        for facts in series.values():
            facts["Equity"] = -100.0
            facts["TreasuryStock"] = 0.0

        leverage = result_for(series, "DebtToEquity")

        self.assertEqual(len(leverage.violation_years), 10)
        self.assertFalse(leverage.passed)

    def test_an_operating_loss_fails_the_interest_cover(self):
        series = decade()
        for facts in series.values():
            facts["OperatingIncome"] = -50.0

        self.assertFalse(result_for(series, "InterestToOperatingIncome").passed)


class TrendTest(unittest.TestCase):
    def test_falling_retained_earnings_is_a_violation_in_the_year_it_falls(self):
        series = decade()
        series[2020]["RetainedEarnings"] = 1.0

        retained = result_for(series, "RetainedEarningsRising")

        # The dip is one bad year; recovering above the old level is not a second.
        self.assertEqual(retained.violation_years, (2020,))
        self.assertTrue(retained.passed)

    def test_a_loss_year_fails_the_earnings_trend(self):
        series = decade()
        series[2018]["NetIncome"] = -10.0
        series[2019]["NetIncome"] = -10.0

        self.assertFalse(result_for(series, "NetIncomeTrend").passed)

    def test_a_decade_that_ends_where_it_started_is_not_rising(self):
        series = decade(growth=0.0)

        trend = result_for(series, "NetIncomeTrend")

        self.assertEqual(trend.violation_years, (2024,))


class NonBlockingTest(unittest.TestCase):
    def test_no_buybacks_is_reported_but_does_not_reject_the_company(self):
        series = decade()
        for facts in series.values():
            del facts["TreasuryStock"]

        result = criteria.evaluate(series)
        treasury = next(c for c in result.criteria if c.name == "TreasuryStockPresent")

        self.assertFalse(treasury.passed)
        self.assertFalse(treasury.blocking)
        self.assertTrue(result.passed)
        self.assertEqual(result.failed_criteria, ())


if __name__ == "__main__":
    unittest.main()
