"""The replay: cached prices, per-date snapshots, and the caveats.

No network. Prices come from ``price_frame`` through a counting stub, which is
also how the caching claim is tested — a backtest that re-fetched per call would
still pass every other assertion here while taking an hour per run.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.value.backtest import membership, numeric, portfolio, stats
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


class PointInTimeUniverseTest(unittest.TestCase):
    """Phase 4b step 1: the universe a rebalance screens is the one that existed.

    Two names, only one of which is in the index on the earlier date. The static
    universe screens both on both dates — which is the defect that produced the
    item-8 and item-9 verdicts — and the point-in-time universe does not.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        db.upsert_facts(self.conn, "GOOD", 1, facts_for(decade()))
        db.upsert_facts(self.conn, "LATER", 2, facts_for(decade()))
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        path = Path(self._dir.name) / "members.csv"
        path.write_text(
            'date,tickers\n'
            '2025-01-02,"GOOD"\n'
            '2025-05-01,"GOOD,LATER"\n',
            encoding="utf-8",
        )
        self.members = membership.load(path)

    def test_a_name_absent_from_the_index_is_not_screened_on_that_date(self):
        history, _ = history_for({"GOOD": 100.0, "LATER": 100.0})

        snaps = numeric.snapshots(
            self.conn, ["2025-03-31", "2025-06-30"], history,
            universe=self.members.as_of,
        )

        self.assertEqual([s.screened for s in snaps], [1, 2])
        self.assertEqual([t for t, _ in snaps[0].valued], ["GOOD"])
        self.assertEqual([t for t, _ in snaps[1].valued], ["GOOD", "LATER"])

    def test_the_static_universe_screens_both_names_on_both_dates(self):
        """The behaviour being replaced, pinned so the difference is visible."""
        history, _ = history_for({"GOOD": 100.0, "LATER": 100.0})

        snaps = numeric.snapshots(self.conn, ["2025-03-31", "2025-06-30"], history)

        self.assertEqual([s.screened for s in snaps], [2, 2])

    def test_each_snapshot_records_how_many_names_were_in_the_index(self):
        history, _ = history_for({"GOOD": 100.0, "LATER": 100.0})

        snaps = numeric.snapshots(
            self.conn, ["2025-03-31", "2025-06-30"], history,
            universe=self.members.as_of,
        )

        self.assertEqual([s.universe for s in snaps], [1, 2])

    def test_an_explicit_ticker_filter_narrows_the_point_in_time_universe(self):
        history, _ = history_for({"GOOD": 100.0, "LATER": 100.0})

        snaps = numeric.snapshots(
            self.conn, ["2025-06-30"], history,
            tickers=["LATER"], universe=self.members.as_of,
        )

        self.assertEqual([t for t, _ in snaps[0].valued], ["LATER"])

    def test_a_date_outside_the_membership_data_is_skipped_and_says_so(self):
        """Never a silent empty universe: that reads as 'nothing qualified'."""
        history, _ = history_for({"GOOD": 100.0})

        snaps = numeric.snapshots(
            self.conn, ["2024-12-31"], history, universe=self.members.as_of,
        )

        self.assertIn("membership", snaps[0].error)
        self.assertEqual(snaps[0].screened, 0)
        self.assertEqual(numeric.schedule_for(snaps, 0.30), [])

    def test_an_index_member_the_store_never_ingested_is_counted_separately(self):
        """A name absent from the store is survivorship exposure; a name that is
        present but too thin to screen is an ordinary exclusion. Reporting one
        number for both would attribute the coverage floor to survivorship."""
        db.upsert_facts(self.conn, "THIN", 3, facts_for(decade(years=3)))
        path = Path(self._dir.name) / "members2.csv"
        path.write_text(
            'date,tickers\n2025-01-02,"GOOD,THIN,NEVER"\n', encoding="utf-8"
        )
        history, _ = history_for({"GOOD": 100.0})

        snaps = numeric.snapshots(
            self.conn, ["2025-03-31"], history,
            universe=membership.load(path).as_of,
        )

        self.assertEqual(snaps[0].universe, 3)
        self.assertEqual(snaps[0].screened, 1)
        self.assertEqual(snaps[0].absent, 1)  # NEVER; THIN is an exclusion

    def test_the_report_separates_absence_from_exclusion(self):
        db.upsert_facts(self.conn, "THIN", 3, facts_for(decade(years=3)))
        path = Path(self._dir.name) / "members3.csv"
        path.write_text(
            'date,tickers\n2025-01-02,"GOOD,THIN,NEVER"\n', encoding="utf-8"
        )
        history, _ = history_for({"GOOD": 100.0})
        snaps = numeric.snapshots(
            self.conn, ["2025-03-31"], history,
            universe=membership.load(path).as_of,
        )

        lines = numeric.report(
            snaps, [], start="2025-01-01", end="2025-12-31",
            benchmark="SPY", benchmark_return=0.10, missing=history.missing,
        )

        self.assertTrue(any("absent from the store" in line for line in lines))

    def test_the_report_names_the_universe_it_used(self):
        history, _ = history_for({"GOOD": 100.0, "LATER": 100.0})
        snaps = numeric.snapshots(
            self.conn, ["2025-03-31", "2025-06-30"], history,
            universe=self.members.as_of,
        )

        lines = numeric.report(
            snaps, [], start="2025-01-01", end="2025-12-31",
            benchmark="SPY", benchmark_return=0.10, missing=history.missing,
        )

        self.assertTrue(any("point-in-time" in line for line in lines))
        self.assertTrue(any("in the index" in line for line in lines))


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
        # The path, not just the endpoints: step 3's intervals are resampled from
        # it, so a simulation that returned no curve would silently disable the
        # verdict rather than fail.
        self.assertTrue(result.curve)
        self.assertAlmostEqual(result.curve[-1][1], result.end_value, places=2)

        lines = numeric.report(
            snaps, [(0.30, result)],
            start="2025-01-01", end="2026-06-30", benchmark="SPY",
            benchmark_return=numeric.benchmark_cagr(clock, "2025-01-01", "2026-06-30"),
            missing=history.missing,
        )
        self.assertIn("6 quarterly rebalance dates", lines[0])
        self.assertTrue(any("survivorship: 0 of 1" in line for line in lines))

    def test_the_report_carries_the_interval_the_criterion_is_judged_on(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.upsert_facts(conn, "GOOD", 1, facts_for(decade()))

        history, _ = history_for({"GOOD": 100.0})
        history._frames["GOOD"] = price_frame(price=1.0, drift=0.0005)
        clock = price_frame(price=50.0, drift=0.0002)

        dates = numeric.quarter_ends("2014-01-01", "2026-06-30")
        snaps = numeric.snapshots(conn, dates, history)
        result = portfolio.simulate(
            {"GOOD": history.frame("GOOD")},
            numeric.schedule_for(snaps, 0.30),
            clock,
            start="2014-01-01", end="2026-06-30",
            start_cash=100_000.0, commission=0.001,
        )
        schedule = numeric.schedule_for(snaps, 0.30)
        power = stats.summary(
            [snap for snap in snaps if not snap.error],
            result.curve, stats.curve_of(clock),
            held={day: len(names) for day, names in schedule},
            setting="the configured trigger 30%",
            years=result.years, benchmark="SPY", samples=200,
        )

        lines = numeric.report(
            snaps, [(0.30, result)],
            start="2014-01-01", end="2026-06-30", benchmark="SPY",
            benchmark_return=numeric.benchmark_cagr(clock, "2014-01-01", "2026-06-30"),
            missing=history.missing, power=power, quiet=True,
        )
        text = "\n".join(lines)

        self.assertIn("pre-registered pass criterion", text)
        self.assertIn("CAGR vs SPY", text)
        self.assertIn("VERDICT:", text)
        # The grid must not read as evidence on its own any more.
        self.assertIn("sensitivity display", text)


class ScheduleTest(unittest.TestCase):
    def test_a_higher_trigger_holds_a_subset_of_a_lower_one(self):
        snaps = [numeric.Snapshot("2024-03-31", 3, 3, (("A", 0.45), ("B", 0.32), ("C", 0.10)))]

        self.assertEqual(numeric.schedule_for(snaps, 0.30)[0][1], ("A", "B"))
        self.assertEqual(numeric.schedule_for(snaps, 0.40)[0][1], ("A",))
        self.assertEqual(numeric.schedule_for(snaps, 0.50)[0][1], ())


class RankSelectionTest(unittest.TestCase):
    """Phase 4b step 4: margin of safety stops gating and starts tie-breaking."""

    def snap(self) -> numeric.Snapshot:
        return numeric.Snapshot(
            "2024-03-31", 10, 4,
            valued=(("A", 0.10), ("B", 0.55), ("C", 0.40), ("D", 0.40)),
            quality=(("A", 0.99), ("B", 0.80), ("C", 0.90), ("D", 0.90)),
        )

    def test_the_best_quality_is_held_even_when_it_is_the_most_expensive(self):
        # A has the highest quality and the *worst* margin of safety — under the
        # trigger it would never be bought at all.
        self.assertEqual(self.snap().top_ranked(2), ("A", "C"))
        self.assertEqual(self.snap().triggered(0.30), ("B", "C", "D"))

    def test_margin_of_safety_breaks_a_tie_in_quality(self):
        # C and D are both 0.90; C is cheaper.
        self.assertEqual(self.snap().top_ranked(3), ("A", "C", "D"))

    def test_asking_for_more_names_than_qualified_holds_what_there_is(self):
        self.assertEqual(len(self.snap().top_ranked(50)), 4)

    def test_a_name_with_no_quality_score_ranks_last_rather_than_first(self):
        snap = numeric.Snapshot(
            "2024-03-31", 10, 2,
            valued=(("UNKNOWN", 0.90), ("SCORED", 0.10)),
            quality=(("SCORED", 0.75),),
        )

        self.assertEqual(snap.top_ranked(1), ("SCORED",))

    def test_the_grid_is_labelled_by_position_count_under_rank_selection(self):
        result = portfolio.Result(
            start_value=100.0, end_value=110.0, years=1.0, max_drawdown=0.1,
            trades=4, winners=3, average_bars_held=60.0,
        )

        lines = numeric.report(
            [self.snap()], [(10.0, result)],
            start="2024-01-01", end="2024-12-31", benchmark="SPY",
            benchmark_return=0.10, missing={}, quiet=True,
            selection="rank", configured=10.0,
        )
        text = "\n".join(lines)

        self.assertIn("quality rank", text)
        self.assertIn("top  10 *", text)
        self.assertIn("VALUE_BACKTEST_TOP_N", text)

    def test_the_ranked_schedule_skips_dates_the_screen_could_not_run(self):
        good = self.snap()
        bad = numeric.Snapshot("2024-06-30", 0, 0, (), error="no discount rate")

        schedule = numeric.schedule_ranked([good, bad], 2)

        self.assertEqual(schedule, [("2024-03-31", ("A", "C"))])


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


class ConstructionTest(unittest.TestCase):
    """Phase 4b step 5: what the *book* does with the picks, not what it picks."""

    @staticmethod
    def snap(as_of: str, valued, passed_names=None) -> numeric.Snapshot:
        names = passed_names if passed_names is not None else tuple(t for t, _ in valued)
        return numeric.Snapshot(
            as_of, len(names), len(names), tuple(valued), passed_names=tuple(names)
        )

    def trigger(self, level: float):
        return lambda snap: snap.triggered(level)

    def test_no_book_is_opened_when_fewer_than_the_minimum_names_qualify(self):
        snaps = [self.snap("2024-03-31", (("A", 0.45), ("B", 0.40)))]

        schedule = numeric.construct(
            snaps, self.trigger(0.30), minimum=5, maximum=15, quality_exit=False
        )

        self.assertEqual(schedule, [("2024-03-31", ())])

    def test_the_book_opens_once_the_minimum_is_reached(self):
        valued = tuple((chr(65 + i), 0.40) for i in range(5))
        snaps = [self.snap("2024-03-31", valued)]

        schedule = numeric.construct(
            snaps, self.trigger(0.30), minimum=5, maximum=15, quality_exit=False
        )

        self.assertEqual(schedule[0][1], ("A", "B", "C", "D", "E"))

    def test_the_minimum_never_forces_a_liquidation_once_invested(self):
        # Five names open the book; next quarter only two still trigger, but all
        # five still clear the criteria. Selling three is the trade step 5 exists
        # to prevent, so the book is held whole.
        opening = tuple((chr(65 + i), 0.40) for i in range(5))
        thinner = (("A", 0.40), ("B", 0.40), ("C", 0.05), ("D", 0.05), ("E", 0.05))
        snaps = [self.snap("2024-03-31", opening), self.snap("2024-06-30", thinner)]

        schedule = numeric.construct(
            snaps, self.trigger(0.30), minimum=5, maximum=15, quality_exit=True
        )

        self.assertEqual(schedule[1][1], ("A", "B", "C", "D", "E"))

    def test_a_holding_is_sold_when_it_stops_clearing_the_criteria(self):
        # C keeps a wide margin of safety and is dropped anyway: it is absent
        # from the criteria pass, which is the only exit step 5 recognises.
        opening = tuple((chr(65 + i), 0.40) for i in range(5))
        snaps = [
            self.snap("2024-03-31", opening),
            self.snap("2024-06-30", (("A", 0.40), ("B", 0.40), ("D", 0.05), ("E", 0.05))),
        ]

        schedule = numeric.construct(
            snaps, self.trigger(0.30), minimum=5, maximum=15, quality_exit=True
        )

        self.assertNotIn("C", schedule[1][1])
        self.assertEqual(schedule[1][1], ("A", "B", "D", "E"))

    def test_without_the_quality_exit_the_book_is_rebuilt_from_the_picks(self):
        opening = tuple((chr(65 + i), 0.40) for i in range(5))
        thinner = (("A", 0.40), ("B", 0.40), ("C", 0.05), ("D", 0.05), ("E", 0.05))
        snaps = [self.snap("2024-03-31", opening), self.snap("2024-06-30", thinner)]

        schedule = numeric.construct(
            snaps, self.trigger(0.30), minimum=5, maximum=15, quality_exit=False
        )

        # Two triggered, the minimum is not met, and nothing is carried: flat.
        self.assertEqual(schedule[1][1], ())

    def test_the_maximum_turns_away_new_ideas_rather_than_selling_holdings(self):
        opening = tuple((chr(65 + i), 0.40) for i in range(5))
        crowded = opening + (("X", 0.90), ("Y", 0.90))
        snaps = [self.snap("2024-03-31", opening), self.snap("2024-06-30", crowded)]

        schedule = numeric.construct(
            snaps, self.trigger(0.30), minimum=5, maximum=6, quality_exit=True
        )

        # X has the widest margin of safety on the date and is still the one cut.
        self.assertEqual(schedule[1][1], ("A", "B", "C", "D", "E", "X"))

    def test_a_name_that_passed_but_could_not_be_valued_is_still_a_holding(self):
        # B has no price at the second date, so it never reaches ``valued``. That
        # is a missing quote, not a quality exit, and must not sell the position.
        opening = (("A", 0.40), ("B", 0.40), ("C", 0.40), ("D", 0.40), ("E", 0.40))
        snaps = [
            self.snap("2024-03-31", opening),
            self.snap(
                "2024-06-30",
                (("A", 0.40), ("C", 0.40), ("D", 0.40), ("E", 0.40)),
                passed_names=("A", "B", "C", "D", "E"),
            ),
        ]

        schedule = numeric.construct(
            snaps, self.trigger(0.30), minimum=5, maximum=15, quality_exit=True
        )

        self.assertIn("B", schedule[1][1])

    def test_dates_the_screen_could_not_run_are_skipped(self):
        snaps = [numeric.Snapshot("2024-03-31", 0, 0, (), error="no discount rate")]

        self.assertEqual(
            numeric.construct(
                snaps, self.trigger(0.30), minimum=1, maximum=15, quality_exit=True
            ),
            [],
        )


class PositionCapTest(unittest.TestCase):
    """The cap is arithmetic inside backtrader, so it is tested as arithmetic."""

    def test_the_cap_binds_only_when_the_book_is_thinner_than_its_reciprocal(self):
        budget = 1.0 - portfolio.CASH_BUFFER

        self.assertAlmostEqual(min(0.15, budget / 2), 0.15)
        self.assertAlmostEqual(min(0.15, budget / 6), 0.15)
        # Seven names at equal weight are already inside a 15% cap.
        self.assertAlmostEqual(min(0.15, budget / 7), budget / 7)

    def test_a_capped_run_leaves_cash_uninvested_when_few_names_qualify(self):
        # Two names, both rising. Uncapped they take 98% of NAV between them;
        # capped they take 30% and the rest sits in cash, so the capped run must
        # capture materially less of the same rally.
        window = {"start": "2024-01-01", "end": "2024-12-31"}
        frames = {
            t: price_frame(**window, price=100.0, drift=0.001) for t in ("A", "B")
        }
        schedule = [("2024-03-31", ("A", "B"))]
        clock = price_frame(**window, price=400.0)

        def run(cap: float):
            return portfolio.simulate(
                frames, schedule, clock, **window,
                start_cash=100_000.0, commission=0.0, position_cap=cap,
            )

        capped, uncapped = run(0.15), run(1.0)

        self.assertEqual(capped.rejected, 0)
        self.assertEqual(uncapped.rejected, 0)
        self.assertGreater(uncapped.end_value, 100_000.0)
        self.assertGreater(uncapped.end_value, capped.end_value)
        # 30% of NAV exposed against 98%: roughly a third of the gain.
        gain_ratio = (capped.end_value - 100_000.0) / (uncapped.end_value - 100_000.0)
        self.assertAlmostEqual(gain_ratio, 0.30 / 0.98, delta=0.05)


class CommandLineTest(unittest.TestCase):
    """The parser is built at call time, so nothing else here exercises it.

    Step 5 shipped a help string containing a literal ``15%``; argparse
    %-expands help text and refused to build the parser at all. Every other test
    in this file passed, because none of them reach ``main``.
    """

    def test_the_parser_builds(self):
        with self.assertRaises(SystemExit) as raised:
            numeric.main(["--help"])

        self.assertEqual(raised.exception.code, 0)

    def test_a_window_with_no_quarter_end_exits_rather_than_simulating_nothing(self):
        self.assertEqual(numeric.main(["--start", "2024-01-01", "--end", "2024-01-05"]), 2)
