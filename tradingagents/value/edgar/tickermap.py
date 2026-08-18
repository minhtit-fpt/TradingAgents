"""Ticker to CIK, from both of SEC's own ticker files.

``company_tickers.json`` alone is not enough, and the gap is not academic: of the
179 historical S&P 500 members the store lacked, it resolved 11. It carries
current listings and has holes even there — item 9 recorded `AEP` and `CMA`
missing from it while both were still filing. ``include/ticker.txt`` is the wider
file (~12k rows, including issuers delisted within the last few years) and
resolves 67 of the same set. Merged, they cover both.

The map is cached on disk because it changes on SEC's schedule, not on ours, and
a backtest that refetched it per run would spend two requests to learn nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from ..config import TICKER_MAP_PATH
from .companyfacts import COMPANY_TICKERS_URL

TICKER_TXT_URL = "https://www.sec.gov/include/ticker.txt"


class _Client(Protocol):
    def get(self, url: str) -> Any: ...
    def get_json(self, url: str) -> Any: ...


def normalise(ticker: str) -> str:
    """SEC writes share classes as ``BF.B``; the store and yfinance use ``BF-B``."""
    return ticker.strip().upper().replace(".", "-")


def load(
    client: _Client,
    *,
    cache_path: Path | str | None = None,
    refresh: bool = False,
) -> dict[str, int]:
    """Merged ``{ticker: cik}``, read from cache unless ``refresh``."""
    path = Path(cache_path) if cache_path is not None else TICKER_MAP_PATH
    if not refresh and path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached:
            return {str(k): int(v) for k, v in cached.items()}

    mapping: dict[str, int] = {}
    for line in client.get(TICKER_TXT_URL).text.splitlines():
        if "\t" not in line:
            continue
        ticker, cik = line.split("\t", 1)
        ticker = normalise(ticker)
        if ticker and cik.strip().isdigit():
            mapping[ticker] = int(cik.strip())

    # company_tickers.json second, and only for tickers ticker.txt omitted: where
    # they disagree the wider file is the one that also carries delisted issuers.
    for row in client.get_json(COMPANY_TICKERS_URL).values():
        ticker, cik = row.get("ticker"), row.get("cik_str")
        if ticker and cik is not None:
            mapping.setdefault(normalise(str(ticker)), int(cik))

    if not mapping:
        raise ValueError("SEC returned no ticker/CIK pairs from either file")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
    return mapping
