"""A company is a CIK; a ticker is only its current label.

Two mechanisms put a company in the store under one string and in a historical
index snapshot under another:

- **rename** — Bank of New York Mellon files continuously, and rebranded `BK` to
  `BNY` in 2025. Both strings are the same CIK.
- **share-class relabelling** — `GOOGL`/`GOOG`, `FOXA`/`FOX`, `NWSA`/`NWS`.

Neither needs a refetch. The store's primary key is already ``(cik, ...)``, so
the fix is to rewrite the historical ticker to whichever one the store holds that
CIK under. What is left after that is genuinely missing and belongs to the ingest
list, not here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping


def store_ciks(conn: sqlite3.Connection) -> dict[int, str]:
    """``{cik: ticker}`` for everything the store holds facts for.

    A CIK filed under two tickers keeps the alphabetically first, so the mapping
    is stable across runs rather than dependent on insertion order.
    """
    rows = conn.execute("SELECT DISTINCT cik, ticker FROM facts ORDER BY cik, ticker")
    held: dict[int, str] = {}
    for cik, ticker in rows:
        held.setdefault(int(cik), str(ticker))
    return held


def aliases(conn: sqlite3.Connection, ticker_to_cik: Mapping[str, int]) -> dict[str, str]:
    """``{historical_ticker: store_ticker}`` for names the store holds elsewhere.

    Only tickers the store does *not* hold directly get an entry, so applying the
    result is a no-op for every name already screenable.
    """
    held = store_ciks(conn)
    directly = set(held.values())
    return {
        ticker: held[cik]
        for ticker, cik in ticker_to_cik.items()
        if ticker not in directly and cik in held
    }


def apply(tickers: Iterable[str], alias_map: Mapping[str, str]) -> tuple[str, ...]:
    """Rewrite ``tickers`` through ``alias_map``, deduplicated and sorted.

    Deduplication matters: an index holding both `GOOGL` and `GOOG` collapses to
    one company, and screening it twice would double its weight in the portfolio.
    """
    return tuple(sorted({alias_map.get(t, t) for t in tickers}))
