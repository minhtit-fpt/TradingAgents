"""Store behaviour: idempotent writes and an honest coverage report."""

import unittest

from tradingagents.value.edgar.concepts import CONCEPT_NAMES, Fact
from tradingagents.value.store import db


def fact(concept, fiscal_year, value=1.0, filed=None, tag="Tag", accn="acc-1"):
    return Fact(
        concept=concept,
        fiscal_year=fiscal_year,
        period_end=f"{fiscal_year}-12-31",
        filed=filed or f"{fiscal_year + 1}-02-10",
        value=value,
        unit="USD",
        source_tag=tag,
        accn=accn,
    )


class UpsertTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_reingesting_the_same_filing_does_not_duplicate_it(self):
        facts = [fact("NetIncome", 2024, 210.0)]
        db.upsert_facts(self.conn, "ACME", 1234567, facts)
        db.upsert_facts(self.conn, "ACME", 1234567, facts)

        rows = db.facts_as_of(self.conn, "ACME", "2026-01-01")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 210.0)

    def test_tickers_lists_what_was_ingested(self):
        db.upsert_facts(self.conn, "ACME", 1, [fact("NetIncome", 2024)])
        db.upsert_facts(self.conn, "beta", 2, [fact("NetIncome", 2024)])

        self.assertEqual(db.tickers(self.conn), ["ACME", "BETA"])


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_the_report_names_every_concept_including_the_missing_ones(self):
        db.upsert_facts(self.conn, "ACME", 1, [fact("NetIncome", 2024)])

        report = db.coverage(self.conn, "ACME", "2026-01-01", years=10)

        self.assertEqual(set(report), set(CONCEPT_NAMES))
        self.assertEqual(report["NetIncome"]["years"], 1)
        self.assertEqual(report["Revenue"]["years"], 0)
        self.assertEqual(report["Revenue"]["tags"], [])

    def test_the_report_records_which_tag_resolved_the_concept(self):
        db.upsert_facts(self.conn, "ACME", 1, [
            fact("Revenue", 2016, tag="SalesRevenueNet"),
            fact("Revenue", 2019, tag="RevenueFromContractWithCustomerExcludingAssessedTax"),
        ])

        report = db.coverage(self.conn, "ACME", "2026-01-01", years=10)

        self.assertEqual(
            report["Revenue"]["tags"],
            ["RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
        )

    def test_missing_years_are_named_not_just_counted(self):
        db.upsert_facts(self.conn, "ACME", 1, [
            fact("NetIncome", 2022), fact("NetIncome", 2023), fact("NetIncome", 2024),
            fact("Revenue", 2022), fact("Revenue", 2024),
        ])

        report = db.coverage(self.conn, "ACME", "2026-01-01", years=10)

        self.assertEqual(report["Revenue"]["missing_years"], [2023])

    def test_coverage_ratio_is_the_share_of_filled_cells(self):
        full = {name: {"years": 10, "tags": [], "missing_years": []} for name in CONCEPT_NAMES}
        empty = {name: {"years": 0, "tags": [], "missing_years": []} for name in CONCEPT_NAMES}

        self.assertAlmostEqual(db.coverage_ratio(full, 10), 1.0)
        self.assertAlmostEqual(db.coverage_ratio(empty, 10), 0.0)

    def test_a_ticker_with_no_facts_reports_zero_rather_than_raising(self):
        report = db.coverage(self.conn, "NOBODY", "2026-01-01", years=10)

        self.assertEqual(db.coverage_ratio(report, 10), 0.0)


class CikLookupTest(unittest.TestCase):
    """EDGAR is addressed by CIK; phase 6 reaches for filings ticker by ticker."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_the_cik_comes_back_for_a_ticker_the_store_holds(self):
        db.upsert_facts(self.conn, "ACME", 1234567, [fact("NetIncome", 2024)])

        self.assertEqual(db.cik_for(self.conn, "acme"), 1234567)

    def test_a_ticker_with_no_facts_is_none_rather_than_a_raise(self):
        self.assertIsNone(db.cik_for(self.conn, "NOBODY"))


if __name__ == "__main__":
    unittest.main()
