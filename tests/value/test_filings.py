"""10-K section extraction: the table of contents must not win, and a filing
published after the as-of date must be invisible."""

import unittest

from tradingagents.value.edgar import filings

BODY_1 = "We sell industrial fasteners through 3,400 branches. " * 40
BODY_1A = "A prolonged construction downturn would reduce branch demand. " * 40
BODY_7 = "Net sales rose 8.2% on price rather than volume. " * 40


def _filing_html() -> str:
    """A 10-K shaped like a real one: a table of contents, then the bodies."""
    return f"""
    <html><head><style>.x{{color:red}}</style></head><body>
      <table>
        <tr><td>Item 1.</td><td>Business</td><td>3</td></tr>
        <tr><td>Item 1A.</td><td>Risk Factors</td><td>9</td></tr>
        <tr><td>Item 1B.</td><td>Unresolved Staff Comments</td><td>21</td></tr>
        <tr><td>Item 7.</td><td>Management's Discussion and Analysis</td><td>30</td></tr>
        <tr><td>Item 7A.</td><td>Quantitative Disclosures</td><td>44</td></tr>
      </table>
      <p>Item 1. Business</p><p>{BODY_1}</p>
      <p>Item 1A. Risk Factors</p><p>{BODY_1A}</p>
      <p>Item 1B. Unresolved Staff Comments</p><p>None.</p>
      <p>Item 7. Management's Discussion and Analysis</p><p>{BODY_7}</p>
      <p>Item 7A. Quantitative Disclosures</p><p>See page 44.</p>
      <script>var tracking = 1;</script>
    </body></html>
    """


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    """Stands in for SecClient; records the URLs asked for."""

    def __init__(self, submissions, document_html=""):
        self.submissions = submissions
        self.document_html = document_html
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        return self.submissions

    def get(self, url):
        self.urls.append(url)
        return _FakeResponse(self.document_html)


def _submissions(rows):
    """``rows`` of (form, filingDate, accessionNumber, primaryDocument)."""
    return {
        "filings": {
            "recent": {
                "form": [row[0] for row in rows],
                "filingDate": [row[1] for row in rows],
                "accessionNumber": [row[2] for row in rows],
                "primaryDocument": [row[3] for row in rows],
            }
        }
    }


class ExtractionTest(unittest.TestCase):
    def setUp(self):
        self.sections = filings.extract(filings.to_text(_filing_html()))

    def test_bodies_are_extracted_not_the_table_of_contents(self):
        # The TOC opens every item a few characters before the next one; the body
        # opens it pages before. Longest span wins, so the TOC entry loses.
        self.assertIn("industrial fasteners", self.sections.business)
        self.assertIn("construction downturn", self.sections.risk_factors)
        self.assertIn("price rather than volume", self.sections.mdna)
        self.assertEqual(self.sections.missing, ())

    def test_a_section_stops_at_the_next_item(self):
        self.assertNotIn("construction downturn", self.sections.business)
        self.assertNotIn("Unresolved Staff Comments", self.sections.risk_factors)
        self.assertNotIn("See page 44", self.sections.mdna)

    def test_scripts_and_styles_are_not_text(self):
        self.assertNotIn("var tracking", self.sections.mdna)
        self.assertNotIn("color:red", self.sections.business)

    def test_a_missing_section_is_reported_not_faked(self):
        html = _filing_html().replace("Item 7. Management's", "Item 9. Other")
        sections = filings.extract(filings.to_text(html))

        self.assertEqual(sections.mdna, "")
        self.assertIn("mdna", sections.missing)

    def test_truncation_records_what_it_dropped(self):
        # 1 token budget == 4 characters, so every section overflows.
        sections = filings.extract(filings.to_text(_filing_html()), token_budget=1)

        self.assertEqual(len(sections.business), 4)
        dropped = dict(sections.dropped)
        self.assertEqual(set(dropped), {"business", "risk_factors", "mdna"})
        self.assertTrue(all(chars > 0 for chars in dropped.values()))

    def test_a_cross_reference_is_not_mistaken_for_a_body(self):
        html = "<html><body><p>Item 1. Business. See Exhibit 99.1.</p>" \
               "<p>Item 2. Properties</p></body></html>"

        self.assertEqual(filings.extract(filings.to_text(html)).business, "")


class FetchTest(unittest.TestCase):
    def test_picks_the_latest_10k_filed_on_or_before_the_as_of_date(self):
        client = _FakeClient(
            _submissions(
                [
                    ("10-K", "2023-02-10", "0000320193-23-000106", "old.htm"),
                    ("10-K", "2024-02-09", "0000320193-24-000123", "current.htm"),
                    ("10-K", "2025-02-07", "0000320193-25-000999", "future.htm"),
                    ("10-Q", "2024-11-01", "0000320193-24-000456", "quarter.htm"),
                ]
            ),
            document_html="<html><body>filing</body></html>",
        )

        filing = filings.fetch_10k(client, 320193, as_of="2024-06-30")

        self.assertEqual(filing.filed, "2024-02-09")
        self.assertIn("000032019324000123/current.htm", filing.url)

    def test_a_filing_published_after_the_as_of_date_is_invisible(self):
        client = _FakeClient(
            _submissions([("10-K", "2025-02-07", "0000320193-25-000999", "future.htm")])
        )

        with self.assertRaises(filings.FilingNotFound):
            filings.fetch_10k(client, 320193, as_of="2024-06-30")

    def test_no_10k_raises_rather_than_returning_another_form(self):
        client = _FakeClient(
            _submissions([("10-Q", "2024-05-01", "0000320193-24-000456", "quarter.htm")])
        )

        with self.assertRaises(filings.FilingNotFound):
            filings.fetch_10k(client, 320193)


if __name__ == "__main__":
    unittest.main()
