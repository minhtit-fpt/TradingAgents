"""The screening pass end to end, with a stubbed market.

No network: prices arrive from ``StubPrices``. What is under test is the order
of operations — criteria first, prices only for survivors — and that every
verdict lands in ``screen_results`` whether or not it produced a valuation.
"""

import unittest

from tradingagents.value.screen import runner
from tradingagents.value.store import db

from .factories import decade, facts_for

AS_OF = "2026-01-01"


class StubPrices:
    """Stands in for ``screen.market``, and counts who it was asked about."""

    def __init__(self, quotes, rate=0.05):
        self.quotes = quotes
        self.rate = rate
        self.asked: list[str] = []

    def close(self, ticker, as_of=None):
        self.asked.append(ticker)
        if ticker not in self.quotes:
            raise runner.market.PriceError(f"no prices for {ticker}")
        return self.quotes[ticker]

    def annual_closes(self, ticker, years, as_of=None):
        return {}

    def split_basis_factors(self, ticker, filed, as_of=None):
        # No stub company splits, so every year is already on one basis.
        return dict.fromkeys(filed, 1.0)

    def risk_free_rate(self, as_of=None):
        return self.rate


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def ingest(self, ticker, series, cik=1):
        db.upsert_facts(self.conn, ticker, cik, facts_for(series))

    def test_a_cheap_excellent_company_triggers_and_is_recorded(self):
        self.ingest("GOOD", decade())
        prices = StubPrices({"GOOD": 1.0})

        outcomes = runner.run(self.conn, AS_OF, prices=prices)

        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].passed)
        self.assertTrue(outcomes[0].triggered)

        row = db.screen_rows(self.conn, AS_OF)[0]
        self.assertEqual(row["ticker"], "GOOD")
        self.assertEqual(row["passed"], 1)
        self.assertEqual(row["price"], 1.0)
        self.assertGreater(row["mos_pct"], 0.30)

    def test_an_excellent_company_at_a_rich_price_passes_without_triggering(self):
        self.ingest("GOOD", decade())

        outcomes = runner.run(self.conn, AS_OF, prices=StubPrices({"GOOD": 10_000.0}))

        self.assertTrue(outcomes[0].passed)
        self.assertFalse(outcomes[0].triggered)
        self.assertLess(db.screen_rows(self.conn, AS_OF)[0]["mos_pct"], 0)

    def test_a_failing_company_is_never_priced(self):
        weak = decade()
        for facts in weak.values():
            facts["NetIncome"] = 10.0  # 1% net margin, and ROE collapses with it
        self.ingest("WEAK", weak)
        prices = StubPrices({"WEAK": 5.0})

        outcomes = runner.run(self.conn, AS_OF, prices=prices)

        self.assertFalse(outcomes[0].passed)
        self.assertEqual(prices.asked, [])
        row = db.screen_rows(self.conn, AS_OF)[0]
        self.assertEqual(row["passed"], 0)
        self.assertIn("NetMargin", row["failed_criteria"])
        self.assertIsNone(row["mos_pct"])

    def test_a_missing_price_keeps_the_criteria_verdict_and_reports_the_gap(self):
        self.ingest("GOOD", decade())

        outcomes = runner.run(self.conn, AS_OF, prices=StubPrices({}))

        self.assertTrue(outcomes[0].passed)
        self.assertIsNone(outcomes[0].valuation)
        self.assertIn("no prices", outcomes[0].error)

        row = db.screen_rows(self.conn, AS_OF)[0]
        self.assertEqual(row["passed"], 1)
        self.assertIsNone(row["intrinsic_value"])

    def test_excluded_companies_are_reported_but_not_screened(self):
        self.ingest("NEW", decade(first_year=2023, years=2))
        prices = StubPrices({"NEW": 5.0})

        outcomes = runner.run(self.conn, AS_OF, prices=prices)

        self.assertEqual(prices.asked, [])
        self.assertTrue(outcomes[0].excluded)
        self.assertEqual(db.screen_rows(self.conn, AS_OF), [])

    def test_rerunning_the_same_day_overwrites_instead_of_accumulating(self):
        self.ingest("GOOD", decade())
        prices = StubPrices({"GOOD": 1.0})

        runner.run(self.conn, AS_OF, prices=prices)
        runner.run(self.conn, AS_OF, prices=prices)

        self.assertEqual(len(db.screen_rows(self.conn, AS_OF)), 1)

    def test_the_report_names_the_funnel_and_the_closest_candidate(self):
        self.ingest("GOOD", decade())

        lines = runner.report(runner.run(self.conn, AS_OF, prices=StubPrices({"GOOD": 1.0})))

        self.assertIn("screened 1", lines[0])
        self.assertIn("closest candidate: GOOD", lines[1])


if __name__ == "__main__":
    unittest.main()
