"""The replay: forward outcomes, the point-in-time line, and the payout sweep.

The one thing these tests exist to hold is the line the module is built around —
everything on the *decision* side of a cohort date is filtered as-of, and only
the outcome is allowed to see the future. A backtest that leaks across that line
does not fail, it flatters.
"""

import unittest
from dataclasses import replace

from tradingagents.value.backtest import prices
from tradingagents.value.dividend import backtest, store
from tradingagents.value.store import db

from .factories import decade, facts_for, price_frame

YEARS = 10
FIRST_FISCAL = 2010
AS_OF = "2020-01-02"
TODAY = "2026-08-24"
FETCHED = "2020-01-01T00:00:00+00:00"


def rising(first: int = 2009, years: int = 16, start: float = 1.0, growth: float = 0.05):
    """A dividend that never falls, spanning the screen window and the forward one."""
    return {first + i: start * (1 + growth) ** i for i in range(years)}


def payments(dps: dict[int, float]) -> list[tuple[str, float]]:
    return [(f"{year}-06-15", amount) for year, amount in sorted(dps.items())]


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def seed(self, ticker: str, dps: dict[int, float], financials=None) -> None:
        store.upsert(self.conn, ticker, payments(dps), FETCHED)
        # Accession numbers namespaced per ticker: ``facts_for`` numbers them by
        # year alone, and two names sharing one accession collide in the store.
        facts = [
            replace(fact, accn=f"{ticker}-{fact.accn}")
            for fact in facts_for(financials or decade(FIRST_FISCAL, YEARS))
        ]
        db.upsert_facts(self.conn, ticker, 1, facts)


class ForwardOutcomeTest(Base):
    def test_a_rising_dividend_through_the_window_is_not_a_cut(self):
        self.seed("PG", rising())
        self.assertIs(backtest.was_cut(self.conn, "PG", AS_OF, 2), False)

    def test_holding_the_dividend_flat_is_not_a_cut(self):
        dps = rising()
        dps[2020] = dps[2019]
        dps[2021] = dps[2019]
        self.seed("KO", dps)
        self.assertIs(backtest.was_cut(self.conn, "KO", AS_OF, 2), False)

    def test_a_cut_in_the_first_forward_year_is_caught(self):
        # The baseline is 2019 — the last year the screen itself saw. Starting
        # the comparison at 2020 instead would read this as a new record.
        dps = rising()
        dps[2020] = dps[2019] * 0.5
        self.seed("MMM", dps)
        self.assertIs(backtest.was_cut(self.conn, "MMM", AS_OF, 2), True)

    def test_stopping_the_dividend_altogether_counts_as_a_cut(self):
        dps = {year: amount for year, amount in rising().items() if year < 2020}
        self.seed("XYZ", dps)
        self.assertIs(backtest.was_cut(self.conn, "XYZ", AS_OF, 2), True)

    def test_a_name_with_no_baseline_year_has_no_outcome(self):
        # Nothing to be cut from. Scoring it either way would score a year
        # nobody can evaluate.
        self.seed("NEW", {2021: 1.0, 2022: 1.1})
        self.assertIsNone(backtest.was_cut(self.conn, "NEW", AS_OF, 2))

    def test_a_cut_after_the_horizon_is_outside_the_window(self):
        dps = rising()
        dps[2023] = 0.1
        self.seed("LATE", dps)
        self.assertIs(backtest.was_cut(self.conn, "LATE", AS_OF, 2), False)
        self.assertIs(backtest.was_cut(self.conn, "LATE", AS_OF, 4), True)


