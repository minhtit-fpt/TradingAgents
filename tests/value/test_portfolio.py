"""Portfolio accounting through backtrader, on prices with known answers.

Every frame here compounds at a fixed rate per bar, so the right end value is
arithmetic rather than a number copied out of a previous run. That is the point:
these tests fail if the wiring silently stops trading — a backtest that quietly
holds cash for ten years reports a flat line, not an error.
"""

import unittest

from tradingagents.value.backtest import portfolio

from .factories import price_frame

START = "2020-01-01"
END = "2021-12-31"


def frames(**drifts):
    return {
        ticker: price_frame(start="2019-01-01", end="2022-06-30", price=100.0, drift=drift)
        for ticker, drift in drifts.items()
    }


def simulate(frames_, schedule, clock=None, cash=100_000.0, commission=0.0):
    return portfolio.simulate(
        frames_,
        schedule,
        clock if clock is not None else price_frame(start="2019-01-01", end="2022-06-30"),
        start=START,
        end=END,
        start_cash=cash,
        commission=commission,
    )


class SimulateTest(unittest.TestCase):
    def test_holding_a_riser_ends_richer_than_it_started(self):
        result = simulate(frames(UP=0.001), [("2020-01-06", ("UP",))])

        self.assertGreater(result.end_value, result.start_value)
        self.assertGreater(result.cagr, 0)

    def test_holding_nothing_leaves_the_cash_untouched(self):
        result = simulate(frames(UP=0.001), [("2020-01-06", ())])

        self.assertAlmostEqual(result.end_value, 100_000.0, places=2)
        self.assertEqual(result.trades, 0)
        self.assertIsNone(result.hit_rate)

    def test_a_two_name_target_is_held_at_equal_weight(self):
        """Half in a riser and half in a flat name beats neither and both."""
        both = simulate(frames(UP=0.001, FLAT=0.0), [("2020-01-06", ("UP", "FLAT"))])
        only_up = simulate(frames(UP=0.001), [("2020-01-06", ("UP",))])

        self.assertGreater(both.end_value, 100_000.0)
        self.assertLess(both.end_value, only_up.end_value)

    def test_selling_a_name_dropped_from_the_target_closes_the_position(self):
        result = simulate(
            frames(UP=0.001),
            [("2020-01-06", ("UP",)), ("2020-07-01", ())],
        )

        self.assertEqual(result.trades, 1)
        self.assertEqual(result.winners, 1)
        self.assertEqual(result.hit_rate, 1.0)
        self.assertGreater(result.average_bars_held, 100)

    def test_a_losing_round_trip_is_counted_as_a_loss(self):
        result = simulate(
            frames(DOWN=-0.001),
            [("2020-01-06", ("DOWN",)), ("2020-07-01", ())],
        )

        self.assertEqual(result.trades, 1)
        self.assertEqual(result.winners, 0)
        self.assertLess(result.end_value, 100_000.0)

    def test_commission_makes_the_same_schedule_end_poorer(self):
        free = simulate(frames(UP=0.001), [("2020-01-06", ("UP",))], commission=0.0)
        charged = simulate(frames(UP=0.001), [("2020-01-06", ("UP",))], commission=0.01)

        self.assertLess(charged.end_value, free.end_value)

    def test_a_rebalance_date_on_a_holiday_still_happens_on_the_next_session(self):
        # 2020-07-04 was a Saturday; the schedule must not be skipped for it.
        result = simulate(frames(UP=0.001), [("2020-07-04", ("UP",))])

        self.assertGreater(result.end_value, 100_000.0)

    def test_the_drawdown_of_a_falling_holding_is_reported(self):
        result = simulate(frames(DOWN=-0.001), [("2020-01-06", ("DOWN",))])

        self.assertGreater(result.max_drawdown, 0.1)

    def test_a_scheduled_name_with_no_price_frame_is_skipped_not_fatal(self):
        result = simulate(frames(UP=0.001), [("2020-01-06", ("UP", "DEAD"))])

        self.assertGreater(result.end_value, 100_000.0)

    def test_an_ordinary_run_has_no_refused_orders(self):
        result = simulate(frames(UP=0.001), [("2020-01-06", ("UP",))])

        self.assertEqual(result.rejected, 0)

    def test_a_refused_order_is_counted_rather_than_absorbed(self):
        """The target is sized off the close and filled at the next open.

        A market that opens 10% above the close it was sized from cannot be
        bought with the cash on hand, and the broker says no.
        """
        gapper = price_frame(start="2019-01-01", end="2022-06-30", price=100.0)
        gapper["Open"] = gapper["Open"] * 1.10

        result = simulate({"GAP": gapper}, [("2020-01-06", ("GAP",))])

        self.assertGreater(result.rejected, 0)
        self.assertEqual(result.end_value, 100_000.0)


class ResultTest(unittest.TestCase):
    def test_cagr_is_none_rather_than_zero_when_the_span_is_empty(self):
        result = portfolio.Result(100.0, 200.0, 0.0, 0.0, 0, 0, 0.0)

        self.assertIsNone(result.cagr)

    def test_doubling_over_two_years_annualises_to_the_square_root(self):
        result = portfolio.Result(100.0, 200.0, 2.0, 0.0, 0, 0, 0.0)

        self.assertAlmostEqual(result.cagr, 2**0.5 - 1, places=6)
        self.assertAlmostEqual(result.total_return, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
