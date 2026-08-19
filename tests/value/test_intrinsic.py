"""Valuation on known inputs, and the caps that stop it running away."""

import unittest

from tradingagents.value.screen import intrinsic

from .factories import decade


def flat_eps_decade():
    """Ten years of exactly 1.00 EPS — the arithmetic stays checkable by hand."""
    series = decade(growth=0.0)
    for facts in series.values():
        facts["NetIncome"] = 100.0
        facts["DilutedShares"] = 100.0
    return series


class GrowthFitTest(unittest.TestCase):
    def test_the_fit_recovers_the_growth_rate_it_was_given(self):
        eps = intrinsic.eps_history(decade(growth=0.10))

        self.assertAlmostEqual(intrinsic.fit_growth(eps), 0.10, places=9)

    def test_a_loss_year_makes_growth_unfittable_rather_than_zero(self):
        series = decade()
        series[2018]["NetIncome"] = -50.0

        with self.assertRaises(intrinsic.ValuationError):
            intrinsic.fit_growth(intrinsic.eps_history(series))

    def test_one_year_of_history_cannot_produce_a_trend(self):
        with self.assertRaises(intrinsic.ValuationError):
            intrinsic.fit_growth(intrinsic.eps_history(decade(years=1)))


class ValuationTest(unittest.TestCase):
    def test_a_flat_earner_is_worth_its_discounted_terminal_multiple(self):
        # 1.00 EPS, no growth, 15x terminal, discounted 10 years at 10%.
        valuation = intrinsic.value(flat_eps_decade(), price=10.0, discount_rate=0.10)

        self.assertAlmostEqual(valuation.growth_rate, 0.0, places=9)
        self.assertAlmostEqual(valuation.intrinsic_value, 15 / 1.10**10, places=6)

    def test_margin_of_safety_is_the_discount_to_that_value(self):
        valuation = intrinsic.value(flat_eps_decade(), price=10.0, discount_rate=0.10)
        half = intrinsic.value(
            flat_eps_decade(),
            price=valuation.intrinsic_value / 2,
            discount_rate=0.10,
        )

        self.assertAlmostEqual(half.margin_of_safety, 0.5, places=9)

    def test_a_price_above_value_gives_a_negative_margin(self):
        valuation = intrinsic.value(flat_eps_decade(), price=100.0, discount_rate=0.10)

        self.assertLess(valuation.margin_of_safety, 0)

    def test_a_worthless_price_is_refused_rather_than_valued(self):
        with self.assertRaises(intrinsic.ValuationError):
            intrinsic.value(flat_eps_decade(), price=0.0, discount_rate=0.10)


class CapTest(unittest.TestCase):
    def test_a_spectacular_decade_is_projected_at_the_capped_growth(self):
        valuation = intrinsic.value(decade(growth=0.30), price=50.0, discount_rate=0.05)

        self.assertAlmostEqual(valuation.growth_rate, 0.15, places=9)
        self.assertTrue(valuation.growth_capped)

    def test_ordinary_growth_is_left_alone(self):
        valuation = intrinsic.value(decade(growth=0.10), price=50.0, discount_rate=0.05)

        self.assertAlmostEqual(valuation.growth_rate, 0.10, places=9)
        self.assertFalse(valuation.growth_capped)

    def test_a_collapsed_bond_yield_is_floored(self):
        valuation = intrinsic.value(flat_eps_decade(), price=10.0, discount_rate=0.01)

        self.assertAlmostEqual(valuation.discount_rate, 0.04, places=9)
        self.assertTrue(valuation.discount_floored)

    def test_a_cheap_history_keeps_its_own_multiple(self):
        valuation = intrinsic.value(
            flat_eps_decade(), price=10.0, discount_rate=0.10, median_pe=8.0
        )

        self.assertAlmostEqual(valuation.terminal_pe, 8.0, places=9)

    def test_a_bubble_multiple_is_capped_at_fifteen(self):
        valuation = intrinsic.value(
            flat_eps_decade(), price=10.0, discount_rate=0.10, median_pe=40.0
        )

        self.assertAlmostEqual(valuation.terminal_pe, 15.0, places=9)


class SanityAnchorTest(unittest.TestCase):
    def test_the_graham_number_uses_the_latest_earnings_and_book_value(self):
        series = decade(years=1)  # EPS 2.50, book value per share 10.00

        self.assertAlmostEqual(
            intrinsic.graham_number(series), (22.5 * 2.5 * 10.0) ** 0.5, places=9
        )

    def test_owner_earnings_charge_maintenance_capex_at_depreciation(self):
        series = decade(years=1)  # 250 + 25 D&A - 25 maintenance, over 100 shares

        self.assertAlmostEqual(intrinsic.owner_earnings_per_share(series), 2.5, places=9)

    def test_a_wild_gap_between_the_two_methods_is_flagged(self):
        valuation = intrinsic.value(decade(growth=0.30), price=50.0, discount_rate=0.05)

        self.assertTrue(valuation.graham_disagrees)


if __name__ == "__main__":
    unittest.main()
