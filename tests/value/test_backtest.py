"""The replay: cached prices, per-date snapshots, and the caveats.

No network. Prices come from ``price_frame`` through a counting stub, which is
also how the caching claim is tested — a backtest that re-fetched per call would
still pass every other assertion here while taking an hour per run.
"""

import unittest

from tradingagents.value.backtest import numeric, portfolio
from tradingagents.value.backtest.prices import History
from tradingagents.value.screen.market import PriceError
from tradingagents.value.store import db

from .factories import decade, facts_for, price_frame


class CountingFetch:
    """Stands in for ``market._history`` and counts the round-trips."""

    def __init__(self, frames):
        self.frames = frames
        self.calls: list[str] = []

    def __call__(self, ticker, start, end, interval="1d"):
        self.calls.append(ticker)
        if ticker not in self.frames:
            raise PriceError(f"no prices for {ticker} between {start} and {end}")
        return self.frames[ticker]


def history_for(quotes: dict[str, float], rate: float = 4.2) -> tuple[History, CountingFetch]:
    """A ``History`` over flat-priced tickers plus a Treasury series at ``rate``."""
    frames = {ticker: price_frame(price=price) for ticker, price in quotes.items()}
    frames["^TNX"] = price_frame(price=rate)
    fetch = CountingFetch(frames)
    return History("2005-01-01", "2026-06-30", fetch=fetch), fetch


class QuarterEndsTest(unittest.TestCase):
    def test_quarter_ends_are_the_last_calendar_day_of_each_quarter(self):
        self.assertEqual(
            numeric.quarter_ends("2024-01-01", "2024-12-31"),
            ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"],
        )

    def test_dates_outside_the_window_are_excluded(self):
        dates = numeric.quarter_ends("2024-04-01", "2024-10-01")

        self.assertEqual(dates, ["2024-06-30", "2024-09-30"])

    def test_a_window_containing_no_quarter_end_yields_nothing(self):
        self.assertEqual(numeric.quarter_ends("2024-04-01", "2024-05-01"), [])


class HistoryTest(unittest.TestCase):
    def test_a_ticker_is_fetched_once_however_often_it_is_asked_about(self):
        history, fetch = history_for({"GOOD": 100.0})

        history.close("GOOD", "2020-06-30")
        history.close("GOOD", "2021-06-30")
        history.annual_closes("GOOD", 10, "2021-06-30")

        self.assertEqual(fetch.calls, ["GOOD"])

    def test_close_is_the_last_bar_at_or_before_the_as_of_date(self):
        history, _ = history_for({"UP": 100.0})
        history._frames["UP"] = price_frame(price=100.0, drift=0.01)

        early = history.close("UP", "2005-01-10")
        late = history.close("UP", "2010-01-10")

        self.assertLess(early, late)
        self.assertAlmostEqual(early, 100.0 * 1.01**5, places=6)

    def test_a_missing_ticker_is_recorded_once_and_never_refetched(self):
        history, fetch = history_for({"GOOD": 100.0})

        with self.assertRaises(PriceError):
            history.close("DEAD", "2020-06-30")
        with self.assertRaises(PriceError):
            history.close("DEAD", "2021-06-30")

        self.assertEqual(fetch.calls, ["DEAD"])
        self.assertIn("DEAD", history.missing)

    def test_the_risk_free_rate_is_the_treasury_quote_as_a_decimal(self):
        history, _ = history_for({}, rate=4.2)

        self.assertAlmostEqual(history.risk_free_rate("2020-06-30"), 0.042, places=6)

    def test_the_risk_free_rate_is_read_as_of_the_date_not_today(self):
        history, _ = history_for({})
        history._frames["^TNX"] = price_frame(price=1.0, drift=0.001)

        self.assertLess(history.risk_free_rate("2006-01-01"), history.risk_free_rate("2020-01-01"))

    def test_annual_closes_stop_at_the_as_of_date(self):
        history, _ = history_for({"GOOD": 100.0})

        closes = history.annual_closes("GOOD", 10, "2015-06-30")

        self.assertEqual(max(closes), 2015)
        self.assertEqual(min(closes), 2006)


class SnapshotsTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        db.upsert_facts(self.conn, "GOOD", 1, facts_for(decade()))

    def test_a_cheap_excellent_company_is_valued_on_every_date(self):
        history, _ = history_for({"GOOD": 100.0})

        snaps = numeric.snapshots(self.conn, ["2025-03-31", "2025-06-30"], history)

        self.assertEqual([s.passed for s in snaps], [1, 1])
        self.assertEqual([s.valued[0][0] for s in snaps], ["GOOD", "GOOD"])
        self.assertGreater(snaps[0].valued[0][1], 0.30)

    def test_a_date_before_the_first_filing_sees_nothing(self):
        """Point-in-time, at the backtest level: 2014 cannot see a 2016 filing."""
        history, _ = history_for({"GOOD": 100.0})

        snaps = numeric.snapshots(self.conn, ["2014-12-31"], history)

        self.assertEqual(snaps[0].screened, 0)
        self.assertEqual(snaps[0].valued, ())

    def test_the_backtest_never_writes_into_screen_results(self):
        history, _ = history_for({"GOOD": 100.0})

        numeric.snapshots(self.conn, ["2025-03-31"], history)

        self.assertEqual(db.screen_rows(self.conn, "2025-03-31"), [])

    def test_a_date_without_a_discount_rate_is_skipped_and_says_so(self):
        history, _ = history_for({"GOOD": 100.0})
        history._missing["^TNX"] = "no prices for ^TNX"

        snaps = numeric.snapshots(self.conn, ["2025-03-31"], history)

        self.assertIn("^TNX", snaps[0].error)
        self.assertEqual(numeric.schedule_for(snaps, 0.30), [])

    def test_a_company_with_no_price_series_lands_in_the_bias_estimate(self):
        history, _ = history_for({})

        snaps = numeric.snapshots(self.conn, ["2025-03-31"], history)

        self.assertEqual(snaps[0].passed, 1)
        self.assertEqual(snaps[0].valued, ())
        self.assertIn("GOOD", history.missing)


class EndToEndTest(unittest.TestCase):
    """Store to report, the path ``main`` takes minus argparse and the network."""

    def test_a_cheap_compounder_is_screened_bought_and_reported(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.upsert_facts(conn, "GOOD", 1, facts_for(decade()))

        history, _ = history_for({"GOOD": 100.0})
        # Starts cheap and drifts up: cheap enough to clear the 30% trigger on
        # every rebalance date, rising enough that holding it has to show.
        history._frames["GOOD"] = price_frame(price=1.0, drift=0.0005)
        clock = price_frame(price=50.0, drift=0.0002)

        dates = numeric.quarter_ends("2025-01-01", "2026-06-30")
        snaps = numeric.snapshots(conn, dates, history)
        result = portfolio.simulate(
            {"GOOD": history.frame("GOOD")},
            numeric.schedule_for(snaps, 0.30),
            clock,
            start="2025-01-01",
            end="2026-06-30",
            start_cash=100_000.0,
            commission=0.001,
        )

        self.assertGreater(result.end_value, 100_000.0)
        self.assertEqual(result.rejected, 0)

        lines = numeric.report(
            snaps, [(0.30, result)],
            start="2025-01-01", end="2026-06-30", benchmark="SPY",
            benchmark_return=numeric.benchmark_cagr(clock, "2025-01-01", "2026-06-30"),
            missing=history.missing,
        )
        self.assertIn("6 quarterly rebalance dates", lines[0])
        self.assertTrue(any("survivorship: 0 of 1" in line for line in lines))


class ScheduleTest(unittest.TestCase):
    def test_a_higher_trigger_holds_a_subset_of_a_lower_one(self):
        snaps = [numeric.Snapshot("2024-03-31", 3, 3, (("A", 0.45), ("B", 0.32), ("C", 0.10)))]

        self.assertEqual(numeric.schedule_for(snaps, 0.30)[0][1], ("A", "B"))
        self.assertEqual(numeric.schedule_for(snaps, 0.40)[0][1], ("A",))
        self.assertEqual(numeric.schedule_for(snaps, 0.50)[0][1], ())


class BenchmarkTest(unittest.TestCase):
    def test_buy_and_hold_cagr_matches_the_compounded_drift(self):
        frame = price_frame(start="2015-01-01", end="2025-01-01", price=100.0, drift=0.0004)

        cagr = numeric.benchmark_cagr(frame, "2015-01-01", "2025-01-01")

        self.assertGreater(cagr, 0.09)
        self.assertLess(cagr, 0.12)

    def test_a_window_with_one_bar_gives_no_number_rather_than_zero(self):
        frame = price_frame(start="2015-01-01", end="2025-01-01")

        self.assertIsNone(numeric.benchmark_cagr(frame, "2020-01-01", "2020-01-01"))


class ReportTest(unittest.TestCase):
    def test_the_survivorship_caveat_counts_the_names_it_could_not_price(self):
        snaps = [numeric.Snapshot("2024-03-31", 4, 2, (("A", 0.40), ("B", 0.35)))]

        lines = numeric.report(
            snaps, [], start="2024-01-01", end="2024-12-31",
            benchmark="SPY", benchmark_return=0.10,
            missing={"DEAD": "no prices", "GONE": "no prices"},
        )

        caveat = next(line for line in lines if "survivorship" in line)
        self.assertIn("2 of 4 names (50%)", caveat)
        self.assertIn("biased upward", caveat)

    def test_the_restatement_limitation_is_printed_unasked(self):
        lines = numeric.report(
            [], [], start="2024-01-01", end="2024-12-31",
            benchmark="SPY", benchmark_return=None, missing={}, quiet=True,
        )

        self.assertTrue(any("restatements" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
