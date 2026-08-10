"""SQLite fact store, queried as of a date.

Every read is point-in-time by construction: ``facts_as_of`` filters on the
EDGAR ``filed`` date and takes, per (concept, fiscal year), the most recent
filing at or before the as-of date. That is what an investor could actually have
known that morning. Filtering on ``period_end`` instead — the obvious-looking
alternative — is look-ahead bias: FY2024 statements did not exist on
2024-12-31, they were filed weeks later.

The DDL lives here as a string rather than a ``schema.sql`` data file, so
installing the package needs no change to ``pyproject.toml`` — the isolation
contract forbids editing it.
"""

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..config import DB_PATH, HISTORY_YEARS
from ..edgar.concepts import CONCEPT_NAMES, Fact

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    cik         INTEGER NOT NULL,
    ticker      TEXT    NOT NULL,
    concept     TEXT    NOT NULL,
    fiscal_year INTEGER NOT NULL,
    period_end  TEXT    NOT NULL,
    filed       TEXT    NOT NULL,
    value       REAL    NOT NULL,
    unit        TEXT    NOT NULL,
    source_tag  TEXT    NOT NULL,
    accn        TEXT    NOT NULL,
    PRIMARY KEY (cik, concept, period_end, accn)
);

CREATE INDEX IF NOT EXISTS facts_point_in_time
    ON facts (ticker, concept, fiscal_year, filed);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the value store."""
    target = Path(path) if path else DB_PATH
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_facts(conn: sqlite3.Connection, ticker: str, cik: int, facts: Iterable[Fact]) -> int:
    """Write facts idempotently. Re-ingesting the same filing changes nothing."""
    rows = [
        (cik, ticker.upper(), f.concept, f.fiscal_year, f.period_end,
         f.filed, f.value, f.unit, f.source_tag, f.accn)
        for f in facts
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO facts "
            "(cik, ticker, concept, fiscal_year, period_end, filed, value, unit, "
            " source_tag, accn) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def facts_as_of(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    years: int | None = None,
) -> list[sqlite3.Row]:
    """Facts knowable on ``as_of`` (``YYYY-MM-DD``), latest filing per fiscal year.

    A restatement filed after ``as_of`` is invisible; the figure as originally
    reported is returned instead.
    """
    window = years if years is not None else HISTORY_YEARS
    cursor = conn.execute(
        """
        SELECT f.concept, f.fiscal_year, f.period_end, f.filed, f.value, f.unit,
               f.source_tag, f.accn
        FROM facts f
        JOIN (
            SELECT concept, fiscal_year, MAX(filed) AS filed
            FROM facts
            WHERE ticker = ? AND filed <= ?
            GROUP BY concept, fiscal_year
        ) latest
          ON latest.concept = f.concept
         AND latest.fiscal_year = f.fiscal_year
         AND latest.filed = f.filed
        WHERE f.ticker = ? AND f.filed <= ?
        ORDER BY f.concept, f.fiscal_year
        """,
        (ticker.upper(), as_of, ticker.upper(), as_of),
    )
    rows = cursor.fetchall()
    if not rows:
        return []

    # Keep the most recent ``window`` fiscal years the data actually contains.
    keep = sorted({row["fiscal_year"] for row in rows})[-window:]
    return [row for row in rows if row["fiscal_year"] in keep]


def coverage(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    years: int | None = None,
) -> dict[str, dict]:
    """Which concepts resolved, via which tags, for how many of the wanted years.

    A silent tag miss is the likeliest way this module produces a confidently
    wrong ratio, so the report names every concept — including the ones that
    resolved zero years.
    """
    window = years if years is not None else HISTORY_YEARS
    rows = facts_as_of(conn, ticker, as_of, window)
    fiscal_years = sorted({row["fiscal_year"] for row in rows})

    report: dict[str, dict] = {}
    for concept in CONCEPT_NAMES:
        matched = [row for row in rows if row["concept"] == concept]
        resolved_years = {row["fiscal_year"] for row in matched}
        report[concept] = {
            "years": len(matched),
            "tags": sorted({row["source_tag"] for row in matched}),
            "missing_years": [year for year in fiscal_years if year not in resolved_years],
        }
    return report


def coverage_ratio(report: dict[str, dict], expected_years: int) -> float:
    """Share of (concept, year) cells the ingest actually filled, in [0, 1]."""
    if expected_years <= 0:
        return 0.0
    filled = sum(min(entry["years"], expected_years) for entry in report.values())
    return filled / (len(CONCEPT_NAMES) * expected_years)


def tickers(conn: sqlite3.Connection) -> list[str]:
    """Every ticker present in the store."""
    return [row[0] for row in conn.execute("SELECT DISTINCT ticker FROM facts ORDER BY ticker")]
