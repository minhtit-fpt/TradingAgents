"""The price-stability rank: the two limits, the rank, and what gets dropped.

The line these hold is that a name is only ever compared against names that
lived through the same window. A series that starts after the crash has a
shallow drawdown for a reason that has nothing to do with the company, and
scoring it would put exactly the wrong names at the top of the list.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from tradingagents.value.dividend import stability

START = "2016-01-04"
END = "2026-01-02"


def series(values: list[float], start: str = START, step_days: int = 1):
    """A daily price path with a business-day-ish index, oldest first."""
    first = date.fromisoformat(start)
    index = pd.DatetimeIndex([first + timedelta(days=i * step_days) for i in range(len(values))])
    return pd.Series(values, index=index)


def flat(n: int = 2600, level: float = 100.0):
    return series([level] * n)


def rising(n: int = 2600, rate: float = 0.0002):
    return series([100.0 * (1 + rate) ** i for i in range(n)])


def crashing(n: int = 2600, depth: float = 0.5):
    """Up to a peak, straight down to ``depth`` off it, then flat."""
    half = n // 2
    down = [100.0 - (100.0 * depth) * (i / half) for i in range(n - half)]
    return series([100.0] * half + down)


class Measure(unittest.TestCase):
    def test_flat_series_has_no_volatility_no_fall_and_no_return(self):
        row = stability.measure("FLAT", flat(), START)
        self.assertEqual(row.volatility, 0.0)
        self.assertEqual(row.max_drawdown, 0.0)
        self.assertAlmostEqual(row.annual_return, 0.0, places=6)

    def test_rising_series_annualises_to_a_positive_return(self):
        row = stability.measure("UP", rising(), START)
        self.assertGreater(row.annual_return, 0.0)
        self.assertEqual(row.max_drawdown, 0.0)

    def test_drawdown_is_the_worst_peak_to_trough_fall(self):
        row = stability.measure("DOWN", crashing(depth=0.4), START)
        self.assertAlmostEqual(row.max_drawdown, -0.4, places=2)

    def test_a_series_shorter_than_a_year_is_not_scored(self):
        self.assertIsNone(stability.measure("NEW", flat(n=100), START))

    def test_a_series_starting_after_the_window_is_not_scored(self):
        """The point of the whole file: a late starter missed the crash, not survived it."""
        late = crashing()
        late.index = late.index + timedelta(days=stability.MAX_LATE_START_DAYS + 30)
        self.assertIsNone(stability.measure("LATE", late, START))

    def test_a_start_inside_the_tolerance_is_still_scored(self):
        near = flat()
        near.index = near.index + timedelta(days=stability.MAX_LATE_START_DAYS - 5)
        self.assertIsNotNone(stability.measure("NEAR", near, START))


class Select(unittest.TestCase):
    def rows(self):
        return [
            stability.Stability("CALM", 0.10, -0.10, 0.04),
            stability.Stability("BEST", 0.12, -0.15, 0.11),
            stability.Stability("WILD", 0.60, -0.12, 0.30),
            stability.Stability("DEEP", 0.15, -0.55, 0.25),
            stability.Stability("THIN", 0.11, -0.09, 0.40),
        ]

    def yields(self, **overrides):
        base = {"CALM": 0.04, "BEST": 0.03, "WILD": 0.05, "DEEP": 0.06, "THIN": 0.001}
        return {**base, **overrides}

    def pick(self, **kwargs):
        return stability.select(self.rows(), self.yields(), **{"size": 10, **kwargs})

    def test_every_limit_binds_and_each_cut_is_counted_separately(self):
        chosen, cuts = self.pick(min_yield=0.02, max_volatility=0.28, max_drawdown=0.40)
        self.assertEqual([row.ticker for row in chosen], ["BEST", "CALM"])
        self.assertEqual((cuts.unyielding, cuts.volatile, cuts.deep), (1, 1, 1))

    def test_survivors_rank_by_return_highest_first(self):
        chosen, _ = self.pick(min_yield=0.02)
        self.assertEqual([row.ticker for row in chosen], ["BEST", "CALM"])

    def test_size_truncates_after_the_rank_not_before(self):
        chosen, _ = self.pick(min_yield=0.02, size=1)
        self.assertEqual([row.ticker for row in chosen], ["BEST"])

    def test_a_high_return_name_cannot_buy_its_way_past_a_limit(self):
        """THIN, WILD and DEEP out-return everything and are cut; the rank never rescues."""
        chosen, _ = self.pick(min_yield=0.02, max_volatility=0.28, max_drawdown=0.11)
        self.assertEqual([row.ticker for row in chosen], ["CALM"])

    def test_an_unpriced_name_is_cut_by_the_yield_floor_not_passed_by_it(self):
        chosen, cuts = self.pick(min_yield=0.02, **{})
        self.assertIn("BEST", [row.ticker for row in chosen])
        unpriced, cuts = stability.select(
            self.rows(), self.yields(BEST=None), min_yield=0.02, size=10
        )
        self.assertNotIn("BEST", [row.ticker for row in unpriced])

    def test_an_unpriced_name_is_counted_apart_from_a_name_that_pays_too_little(self):
        """One number for both is how a fetch failure reads as a low payer.

        Live on 2026-08-26: yfinance failed 43 of 153 downloads and the basket
        printed one name instead of three, with ITW and LMT counted as yielding
        too little. Same family as the ADP case ``runner._why`` documents.
        """
        _, cuts = stability.select(
            self.rows(), self.yields(BEST=None), min_yield=0.02, size=10
        )
        self.assertEqual(cuts.unpriced, 1)
        self.assertEqual(cuts.unyielding, 1)

    def test_a_fully_priced_run_reports_no_unpriced_names(self):
        _, cuts = self.pick(min_yield=0.02)
        self.assertEqual(cuts.unpriced, 0)

    def test_the_render_tells_the_operator_to_re_run_rather_than_loosen_a_limit(self):
        lines = stability.render(
            [],
            {},
            None,
            universe=153,
            dropped=0,
            cuts=stability.Cuts(unyielding=81, volatile=23, deep=5, unpriced=43),
            window=("2016-08-26", "2026-08-26"),
            floor=0.05,
        )
        text = "\n".join(lines)
        self.assertIn("43 more had no price", text)
        self.assertIn("Re-run", text)
        self.assertIn("81 yield too little", text)


class Basket(unittest.TestCase):
    def test_the_book_is_measured_as_one_path_not_as_an_average_of_falls(self):
        """Two names that bottom on different days leave the book shallower than either."""
        n = 2000
        early = series([100.0] * 200 + [50.0] * 200 + [100.0] * (n - 400))
        late = series([100.0] * (n - 400) + [50.0] * 200 + [100.0] * 200)
        curves = {"EARLY": early, "LATE": late}

        book = stability.basket(curves, ["EARLY", "LATE"], START)
        self.assertAlmostEqual(book.max_drawdown, -0.25, places=2)
        for row in (
            stability.measure("EARLY", early, START),
            stability.measure("LATE", late, START),
        ):
            self.assertLess(row.max_drawdown, book.max_drawdown)

    def test_no_overlapping_series_is_reported_as_none_not_as_zero(self):
        self.assertIsNone(stability.basket({}, ["NONE"], START))


class Render(unittest.TestCase):
    def head(self, **kwargs):
        defaults = {
            "universe": 100,
            "dropped": 5,
            "cuts": stability.Cuts(unyielding=4, volatile=10, deep=20),
            "window": (START, END),
            "floor": 0.05,
        }
        return stability.render(**{**defaults, **kwargs})

    def test_an_empty_basket_says_so_rather_than_printing_a_table(self):
        lines = self.head(chosen=[], yields={}, book=None)
        self.assertIn("no name cleared all three limits", "\n".join(lines))

    def test_an_unpriced_name_reads_unknown_never_zero(self):
        rows = [stability.Stability("PG", 0.15, -0.20, 0.08)]
        lines = self.head(chosen=rows, yields={"PG": None}, book=None)
        self.assertIn("unknown", "\n".join(lines))
        self.assertNotIn("0.00%", "\n".join(lines))

    def test_the_sizing_line_follows_the_measured_fall(self):
        rows = [stability.Stability("PG", 0.15, -0.20, 0.08)]
        book = stability.Stability("BASKET", 0.14, -0.20, 0.07)
        lines = self.head(chosen=rows, yields={"PG": 0.03}, book=book)
        self.assertIn("at most 25% of capital", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