class PointInTimeTest(Base):
    def test_the_screen_side_cannot_see_a_dividend_paid_after_the_cohort_date(self):
        # 2020 onward is a collapse. The screen standing on 2020-01-02 must
        # still pass the name; only the outcome is allowed to know.
        dps = rising()
        for year in (2020, 2021):
            dps[year] = 0.01
        self.seed("PG", dps)

        verdicts = backtest.verdicts(
            self.conn, "PG", AS_OF, years=YEARS, payout_grid=(0.60,)
        )
        self.assertTrue(verdicts[0.60])
        self.assertIs(backtest.was_cut(self.conn, "PG", AS_OF, 2), True)

    def test_a_name_with_no_facts_yet_is_not_screened_at_all(self):
        # Absent from both arms. Counting it as a rejection would credit the
        # screen with turning down a company it never saw.
        store.upsert(self.conn, "GHOST", payments(rising()), FETCHED)
        self.assertIsNone(
            backtest.verdicts(self.conn, "GHOST", AS_OF, years=YEARS, payout_grid=(0.60,))
        )

    def test_a_name_with_no_dividends_in_the_window_is_not_screened(self):
        db.upsert_facts(self.conn, "NOPAY", 1, facts_for(decade(FIRST_FISCAL, YEARS)))
        self.assertIsNone(
            backtest.verdicts(self.conn, "NOPAY", AS_OF, years=YEARS, payout_grid=(0.60,))
        )


class PayoutGridTest(Base):
    def test_one_read_of_the_inputs_answers_every_limit(self):
        # DividendsPaid at 65% of NetIncome: inside 0.70, outside 0.60.
        financials = {
            year: {**facts, "DividendsPaid": facts["NetIncome"] * 0.65}
            for year, facts in decade(FIRST_FISCAL, YEARS).items()
        }
        self.seed("PEP", rising(), financials)

        verdicts = backtest.verdicts(
            self.conn, "PEP", AS_OF, years=YEARS, payout_grid=(0.50, 0.60, 0.70)
        )
        self.assertEqual(verdicts, {0.50: False, 0.60: False, 0.70: True})


class RunTest(Base):
    def cohorts(self, **kwargs):
        return backtest.run(
            self.conn,
            kwargs.pop("dates", (AS_OF,)),
            horizon=kwargs.pop("horizon", 2),
            years=YEARS,
            payout_grid=(0.60,),
            payout_max=0.60,
            today=kwargs.pop("today", TODAY),
            **kwargs,
        )

    def test_a_cohort_whose_forward_window_has_not_elapsed_is_dropped(self):
        self.seed("PG", rising())
        grid = self.cohorts(dates=("2025-01-02",), today="2026-08-24")
        self.assertEqual(grid[0.60], [])

    def test_passers_and_rejects_land_in_separate_arms(self):
        self.seed("PG", rising())
        cut = rising()
        cut[2015] = 0.1
        self.seed("MMM", cut)

        cohort = self.cohorts()[0.60][0]
        self.assertEqual([r.ticker for r in cohort.passers], ["PG"])
        self.assertEqual([r.ticker for r in cohort.rejects], ["MMM"])
        self.assertEqual(cohort.until, "2022-01-02")

    def test_the_gap_is_positive_when_the_rejects_are_the_ones_that_cut(self):
        self.seed("PG", rising())
        broken = rising()
        broken[2015] = 0.1
        broken[2021] = 0.1
        self.seed("MMM", broken)

        cohorts = self.cohorts()[0.60]
        self.assertEqual(backtest.cut_gap(cohorts), 1.0)

    def test_a_cohort_with_only_one_arm_is_not_usable(self):
        self.seed("PG", rising())
        cohort = self.cohorts()[0.60][0]
        self.assertFalse(cohort.usable)


class BenchmarkTest(Base):
    """A price-only benchmark against total-return names is a wrong number, not a caveat."""

    def history(self):
        frame = price_frame(start="2019-01-01", end="2023-01-01", price=100.0)
        return prices.History("2020-01-02", "2022-01-02", fetch=lambda *a, **k: frame)

    def test_an_uncached_benchmark_stops_the_run(self):
        self.seed("PG", rising())
        with self.assertRaises(backtest.BenchmarkError) as caught:
            backtest.run(
                self.conn, (AS_OF,), horizon=2, years=YEARS, payout_grid=(0.60,),
                payout_max=0.60, hist=self.history(), benchmark="SPY", today=TODAY,
            )
        self.assertIn("price-only", str(caught.exception))

    def test_a_cached_benchmark_runs(self):
        self.seed("PG", rising())
        store.upsert(self.conn, "SPY", payments({2020: 1.5, 2021: 1.6}), FETCHED)
        grid = backtest.run(
            self.conn, (AS_OF,), horizon=2, years=YEARS, payout_grid=(0.60,),
            payout_max=0.60, hist=self.history(), benchmark="SPY", today=TODAY,
        )
        self.assertAlmostEqual(grid[0.60][0].benchmark_return, 3.1 / 100.0, places=6)

    def test_without_prices_the_benchmark_is_not_required(self):
        self.seed("PG", rising())
        grid = backtest.run(
            self.conn, (AS_OF,), horizon=2, years=YEARS, payout_grid=(0.60,),
            payout_max=0.60, hist=None, benchmark="SPY", today=TODAY,
        )
        self.assertEqual(len(grid[0.60]), 1)


