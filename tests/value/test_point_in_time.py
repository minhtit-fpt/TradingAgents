"""The most important test in this module: nothing is visible before it was filed.

Look-ahead through fundamentals is not a style problem. FY2024 statements did
not exist on 2024-12-31 — they were filed weeks later — and a backtest that can
see them will report an edge that never existed.
"""

import unittest

from tradingagents.value.edgar.concepts import Fact
from tradingagents.value.store import db


def fact(concept, fiscal_year, value, filed, accn="acc-1", period_end=None):
    return Fact(
        concept=concept,
        fiscal_year=fiscal_year,
        period_end=period_end or f"{fiscal_year}-12-31",
        filed=filed,
        value=value,
        unit="USD",
        source_tag="NetIncomeLoss",
        accn=accn,
    )


class PointInTimeTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _as_of(self, as_of, concept="NetIncome"):
        return {
            row["fiscal_year"]: row["value"]
            for row in db.facts_as_of(self.conn, "ACME", as_of, years=20)
            if row["concept"] == concept
        }

    def test_a_fact_filed_after_the_as_of_date_is_invisible(self):
        db.upsert_facts(self.conn, "ACME", 1234567, [
            fact("NetIncome", 2024, 210.0, filed="2025-02-18"),
        ])

        self.assertEqual(self._as_of("2024-12-31"), {})
        self.assertEqual(self._as_of("2025-02-17"), {})
        self.assertEqual(self._as_of("2025-02-18"), {2024: 210.0})

    def test_a_restatement_is_invisible_until_it_is_filed(self):
        db.upsert_facts(self.conn, "ACME", 1234567, [
            fact("NetIncome", 2008, 4834.0, filed="2009-10-27", accn="acc-2009"),
            fact("NetIncome", 2008, 6119.0, filed="2010-10-27", accn="acc-2010"),
        ])

        self.assertEqual(self._as_of("2010-01-01"), {2008: 4834.0})
        self.assertEqual(self._as_of("2011-01-01"), {2008: 6119.0})

    def test_the_latest_filing_at_or_before_the_as_of_date_wins(self):
        db.upsert_facts(self.conn, "ACME", 1234567, [
            fact("NetIncome", 2020, 10.0, filed="2021-02-10", accn="a"),
            fact("NetIncome", 2020, 11.0, filed="2022-02-10", accn="b"),
            fact("NetIncome", 2020, 12.0, filed="2023-02-10", accn="c"),
        ])

        self.assertEqual(self._as_of("2022-06-01"), {2020: 11.0})

    def test_the_year_window_keeps_the_most_recent_years(self):
        db.upsert_facts(self.conn, "ACME", 1234567, [
            fact("NetIncome", year, float(year), filed=f"{year + 1}-02-10")
            for year in range(2010, 2025)
        ])

        rows = db.facts_as_of(self.conn, "ACME", "2026-01-01", years=3)

        self.assertEqual(sorted({row["fiscal_year"] for row in rows}), [2022, 2023, 2024])

    def test_an_unknown_ticker_returns_nothing_rather_than_everything(self):
        db.upsert_facts(self.conn, "ACME", 1234567, [
            fact("NetIncome", 2024, 210.0, filed="2025-02-18"),
        ])

        self.assertEqual(db.facts_as_of(self.conn, "OTHER", "2026-01-01"), [])


if __name__ == "__main__":
    unittest.main()
