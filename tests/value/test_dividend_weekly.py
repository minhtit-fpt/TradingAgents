"""The weekly job: what broke, what is new, and what it refuses to repeat.

The dedupe table is the part worth testing hardest. A job that repeats itself
gets ignored, and a job that silences an alert it never delivered is worse than
one that never had a dedupe table at all.
"""

import unittest
from datetime import date

from tradingagents.value.dividend import history, ledger, runner, store, weekly
from tradingagents.value.edgar.concepts import Fact
from tradingagents.value.store import db

from .factories import decade

AS_OF = "2025-06-30"
WINDOW = tuple(range(2015, 2025))
STAMP = "2025-06-30T09:00:00+00:00"


def rising(start: float = 1.0) -> dict[int, float]:
    return {year: start * 1.06 ** index for index, year in enumerate(WINDOW)}


def no_fetch(ticker):
    """Stands in for the yfinance call: returns nothing, so the cache is left as seeded."""
    return []


def silent(text):
    """A sender that reports the message did not go out over the network."""
    return False


class Recorder:
    """A sender that succeeds and keeps what it was given."""

    def __init__(self):
        self.messages = []

    def __call__(self, text):
        self.messages.append(text)
        return True


class WeeklyTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def seed(self, ticker, dps=None, financials=None):
        """One screenable name: ten years of facts and ten years of dividends."""
        for year, facts in (financials or decade(2015, 10)).items():
            db.upsert_facts(self.conn, ticker, 1, [
                Fact(concept=concept, fiscal_year=year, period_end=f"{year}-12-31",
                     filed=f"{year + 1}-02-15", value=value, unit="USD",
                     source_tag=concept, accn=f"{ticker}-{year}")
                for concept, value in facts.items()
            ])
        store.upsert(
            self.conn, ticker,
            [(f"{year}-06-15", amount) for year, amount in sorted((dps or rising()).items())],
            "2025-01-01T00:00:00+00:00",
        )

    def buy(self, ticker, shares=10, price=100.0):
        ledger.record_cash(self.conn, "deposit", 10_000.0, happened_on="2015-01-02")
        ledger.record_trade(self.conn, ticker, "buy", shares, price, traded_on="2015-01-05")

    def run_week(self, sender=None, **kwargs):
        sender = sender if sender is not None else Recorder()
        options = {
            "sender": sender,
            "now": lambda: STAMP,
            "fetcher": no_fetch,
            "refresh_limit": 0,
            "prices": lambda ticker, as_of: 120.0,
            "closes": lambda tickers: dict.fromkeys(tickers, 120.0),
            "splits": lambda ticker, trades, as_of: trades,
            **kwargs,
        }
        return weekly.weekly(self.conn, AS_OF, **options), sender

    # --- a dead price feed --------------------------------------------------

    def test_a_dead_price_feed_becomes_a_note_and_leaves_the_breaks_standing(self):
        cut = dict(rising())
        cut[2020] = cut[2019] * 0.5
        self.seed("KO", dps=cut)
        self.buy("KO")
        self.seed("PG")

        def dead(tickers):
            raise history.DividendError("price lookup failed: network is down")

        text, _ = self.run_week(closes=dead)
        self.assertIn("KO", text)
        self.assertIn("candidate yields unavailable", text)
        self.assertIn("yield unknown", text)

    # --- what is held -------------------------------------------------------

    def test_only_open_positions_count_as_held(self):
        self.buy("PG")
        ledger.record_trade(self.conn, "PG", "sell", 10, 130.0, traded_on="2016-01-05")
        self.assertEqual(weekly.held(self.conn), [])

    def test_held_reads_the_ledger_and_not_the_screen(self):
        self.seed("PG")
        self.buy("PG")
        self.assertEqual(weekly.held(self.conn), ["PG"])

    # --- breaks -------------------------------------------------------------

    def test_a_held_name_that_still_passes_is_not_a_break(self):
        self.seed("PG")
        self.buy("PG")
        broken, unknown = weekly.breaks(self.conn, ["PG"], AS_OF)
        self.assertEqual(broken, [])
        self.assertEqual(unknown, [])

    def test_a_cut_dividend_on_a_held_name_is_a_break_naming_the_criterion(self):
        cut = dict(rising())
        cut[2020] = cut[2019] * 0.5
        self.seed("KO", dps=cut)
        self.buy("KO")
        broken, _ = weekly.breaks(self.conn, ["KO"], AS_OF)
        self.assertEqual(broken[0].ticker, "KO")
        self.assertIn("DividendNeverCut", broken[0].failed)

    def test_a_held_name_with_no_data_is_reported_not_scored(self):
        self.buy("XYZ")
        broken, unknown = weekly.breaks(self.conn, ["XYZ"], AS_OF)
        self.assertEqual(broken, [])
        self.assertIn("XYZ", unknown[0])

    # --- candidates ---------------------------------------------------------

    def test_candidates_exclude_what_is_already_held(self):
        self.seed("PG")
        self.seed("JNJ")
        self.buy("PG")
        fresh, _ = weekly.candidates(self.conn, AS_OF, {"PG"})
        self.assertEqual([outcome.ticker for outcome in fresh], ["JNJ"])

    def test_names_with_no_cached_history_are_counted_not_listed(self):
        self.seed("PG")
        store.upsert(self.conn, "ZZZ", [("2024-06-15", 1.0)], "2025-01-01T00:00:00+00:00")
        fresh, skipped = weekly.candidates(self.conn, AS_OF, set())
        self.assertEqual([outcome.ticker for outcome in fresh], ["PG"])
        self.assertEqual(skipped, 1)

    # --- the message --------------------------------------------------------

    def test_a_quiet_week_still_sends_one_message(self):
        text, sender = self.run_week()
        self.assertEqual(len(sender.messages), 1)
        self.assertIn("BROKEN: none", text)
        self.assertIn("CANDIDATES: none", text)

    def test_breaks_are_reported_above_candidates(self):
        cut = dict(rising())
        cut[2020] = cut[2019] * 0.5
        self.seed("KO", dps=cut)
        self.seed("PG")
        self.buy("KO")
        text, _ = self.run_week()
        self.assertLess(text.index("BROKEN"), text.index("CANDIDATES"))
        self.assertIn("KO NEW", text)
        self.assertIn("PG NEW", text)

    def test_the_message_names_no_action(self):
        self.seed("PG")
        self.buy("PG")
        text, _ = self.run_week()
        self.assertIn("Not a recommendation, not an order, not sized.", text)

    def test_a_dead_price_feed_does_not_suppress_the_breaks(self):
        cut = dict(rising())
        cut[2020] = 0.1
        self.seed("KO", dps=cut)
        self.buy("KO")

        def broken_prices(ticker, as_of):
            raise RuntimeError("no price")

        text, _ = self.run_week(prices=broken_prices)
        self.assertIn("KO", text)
        self.assertIn("book not valued", text)

    # --- saying it once -----------------------------------------------------

    def test_the_same_break_is_announced_once(self):
        cut = dict(rising())
        cut[2020] = cut[2019] * 0.5
        self.seed("KO", dps=cut)
        self.buy("KO")

        first, _ = self.run_week()
        self.assertIn("KO NEW", first)
        second, _ = self.run_week()
        self.assertIn("KO", second)
        self.assertNotIn("KO NEW", second)

    def test_a_second_criterion_breaking_is_a_new_signature_and_speaks_again(self):
        cut = dict(rising())
        cut[2020] = cut[2019] * 0.5
        self.seed("KO", dps=cut)
        self.buy("KO")
        self.run_week()

        # The payout now blows through the limit as well: a different failure,
        # and the one worth interrupting for.
        strained = {
            year: {**facts, "DividendsPaid": facts["NetIncome"] * 0.9}
            for year, facts in decade(2015, 10).items()
        }
        self.seed("KO", dps=cut, financials=strained)
        text, _ = self.run_week()
        self.assertIn("KO NEW", text)

    def test_an_alert_that_never_went_out_is_said_again_next_week(self):
        cut = dict(rising())
        cut[2020] = cut[2019] * 0.5
        self.seed("KO", dps=cut)
        self.buy("KO")

        first, _ = self.run_week(sender=silent)
        self.assertIn("KO NEW", first)
        second, _ = self.run_week()
        self.assertIn("KO NEW", second)

    def test_a_dry_run_writes_nothing_and_cannot_silence_the_real_run(self):
        cut = dict(rising())
        cut[2020] = cut[2019] * 0.5
        self.seed("KO", dps=cut)
        self.buy("KO")

        self.run_week(record=False)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM dividend_alerts").fetchone()[0], 0
        )
        text, _ = self.run_week()
        self.assertIn("KO NEW", text)