class ReturnTest(Base):
    """The one place a price and a dividend meet, and the basis trap that lives there."""

    def history(self, splits=None):
        frame = price_frame(start="2019-01-01", end="2023-01-01", price=100.0, splits=splits)
        return prices.History("2020-01-02", "2022-01-02", fetch=lambda *a, **k: frame)

    def test_dividends_paid_inside_the_window_are_collected(self):
        self.seed("PG", {2019: 2.0, 2020: 3.0, 2021: 4.0})
        got = backtest.total_return(self.conn, self.history(), "PG", AS_OF, "2022-01-02")
        # Flat price, so the whole return is the 2020 and 2021 payments.
        self.assertAlmostEqual(got, 7.0 / 100.0, places=6)

    def test_a_dividend_before_the_entry_date_is_not_collected(self):
        self.seed("PG", {2019: 2.0, 2020: 3.0})
        got = backtest.total_return(self.conn, self.history(), "PG", AS_OF, "2022-01-02")
        self.assertAlmostEqual(got, 3.0 / 100.0, places=6)

    def test_a_split_inside_the_window_does_not_read_as_a_collapse(self):
        # ``AS_TRADED`` undoes the split on the price while the dividend cache
        # stays split-adjusted; putting those two over one line reads a 4-for-1
        # as a 75% loss. The split-adjusted ``Close`` is the basis that matches.
        import pandas as pd

        splits = pd.Series([4.0], index=[pd.Timestamp("2021-01-04")])
        self.seed("AAPL", {2019: 2.0, 2020: 0.0})
        got = backtest.total_return(
            self.conn, self.history(splits), "AAPL", AS_OF, "2022-01-02"
        )
        self.assertAlmostEqual(got, 0.0, places=6)

    def test_a_name_with_no_price_series_returns_none_rather_than_zero(self):
        self.seed("PG", rising())

        def missing(*args, **kwargs):
            raise prices.PriceError("no prices")

        hist = prices.History("2020-01-02", "2022-01-02", fetch=missing)
        self.assertIsNone(
            backtest.total_return(self.conn, hist, "PG", AS_OF, "2022-01-02")
        )


class PriceHistoryTest(unittest.TestCase):
    def test_the_span_starts_before_the_first_cohort_so_a_holiday_still_prices(self):
        # 2012-01-02 was a market holiday. A frame starting on the cohort date
        # has no bar at or before it, and the whole cohort drops out of the
        # return figure without raising anything.
        asked = {}

        def fetch(ticker, start, end, interval):
            asked["start"] = start
            return price_frame(start=start.isoformat(), end="2018-01-01", price=100.0)

        hist = backtest.price_history(("2012-01-02",), 5, fetch=fetch)
        self.assertEqual(backtest._close(hist, "PG", "2012-01-02"), 100.0)
        self.assertLess(asked["start"].isoformat(), "2012-01-02")


