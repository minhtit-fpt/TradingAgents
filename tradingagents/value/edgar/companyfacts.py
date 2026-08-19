"""Fetch companyfacts and the ticker → CIK map from EDGAR.

Phase 1 deliberately fetches **per company** rather than pulling the ~2 GB
``companyfacts.zip``: the acceptance case is a 100-ticker sample, which is 100
requests — about fifteen seconds under the rate limit. The bulk zip earns its
download when phase 2 sweeps the full universe, and belongs there.
"""

from dataclasses import dataclass

from .client import SecClient

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


@dataclass(frozen=True)
class Company:
    """A listed issuer as EDGAR identifies it."""

    ticker: str
    cik: int
    name: str


def companies(client: SecClient) -> list[Company]:
    """Every ticker EDGAR maps to a CIK, in EDGAR's own order.

    That order is roughly by size at the time SEC generated the file, which makes
    a leading slice a usable large-cap sample without needing a second source.
    """
    payload = client.get_json(COMPANY_TICKERS_URL)
    return [
        Company(
            ticker=str(row["ticker"]).upper(),
            cik=int(row["cik_str"]),
            name=str(row.get("title", "")),
        )
        for row in payload.values()
        if row.get("ticker") and row.get("cik_str") is not None
    ]


def fetch_companyfacts(client: SecClient, cik: int) -> dict:
    """Raw companyfacts payload for one CIK."""
    return client.get_json(COMPANYFACTS_URL.format(cik=cik))
