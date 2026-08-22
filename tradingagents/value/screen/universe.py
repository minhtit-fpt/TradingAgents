"""Who is even eligible to be screened, and why the rest were dropped.

The universe is built from evidence already in the store rather than from a
list of exclusions to maintain, which is what makes the three exclusions the
plan names fall out for free:

- **ETFs and trusts** file N-CSR, never a 10-K, so ``annual_facts`` yields
  nothing for them and they land in "no 10-K facts".
- **Foreign private issuers** file 20-F. Same outcome, same reason.
- **SPACs** do file 10-Ks — but a blank-check shell has no revenue and no
  decade of history, so it fails the history and revenue gates.

Every exclusion carries its reason. A universe that silently shrinks is a
universe nobody notices is broken.
"""

import sqlite3
from dataclasses import dataclass

from ..config import HISTORY_YEARS, MIN_CONCEPT_COVERAGE, VIOLATION_TOLERANCE
from ..store import db


@dataclass(frozen=True)
class Exclusion:
    """One company kept out of the screen, and what kept it out."""

    ticker: str
    reason: str


@dataclass(frozen=True)
class Universe:
    """The screenable set as of a date, plus the audit trail for the rest."""

    tickers: tuple[str, ...]
    excluded: tuple[Exclusion, ...]

    @property
    def summary(self) -> str:
        return f"{len(self.tickers)} screenable, {len(self.excluded)} excluded"


def screenable(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    tickers: list[str] | None = None,
    years: int | None = None,
    min_coverage: float = MIN_CONCEPT_COVERAGE,
) -> Universe:
    """Split the store's companies into screenable and excluded, as of a date."""
    window = years if years is not None else HISTORY_YEARS
    candidates = [t.upper() for t in tickers] if tickers else db.tickers(conn)

    eligible: list[str] = []
    excluded: list[Exclusion] = []
    for ticker in candidates:
        reason = _rejection(conn, ticker, as_of, window, min_coverage)
        if reason:
            excluded.append(Exclusion(ticker, reason))
        else:
            eligible.append(ticker)

    return Universe(tickers=tuple(eligible), excluded=tuple(excluded))


def _rejection(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    years: int,
    min_coverage: float,
) -> str | None:
    """Why ``ticker`` cannot be screened on ``as_of``, or None if it can."""
    series = db.series_as_of(conn, ticker, as_of, years)
    if not series:
        return "no 10-K facts as of this date (ETF, 20-F filer, or not yet public)"

    if len(series) < years:
        return f"only {len(series)} of {years} fiscal years of history"

    # Below the coverage floor the ratios would be computed from holes in the
    # data. Excluding beats screening on partial inputs (plan section 9.6).
    report = db.coverage(conn, ticker, as_of, years)
    ratio = db.coverage_ratio(report, years)
    if ratio < min_coverage:
        return f"tag coverage {ratio:.0%} below the {min_coverage:.0%} floor"

    latest = series[max(series)]
    if not latest.get("Revenue"):
        return "no revenue in the latest fiscal year (blank-check shell)"

    # Four of the thirteen criteria divide by gross profit, and a filer with no
    # cost-of-revenue line anywhere — McDonald's, Exxon, Union Pacific, and every
    # bank — cannot produce one from any tag or identity. Left to the screen those
    # four count as violations and the company is reported as failing on margin,
    # which reads as a verdict on the business rather than on the filing. Excluded
    # with its reason instead: unevaluable is not the same as bad.
    evaluable = sum(1 for year in series if "GrossProfit" in series[year])
    if evaluable < years - VIOLATION_TOLERANCE:
        return (
            f"gross profit resolves for only {evaluable} of {years} years — "
            "no cost-of-revenue line to compute it from (financial, or a filer "
            "that reports no cost of sales)"
        )

    return None