class IntervalTest(unittest.TestCase):
    def cohort(self, as_of: str, pass_cut: bool, reject_cut: bool) -> backtest.Cohort:
        return backtest.Cohort(
            as_of=as_of,
            until=as_of,
            results=(
                backtest.NameResult("A", True, pass_cut),
                backtest.NameResult("B", False, reject_cut),
            ),
        )

    def test_too_few_cohorts_gives_no_interval_and_therefore_no_verdict(self):
        cohorts = [self.cohort(f"201{i}-01-02", False, True) for i in range(3)]
        self.assertIsNone(backtest.interval(cohorts, backtest.cut_gap, samples=50))

    def test_a_consistent_gap_produces_an_interval_above_zero(self):
        cohorts = [self.cohort(f"201{i}-01-02", False, True) for i in range(6)]
        got = backtest.interval(cohorts, backtest.cut_gap, samples=200)
        self.assertEqual(got.point, 1.0)
        self.assertGreater(got.low, 0.0)

    def test_disabling_the_bootstrap_disables_the_interval(self):
        cohorts = [self.cohort(f"201{i}-01-02", False, True) for i in range(6)]
        self.assertIsNone(backtest.interval(cohorts, backtest.cut_gap, samples=0))

    def test_render_says_so_rather_than_printing_a_verdict_it_cannot_support(self):
        cohorts = [self.cohort(f"201{i}-01-02", False, True) for i in range(3)]
        text = "\n".join(backtest.render(
            {0.60: cohorts}, payout_max=0.60, horizon=5, benchmark="SPY", samples=50
        ))
        self.assertIn("no interval", text)
        self.assertNotIn("VERDICT", text)


class RenderTest(unittest.TestCase):
    def cohorts(self, gap: bool) -> list[backtest.Cohort]:
        return [
            backtest.Cohort(
                as_of=f"201{i}-01-02",
                until=f"202{i}-01-02",
                results=(
                    backtest.NameResult("A", True, False, 0.5),
                    backtest.NameResult("B", False, gap, -0.1),
                ),
                benchmark_return=0.2,
            )
            for i in range(6)
        ]

    def test_the_criterion_is_printed_before_any_number(self):
        text = "\n".join(backtest.render(
            {0.60: self.cohorts(True)},
            payout_max=0.60, horizon=5, benchmark="SPY", samples=200,
        ))
        self.assertTrue(text.startswith("pre-registered pass criterion"))
        self.assertIn("VERDICT: pass", text)

    def test_a_screen_that_does_not_separate_the_cutters_fails(self):
        text = "\n".join(backtest.render(
            {0.60: self.cohorts(False)},
            payout_max=0.60, horizon=5, benchmark="SPY", samples=200,
        ))
        self.assertIn("VERDICT: fail", text)

    def test_the_return_block_is_labelled_as_gating_nothing(self):
        text = "\n".join(backtest.render(
            {0.60: self.cohorts(True)},
            payout_max=0.60, horizon=5, benchmark="SPY", samples=200,
        ))
        self.assertIn("gates nothing", text)
        self.assertIn("cohort return vs SPY", text)


if __name__ == "__main__":
    unittest.main()


def frame_priced(closes_at, start: str = "2019-01-01", end: str = "2023-01-01"):
    """A price frame whose close on each day comes from ``closes_at(iso_date)``.

    ``factories.price_frame`` only drifts monotonically, and a drawdown that
    never happens is not a test of one.
    """
    import pandas as pd

    from tradingagents.value.screen.market import with_as_traded

    index = pd.bdate_range(start=start, end=end)
    closes = [closes_at(stamp.date().isoformat()) for stamp in index]
    frame = pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000] * len(index),
        },
        index=index,
    )
    return with_as_traded(frame, None)


def flat(price: float):
    return lambda _day: price


def dips(low: float, first: str, last: str, price: float = 100.0):
    """Flat at ``price``, except between ``first`` and ``last`` inclusive."""
    return lambda day: low if first <= day <= last else price


