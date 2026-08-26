"""The forward replay: the point-in-time line, the random baseline, the verdict.

One test here matters more than the rest. ``trailing()`` reads a price frame
that also spans the forward window — it has to, the same cache prices the
outcome — so the slice at the cohort date is the only thing between the decision
and the answer. If it leaks, this module does not fail, it flatters.
"""

import unittest
from datetime import date

import pandas as pd

from tradingagents.value.backtest import stats
from tradingagents.value.dividend import forward

AS_OF = "2016-01-04"
UNTIL = "2021-01-04"


class Frames:
    """A ``prices.History`` stand-in: hands back frames, fetches nothing."""

    def __init__(self, frames: dict):
        self._frames = frames

    def frame(self, ticker: str):
        from tradingagents.value.backtest.prices import PriceError

        if ticker not in self._frames:
            raise PriceError(f"no prices for {ticker}")
        return self._frames[ticker]


def frame(closes: list[float], start: str = "2006-01-02"):
    index = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame({"Close": closes}, index=index)


def steady(n: int, level: float = 100.0):
    return [level] * n


def step_at(when: str, *, before: float = 100.0, after: float = 100.0,
            start: str = "2004-01-02", periods: int = 4200):
    """A flat series that steps to a new level on ``when``, and never moves again.

    Built from the index rather than from bar counts: a test that counts business
    days to place an event lands somewhere slightly different every time the
    calendar does, which is how the first draft of these tests put a crash seven
    bars *before* the cohort date it meant to put it after.
    """
    index = pd.bdate_range(start=start, periods=periods)
    edge = date.fromisoformat(when)
    closes = [after if stamp.date() >= edge else before for stamp in index]
    return pd.DataFrame({"Close": closes}, index=index)


class PointInTime(unittest.TestCase):
    """The line. Nothing after the cohort date may reach the decision."""

    def test_a_crash_after_the_cohort_date_is_invisible_to_the_measurement(self):
        hist = Frames({"CALM": step_at("2016-06-01", before=100.0, after=40.0)})

        row = forward.trailing(hist, "CALM", AS_OF, years=10)
        self.assertEqual(row.max_drawdown, 0.0)
        self.assertEqual(row.volatility, 0.0)

    def test_the_same_frame_read_without_the_cutoff_would_have_seen_it(self):
        """The control for the test above: the crash is in the frame, not absent."""
        hist = Frames({"CALM": step_at("2016-06-01", before=100.0, after=40.0)})

        row = forward.trailing(hist, "CALM", "2019-01-02", years=10)
        self.assertAlmostEqual(row.max_drawdown, -0.6, places=6)

    def test_a_crash_before_the_cohort_date_is_visible(self):
        closes = steady(1200) + [50.0] * 200 + steady(1200)
        hist = Frames({"WOBBLY": frame(closes)})

        row = forward.trailing(hist, "WOBBLY", AS_OF, years=10)
        self.assertAlmostEqual(row.max_drawdown, -0.5, places=2)

    def test_a_name_with_no_price_series_is_dropped_not_guessed(self):
        self.assertIsNone(forward.trailing(Frames({}), "GONE", AS_OF, years=10))

    def test_a_frame_that_starts_after_the_window_is_not_scored(self):
        hist = Frames({"NEW": frame(steady(400), start="2015-01-02")})
        self.assertIsNone(forward.trailing(hist, "NEW", AS_OF, years=10))


class Effect(unittest.TestCase):
    def cohort(self, filtered, random_dd):
        return forward.Cohort(
            as_of=AS_OF,
            until=UNTIL,
            universe=100,
            chosen=("A", "B"),
            filtered_drawdown=filtered,
            random_drawdown=random_dd,
        )

    def test_falling_less_than_random_is_a_positive_effect(self):
        self.assertAlmostEqual(self.cohort(-0.25, -0.32).effect, 0.07)

    def test_falling_further_than_random_is_a_negative_effect(self):
        self.assertAlmostEqual(self.cohort(-0.40, -0.32).effect, -0.08)

    def test_an_unpriced_cohort_carries_no_comparison_rather_than_a_zero(self):
        """A date with no measurement is not a date with no effect."""
        bare = forward.Cohort(as_of=AS_OF, until=UNTIL, universe=100, chosen=())
        self.assertIsNone(bare.effect)
        self.assertFalse(bare.usable)

    def test_mean_effect_ignores_dates_that_could_not_be_measured(self):
        cohorts = [
            self.cohort(-0.25, -0.35),
            self.cohort(-0.30, -0.30),
            forward.Cohort(as_of=AS_OF, until=UNTIL, universe=1, chosen=()),
        ]
        self.assertAlmostEqual(forward.mean_effect(cohorts), 0.05)

    def test_mean_effect_of_nothing_measurable_is_none(self):
        bare = forward.Cohort(as_of=AS_OF, until=UNTIL, universe=1, chosen=())
        self.assertIsNone(forward.mean_effect([bare]))


class Verdict(unittest.TestCase):
    def test_an_interval_straddling_zero_fails_as_not_separated_from_random(self):
        passed, why = forward.verdict(stats.Interval(point=0.03, low=-0.02, high=0.08))
        self.assertFalse(passed)
        self.assertIn("not separated from picking at random", why)

    def test_an_interval_below_zero_fails_as_worse_than_random(self):
        passed, why = forward.verdict(stats.Interval(point=-0.05, low=-0.09, high=-0.01))
        self.assertFalse(passed)
        self.assertIn("fell further than a random one", why)

    def test_an_interval_entirely_above_zero_passes(self):
        passed, _ = forward.verdict(stats.Interval(point=0.06, low=0.02, high=0.10))
        self.assertTrue(passed)

    def test_no_interval_is_no_verdict_rather_than_a_pass(self):
        passed, why = forward.verdict(None)
        self.assertFalse(passed)
        self.assertIn("no verdict", why)


class BookReturn(unittest.TestCase):
    def test_equal_weight_return_averages_the_names_not_the_prices(self):
        hist = Frames(
            {
                "UP": step_at("2016-03-01", before=100.0, after=200.0),
                "FLAT": step_at("2016-03-01"),
            }
        )
        got = forward.book_return(hist, ["UP", "FLAT"], AS_OF, "2016-06-01")
        self.assertAlmostEqual(got, 0.5, places=6)

    def test_a_name_without_prices_drops_out_rather_than_scoring_flat(self):
        hist = Frames({"FLAT": step_at("2016-03-01")})
        self.assertEqual(forward.book_return(hist, ["FLAT", "GONE"], AS_OF, "2016-06-01"), 0.0)

    def test_no_priceable_name_is_none_not_zero(self):
        self.assertIsNone(forward.book_return(Frames({}), ["GONE"], AS_OF, "2016-06-01"))


class Render(unittest.TestCase):
    def test_the_criterion_is_printed_verbatim_above_the_result(self):
        lines = forward.render([], None, size=15)
        self.assertEqual(lines[0], forward.CRITERION)

    def test_a_run_with_no_usable_cohort_reports_no_verdict_rather_than_a_pass(self):
        lines = forward.render([], None, size=15)
        self.assertIn("VERDICT: fail", "\n".join(lines))
        self.assertIn("no verdict", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
