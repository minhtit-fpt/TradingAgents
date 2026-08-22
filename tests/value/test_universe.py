"""Who gets screened. ETFs, 20-F filers and SPACs must not.

None of the three is excluded by name. They are excluded by what the store does
and does not hold for them, which is why the same rules keep working when a new
kind of non-operating filer appears.
"""

import unittest

from tradingagents.value.screen import universe
from tradingagents.value.store import db

from .factories import decade, facts_for

AS_OF = "2026-01-01"


class UniverseTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def ingest(self, ticker, series, cik=1):
        db.upsert_facts(self.conn, ticker, cik, facts_for(series))

    def reason_for(self, ticker):
        result = universe.screenable(self.conn, AS_OF)
        return next((e.reason for e in result.excluded if e.ticker == ticker), None)

    def test_an_operating_filer_with_a_full_decade_is_screenable(self):
        self.ingest("GOOD", decade())

        result = universe.screenable(self.conn, AS_OF)

        self.assertEqual(result.tickers, ("GOOD",))
        self.assertEqual(result.excluded, ())

    def test_a_fund_that_never_filed_a_10k_is_excluded(self):
        # ETFs and 20-F filers reach the store the same way: with nothing in it.
        self.ingest("GOOD", decade())

        result = universe.screenable(self.conn, AS_OF, tickers=["GOOD", "SPY"])

        self.assertEqual(result.tickers, ("GOOD",))
        self.assertEqual(len(result.excluded), 1)
        self.assertIn("no 10-K facts", result.excluded[0].reason)

    def test_a_blank_check_shell_with_no_revenue_is_excluded(self):
        shell = decade()
        for facts in shell.values():
            facts["Revenue"] = 0.0

        self.ingest("SPAC", shell)

        self.assertIn("blank-check shell", self.reason_for("SPAC"))

    def test_a_recent_listing_without_ten_years_is_excluded(self):
        self.ingest("NEW", decade(first_year=2021, years=4))

        self.assertIn("only 4 of 10 fiscal years", self.reason_for("NEW"))

    def test_a_company_with_holes_in_its_tags_is_excluded_not_guessed_at(self):
        sparse = decade()
        for facts in sparse.values():
            for concept in ("SGA", "RnD", "OperatingIncome", "Capex", "Equity",
                            "Liabilities", "RetainedEarnings"):
                facts.pop(concept)

        self.ingest("SPARSE", sparse)

        self.assertIn("below the", self.reason_for("SPARSE"))

    def test_a_filer_with_no_cost_of_revenue_is_excluded_not_failed_on_margin(self):
        # McDonald's, Exxon, Union Pacific and every bank report no cost of
        # sales, so gross profit cannot be resolved or derived. Reporting them
        # as failing the margin criteria states a verdict the filing does not
        # support.
        no_cost = decade()
        for facts in no_cost.values():
            facts.pop("GrossProfit")
            facts.pop("CostOfRevenue")

        self.ingest("BANK", no_cost)

        self.assertIn("gross profit resolves for only 0 of 10", self.reason_for("BANK"))

    def test_a_couple_of_missing_gross_profit_years_still_screen(self):
        # Within the violation tolerance the criteria can still reach a verdict,
        # so the company belongs in the screen rather than in the exclusions.
        gappy = decade()
        for year in sorted(gappy)[:2]:
            gappy[year].pop("GrossProfit")
            gappy[year].pop("CostOfRevenue")

        self.ingest("GAPPY", gappy)

        self.assertIsNone(self.reason_for("GAPPY"))

    def test_history_is_judged_as_of_the_date_not_as_of_today(self):
        self.ingest("GOOD", decade())

        # Mid-2019 only five annual reports had been filed.
        result = universe.screenable(self.conn, "2019-06-30")

        self.assertEqual(result.tickers, ())
        self.assertIn("fiscal years", result.excluded[0].reason)


if __name__ == "__main__":
    unittest.main()