class DrawdownTest(Base):
    """How far down the road went — the only honest answer to "cap the loss at 5%".

    A screen cannot bound a drawdown; an allocation can, and it needs a measured
    number to be sized against. These tests pin what that number counts.
    """

    UNTIL = "2022-01-02"

    def history(self, frames: dict):
        return prices.History(
            "2020-01-02", self.UNTIL, fetch=lambda ticker, *a, **k: frames[ticker]
        )

    def test_a_v_shaped_fall_reports_its_trough(self):
        hist = self.history({"PG": frame_priced(dips(60.0, "2020-07-01", "2020-09-01"))})
        got = backtest.book_drawdown(hist, ["PG"], AS_OF, self.UNTIL)
        self.assertAlmostEqual(got, -0.40, places=6)

    def test_a_book_that_only_rises_has_no_drawdown(self):
        hist = self.history({"PG": frame_priced(flat(100.0))})
        self.assertAlmostEqual(
            backtest.book_drawdown(hist, ["PG"], AS_OF, self.UNTIL), 0.0, places=6
        )

    def test_equal_weight_halves_a_fall_that_hits_one_of_two_names(self):
        # The book is the thing being sized, not the name. One name at 50 while
        # the other holds is a 25% book, and reporting the name's 50% would
        # oversize the cash the operator is told to hold.
        hist = self.history({
            "PG": frame_priced(dips(50.0, "2020-07-01", "2020-09-01")),
            "KO": frame_priced(flat(100.0)),
        })
        got = backtest.book_drawdown(hist, ["PG", "KO"], AS_OF, self.UNTIL)
        self.assertAlmostEqual(got, -0.25, places=6)

    def test_dividends_do_not_cushion_the_fall(self):
        # Price only on purpose: this book is held to be spent from, so the cash
        # has left the account by the time the fall arrives.
        self.seed("PG", rising(start=20.0))
        hist = self.history({"PG": frame_priced(dips(60.0, "2020-07-01", "2020-09-01"))})
        got = backtest.book_drawdown(hist, ["PG"], AS_OF, self.UNTIL)
        self.assertAlmostEqual(got, -0.40, places=6)

    def test_a_name_with_no_price_series_drops_out_rather_than_reading_flat(self):
        def fetch(ticker, *args, **kwargs):
            if ticker == "GONE":
                raise prices.PriceError("delisted")
            return frame_priced(dips(60.0, "2020-07-01", "2020-09-01"))

        hist = prices.History("2020-01-02", self.UNTIL, fetch=fetch)
        got = backtest.book_drawdown(hist, ["PG", "GONE"], AS_OF, self.UNTIL)
        self.assertAlmostEqual(got, -0.40, places=6)

    def test_a_book_with_no_priced_name_is_unknown_not_zero(self):
        def fetch(*args, **kwargs):
            raise prices.PriceError("no prices")

        hist = prices.History("2020-01-02", self.UNTIL, fetch=fetch)
        self.assertIsNone(backtest.book_drawdown(hist, ["PG"], AS_OF, self.UNTIL))

    def test_the_cohort_carries_the_drawdown_of_the_names_that_passed(self):
        self.seed("PG", rising())
        store.upsert(self.conn, "SPY", payments({2020: 1.5, 2021: 1.6}), FETCHED)
        hist = self.history({
            "PG": frame_priced(dips(60.0, "2020-07-01", "2020-09-01")),
            "SPY": frame_priced(flat(100.0)),
        })
        grid = backtest.run(
            self.conn, (AS_OF,), horizon=2, years=YEARS, payout_grid=(0.60,),
            payout_max=0.60, hist=hist, benchmark="SPY", today=TODAY,
        )
        self.assertAlmostEqual(grid[0.60][0].max_drawdown, -0.40, places=6)


class SizingTest(unittest.TestCase):
    def test_a_forty_percent_fall_allows_an_eighth_of_capital_inside_a_five_percent_floor(self):
        self.assertAlmostEqual(backtest.sizing_for_floor(-0.40, 0.05), 0.125, places=6)

    def test_a_fall_shallower_than_the_floor_allows_the_whole_portfolio(self):
        self.assertEqual(backtest.sizing_for_floor(-0.03, 0.05), 1.0)

    def test_a_book_that_never_fell_is_not_sized_above_one(self):
        self.assertEqual(backtest.sizing_for_floor(0.0, 0.05), 1.0)

    def test_the_report_names_the_sizing_rather_than_implying_a_guarantee(self):
        cohort = backtest.Cohort(
            as_of=AS_OF, until="2022-01-02",
            results=(
                backtest.NameResult("PG", True, False),
                backtest.NameResult("XYZ", False, True),
            ),
            max_drawdown=-0.40,
        )
        lines = backtest.render(
            {0.60: [cohort]}, payout_max=0.60, horizon=2, benchmark="SPY",
            samples=0, floor=0.05,
        )
        text = "\n".join(lines)
        self.assertIn("worst across cohorts: -40.0%", text)
        # 12.5% renders as 12: the rounding lands on the smaller allocation,
        # which is the right direction for a line about a loss floor.
        self.assertIn("12% of capital", text)
        self.assertIn("not a recommendation", text)
