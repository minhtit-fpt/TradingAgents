"""The on-demand dossier: the operator's evidence, and the entry price.

No network and no paid call. Prices come from ``StubPrices``, the filing read is
patched. What is under test is that the dossier reports *why* — the years that
failed a criterion, the price the trigger fires at, and an unread filing saying
so rather than looking like a clean one.
"""

import unittest
from unittest.mock import patch

from tradingagents.value import report
from tradingagents.value.analyst.schemas import (
    Concentration,
    Confidence,
    Moat,
    MoatTrend,
    ValueAssessment,
    Verdict,
)
from tradingagents.value.config import MARGIN_OF_SAFETY_MIN
from tradingagents.value.edgar.filings import Filing, Sections
from tradingagents.value.store import db

from .factories import decade, facts_for
from .test_runner import StubPrices

AS_OF = "2026-01-01"

ASSESSMENT = ValueAssessment(
    ticker="GOOD",
    verdict=Verdict.CAUTION,
    moat=Moat.WIDE,
    moat_trend=MoatTrend.STABLE,
    customer_concentration=Concentration.MODERATE,
    accounting_flags=["receivables up 30% against flat revenue"],
    key_risks=["one plant makes 60% of units"],
    thesis="A good business at a fair price.",
    confidence=Confidence.MEDIUM,
    evidence_gaps=[],
)

FILING = Filing(cik=1, accession="0000000000-26-000001", filed="2025-12-01",
                url="https://example.invalid/10k.htm", html="<html></html>")
SECTIONS = Sections(business="b", risk_factors="r", mdna="m")


class DossierTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def ingest(self, ticker="GOOD", series=None, cik=1):
        db.upsert_facts(self.conn, ticker, cik, facts_for(series or decade()))

    def build(self, price=1.0, **kwargs):
        return report.build(
            self.conn, "GOOD", AS_OF,
            prices=StubPrices({"GOOD": price}),
            ingest_missing=False,
            **kwargs,
        )

    def test_trigger_price_is_the_price_at_which_the_configured_mos_is_reached(self):
        self.ingest()

        dossier = self.build(price=100.0)
        valuation = dossier.outcome.valuation

        self.assertIsNotNone(valuation)
        self.assertAlmostEqual(
            dossier.trigger_price,
            valuation.intrinsic_value * (1 - MARGIN_OF_SAFETY_MIN),
        )
        # At exactly the trigger price, the margin of safety is the trigger.
        implied = (valuation.intrinsic_value - dossier.trigger_price) / valuation.intrinsic_value
        self.assertAlmostEqual(implied, MARGIN_OF_SAFETY_MIN)
        self.assertIn(f"{MARGIN_OF_SAFETY_MIN:.0%} MoS needs price <=", "\n".join(report.render(dossier)))

    def test_a_failed_criterion_names_the_years_that_failed_it(self):
        series = decade()
        # Three years of collapsed gross margin: one past the tolerance of two, so
        # it fails, and the render has to name the years rather than only the verdict.
        for year in (2017, 2018, 2019):
            series[year]["GrossProfit"] = 10.0
            series[year]["CostOfRevenue"] = series[year]["Revenue"] - 10.0
        self.ingest(series=series)

        rendered = "\n".join(report.render(self.build(price=100.0)))

        self.assertIn("FAILED", rendered)
        self.assertRegex(rendered, r"\[FAIL\].*GrossMargin.*violated 2017, 2018, 2019")

    def test_the_filing_section_says_it_was_not_read_when_it_was_not(self):
        self.ingest()

        rendered = "\n".join(report.render(self.build(price=100.0)))

        self.assertIn("not read (pass --read-filing", rendered)
        self.assertNotIn("caution", rendered)

    def test_a_read_filing_is_reported_as_a_read_and_never_as_a_veto(self):
        self.ingest()

        with patch.object(report.filings, "sections_for", return_value=(FILING, SECTIONS)), \
             patch.object(report.value_analyst, "assess", return_value=ASSESSMENT) as assess:
            dossier = self.build(price=100.0, with_filing=True, client=object())

        self.assertEqual(dossier.assessment, ASSESSMENT)
        # The screen's own verdict travels into the prompt, so the analyst argues
        # with the numeric case instead of re-deriving it.
        self.assertIn("thirteen-criterion screen", assess.call_args.args[2])

        rendered = "\n".join(report.render(dossier))
        self.assertIn("not a veto", rendered)
        # Phase 8 demoted the verdict rather than removing it: it is still there,
        # but every field phase 6 found actually checkable comes first, and the
        # 70% that makes `caution` near-meaningless is printed beside it.
        self.assertIn("verdict: caution", rendered)
        self.assertIn("70%", rendered)
        self.assertLess(rendered.index("accounting flags:"), rendered.index("verdict:"))
        # A caution does not remove the entry price: the operator still decides.
        self.assertIn("MoS needs price <=", rendered)

    def test_an_unreachable_filing_is_a_note_not_a_substituted_verdict(self):
        self.ingest()
        boom = report.filings.FilingNotFound("no 10-K on or before 2026-01-01")

        with patch.object(report.filings, "sections_for", side_effect=boom):
            dossier = self.build(price=100.0, with_filing=True, client=object())

        self.assertIsNone(dossier.assessment)
        self.assertTrue(any("filing not read" in n for n in dossier.notes))
        self.assertIn("filing not read", "\n".join(report.render(dossier)))

    def test_a_name_with_no_price_still_reports_quality_and_says_why_not_valued(self):
        self.ingest()

        dossier = report.build(
            self.conn, "GOOD", AS_OF,
            prices=StubPrices({}),  # no quote for GOOD
            ingest_missing=False,
        )
        rendered = "\n".join(report.render(dossier))

        self.assertIsNone(dossier.trigger_price)
        self.assertIn("PASSED", rendered)
        self.assertIn("not valued", rendered)
        self.assertIn("no valuation, so no trigger price", rendered)


if __name__ == "__main__":
    unittest.main()
