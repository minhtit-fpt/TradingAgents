"""Ticker identity: a company is a CIK, not a string.

The store's primary key is already ``(cik, ...)``, but the screen looks names up
by ticker — so when Bank of New York Mellon rebranded `BK` to `BNY` in 2025, a
2014 rebalance date screening `BK` found nothing, and the company silently left
a decade of history behind. Item 9 of the phase-4 findings recorded that as one
of two mechanisms behind a 38% hole in the universe.

These tests pin the resolution: SEC's own two ticker files merged into one
ticker->CIK map, and a historical ticker mapped onto whatever ticker the store
already holds that CIK under. No refetch, no re-ingest, no guesswork.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.value.edgar import tickermap
from tradingagents.value.screen import identity
from tradingagents.value.store import db

from .factories import decade, facts_for


class _Response:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _Client:
    """Serves the two SEC ticker files and counts the round-trips."""

    TICKER_TXT = "aapl\t320193\nbny\t1390777\nbk\t1390777\natvi\t718877\nbf.b\t14693\n"
    COMPANY_TICKERS = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1467373, "ticker": "ACN", "title": "Accenture plc"},
    }

    def __init__(self):
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return _Response(text=self.TICKER_TXT)

    def get_json(self, url):
        self.calls += 1
        return self.COMPANY_TICKERS


class TickerMapTest(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.cache = Path(self._dir.name) / "ticker_cik.json"
        self.client = _Client()

    def test_both_sec_files_are_merged(self):
        mapping = tickermap.load(self.client, cache_path=self.cache)

        self.assertEqual(mapping["ATVI"], 718877)   # ticker.txt only
        self.assertEqual(mapping["ACN"], 1467373)   # company_tickers.json only

    def test_share_classes_are_normalised_to_the_store_convention(self):
        mapping = tickermap.load(self.client, cache_path=self.cache)

        self.assertEqual(mapping["BF-B"], 14693)
        self.assertNotIn("BF.B", mapping)

    def test_a_renamed_company_keeps_both_tickers_on_one_cik(self):
        mapping = tickermap.load(self.client, cache_path=self.cache)

        self.assertEqual(mapping["BK"], mapping["BNY"])

    def test_the_map_is_cached_so_a_second_load_makes_no_request(self):
        tickermap.load(self.client, cache_path=self.cache)
        before = self.client.calls

        again = tickermap.load(self.client, cache_path=self.cache)

        self.assertEqual(self.client.calls, before)
        self.assertEqual(again["ATVI"], 718877)

    def test_refresh_ignores_the_cache(self):
        tickermap.load(self.client, cache_path=self.cache)
        before = self.client.calls

        tickermap.load(self.client, cache_path=self.cache, refresh=True)

        self.assertGreater(self.client.calls, before)


class AliasTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        # The store holds Bank of New York Mellon under its 2025 ticker.
        db.upsert_facts(self.conn, "BNY", 1390777, facts_for(decade()))
        self.mapping = {"BK": 1390777, "BNY": 1390777, "ATVI": 718877}

    def test_a_historical_ticker_resolves_to_the_ticker_the_store_holds(self):
        aliases = identity.aliases(self.conn, self.mapping)

        self.assertEqual(aliases["BK"], "BNY")

    def test_a_ticker_the_store_already_holds_is_not_aliased(self):
        aliases = identity.aliases(self.conn, self.mapping)

        self.assertNotIn("BNY", aliases)

    def test_a_company_the_store_never_ingested_gets_no_alias(self):
        """ATVI resolves to a CIK, but no CIK means no facts to point at. It is
        step 2's ingest list, not an alias."""
        aliases = identity.aliases(self.conn, self.mapping)

        self.assertNotIn("ATVI", aliases)

    def test_applying_aliases_rewrites_a_members_list_without_duplicates(self):
        aliases = identity.aliases(self.conn, self.mapping)

        self.assertEqual(identity.apply(("BK", "BNY", "ATVI"), aliases), ("ATVI", "BNY"))


if __name__ == "__main__":
    unittest.main()
