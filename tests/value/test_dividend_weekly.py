"""The weekly job: what broke, what is new, and what it refuses to repeat.

The dedupe table is the part worth testing hardest. A job that repeats itself
gets ignored, and a job that silences an alert it never delivered is worse than
one that never had a dedupe table at all.
"""

import unittest

from tradingagents.value.dividend import ledger, store, weekly
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
            "splits": lambda ticker, trades, as_of: trades,
            **kwargs,
        }
        return weekly.weekly(self.conn, AS_OF, **options), sender

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