if __name__ == "__main__":
    unittest.main()


LAST_FULL_YEAR = date.today().year - 1


class YieldTest(unittest.TestCase):
    """What a candidate pays at today's price — the column a book bought for income needs.

    The screen's own order is clean-share, which answers how durable the payout
    is and not how much of it there is. Both questions matter to a book that is
    spent from, and only one of them was on the message before this.
    """

    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def seed(self, ticker: str, last_full: float) -> None:
        store.upsert(
            self.conn, ticker,
            [(f"{LAST_FULL_YEAR}-06-15", last_full)],
            "2026-01-01T00:00:00+00:00",
        )

    def test_the_yield_is_last_full_year_over_todays_price(self):
        self.seed("PG", 4.00)
        self.assertAlmostEqual(weekly.forward_yield(self.conn, "PG", 100.0), 0.04, places=6)

    def test_a_name_with_no_cached_history_yields_unknown_not_zero(self):
        # A 0.00% that actually means "no data" is the confidently wrong number
        # the module refuses everywhere else.
        self.assertIsNone(weekly.forward_yield(self.conn, "NEW", 100.0))

    def test_a_name_with_no_price_yields_unknown_rather_than_a_number(self):
        self.seed("PG", 4.00)
        self.assertIsNone(weekly.forward_yield(self.conn, "PG", None))

    def test_the_candidate_list_is_ranked_by_income(self):
        self.seed("LOW", 2.00)
        self.seed("HIGH", 6.00)
        ranked, yields = weekly.rank_by_yield(
            self.conn, [runner.Outcome("LOW"), runner.Outcome("HIGH")],
            closes=lambda tickers: dict.fromkeys(tickers, 100.0),
        )
        self.assertEqual([o.ticker for o in ranked], ["HIGH", "LOW"])
        self.assertAlmostEqual(yields["HIGH"], 0.06, places=6)

    def test_an_unpriced_name_keeps_its_place_behind_the_priced_ones(self):
        self.seed("PG", 2.00)
        ranked, _ = weekly.rank_by_yield(
            self.conn, [runner.Outcome("NOPRICE"), runner.Outcome("PG")],
            closes=lambda tickers: {"PG": 100.0},
        )
        self.assertEqual([o.ticker for o in ranked], ["PG", "NOPRICE"])

    def test_every_candidate_is_priced_in_one_request(self):
        # The earlier version priced the top 25 by clean-share, which is an
        # alphabetical cut once a hundred names tie at 100% clean. A ranking over
        # a quarter of the list ranks nothing.
        names = [f"T{index:02d}" for index in range(40)]
        for ticker in names:
            self.seed(ticker, 1.00)
        calls = []

        def closes(tickers):
            calls.append(tuple(tickers))
            return dict.fromkeys(tickers, 100.0)

        _, yields = weekly.rank_by_yield(
            self.conn, [runner.Outcome(t) for t in names], closes=closes
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 40)
        self.assertTrue(all(rate is not None for rate in yields.values()))

    def test_a_dead_price_feed_raises_out_of_the_ranking(self):
        def dead(tickers):
            raise history.DividendError("price lookup failed")

        with self.assertRaises(history.DividendError):
            weekly.rank_by_yield(self.conn, [runner.Outcome("PG")], closes=dead)

    def test_the_message_prints_the_yield_and_says_so_when_it_is_unknown(self):
        from types import SimpleNamespace

        fresh = [
            SimpleNamespace(ticker="PG", result=SimpleNamespace(quality=0.95)),
            SimpleNamespace(ticker="KO", result=SimpleNamespace(quality=0.90)),
        ]
        text = weekly.compose(
            AS_OF, [], set(), fresh, set(), None, None, [],
            {"PG": 0.035, "KO": None},
        )
        self.assertIn("PG: clean 95%, yield 3.50%", text)
        self.assertIn("KO: clean 90%, yield unknown", text)
