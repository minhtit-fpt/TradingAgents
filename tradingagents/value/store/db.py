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

CREATE TABLE IF NOT EXISTS screen_results (
    ticker          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    passed          INTEGER NOT NULL,
    failed_criteria TEXT NOT NULL,
    intrinsic_value REAL,
    price           REAL,
    mos_pct         REAL,
    PRIMARY KEY (ticker, as_of)
);

-- One row per (name, trigger date). queued_at is written before Telegram is
-- called and sent_at only after it confirms, so an outage leaves a row the next
-- run retries rather than an alert nobody ever sees (plan section 9.4).
CREATE TABLE IF NOT EXISTS alerts (
    ticker       TEXT NOT NULL,
    trigger_date TEXT NOT NULL,
    mos_pct      REAL NOT NULL,
    price        REAL NOT NULL,
    queued_at    TEXT NOT NULL,
    sent_at      TEXT,
    PRIMARY KEY (ticker, trigger_date)
);

-- Append-only. Changing your mind is a new row, never an edit: a journal that
-- can be rewritten after the fact is not evidence about your past self. The
-- price/value columns snapshot the numbers as they stood, so a review years from
-- now does not have to re-derive them from a store that has moved on.
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    decided_on      TEXT NOT NULL,
    action          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    price           REAL,
    intrinsic_value REAL,
    mos_pct         REAL,
    screen_passed   INTEGER,
    filing_read     INTEGER NOT NULL DEFAULT 0,
    recorded_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS decisions_by_ticker ON decisions (ticker, decided_on);
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


def series_as_of(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    years: int | None = None,
) -> dict[int, dict[str, float]]:
    """``facts_as_of`` reshaped as ``{fiscal_year: {concept: value}}``.

    The screen reasons year by year across concepts, so it wants rows pivoted.
    Point-in-time filtering already happened upstream; this only reshapes.
    """
    series: dict[int, dict[str, float]] = {}
    for row in facts_as_of(conn, ticker, as_of, years):
        series.setdefault(row["fiscal_year"], {})[row["concept"]] = row["value"]
    return series


def filed_as_of(
    conn: sqlite3.Connection,
    ticker: str,
    concept: str,
    as_of: str,
    years: int | None = None,
) -> dict[int, str]:
    """Per fiscal year, the filing date of the figure ``series_as_of`` returned.

    A share count is only meaningful alongside the date it was filed on: a
    split between two filings puts two years of the same series on two
    different bases, and this is what lets the screen put them back together.
    """
    return {
        row["fiscal_year"]: row["filed"]
        for row in facts_as_of(conn, ticker, as_of, years)
        if row["concept"] == concept
    }


def record_screen(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    passed: bool,
    failed_criteria: Iterable[str],
    intrinsic_value: float | None = None,
    price: float | None = None,
    mos_pct: float | None = None,
) -> None:
    """Write one screen verdict, replacing any earlier run for the same day.

    Keyed on (ticker, as_of) so a re-run — retry, reboot, a second cron firing —
    overwrites rather than accumulating. Phase 7's alert dedupe depends on this
    row already existing before anything is sent.
    """
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO screen_results "
            "(ticker, as_of, passed, failed_criteria, intrinsic_value, price, mos_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(),
                as_of,
                int(passed),
                ",".join(failed_criteria),
                intrinsic_value,
                price,
                mos_pct,
            ),
        )


def screen_rows(conn: sqlite3.Connection, as_of: str) -> list[sqlite3.Row]:
    """Every verdict recorded for ``as_of``, best margin of safety first."""
    return conn.execute(
        "SELECT * FROM screen_results WHERE as_of = ? "
        "ORDER BY mos_pct IS NULL, mos_pct DESC, ticker",
        (as_of,),
    ).fetchall()


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


def cik_for(conn: sqlite3.Connection, ticker: str) -> int | None:
    """The CIK the store holds ``ticker`` under, or ``None`` if it holds none.

    EDGAR is addressed by CIK, not by ticker — a filing lookup needs this, and
    the store already carries it on every fact. ``None`` rather than a raise:
    a ticker with no facts is an ordinary outcome for a caller iterating over a
    historical index, and the caller reports it by name.
    """
    row = conn.execute(
        "SELECT cik FROM facts WHERE ticker = ? LIMIT 1", (ticker.upper(),)
    ).fetchone()
    return int(row["cik"]) if row else None


# --- Alerts (phase 8) ----------------------------------------------------------
#
# Dedupe is one predicate over this table, so it lives here with the rest of the
# module's state rather than in an `alerts/dedupe.py` whose whole body would be
# `not alert_queued(...)`.


def alert_queued(conn: sqlite3.Connection, ticker: str, trigger_date: str) -> bool:
    """Has this (name, trigger date) already been queued by any earlier run?"""
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE ticker = ? AND trigger_date = ?",
        (ticker.upper(), trigger_date),
    ).fetchone()
    return row is not None


def queue_alert(
    conn: sqlite3.Connection,
    ticker: str,
    trigger_date: str,
    mos_pct: float,
    price: float,
    queued_at: str,
) -> bool:
    """Claim the send. Returns False if another run already claimed it.

    Written *before* the Telegram call: a crash between here and the send leaves
    a row with ``sent_at`` NULL, which ``unsent_alerts`` hands back to the next
    run. The opposite order loses the alert entirely on any failure.
    """
    with conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO alerts "
            "(ticker, trigger_date, mos_pct, price, queued_at, sent_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (ticker.upper(), trigger_date, mos_pct, price, queued_at),
        )
    return cursor.rowcount == 1


def confirm_alert(conn: sqlite3.Connection, ticker: str, trigger_date: str, sent_at: str) -> None:
    """Record that Telegram accepted the message."""
    with conn:
        conn.execute(
            "UPDATE alerts SET sent_at = ? WHERE ticker = ? AND trigger_date = ?",
            (sent_at, ticker.upper(), trigger_date),
        )


def unsent_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Alerts queued but never confirmed — the retry queue, oldest first."""
    return conn.execute(
        "SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY queued_at, ticker"
    ).fetchall()


# --- Decision log (phase 8) ----------------------------------------------------


def record_decision(
    conn: sqlite3.Connection,
    ticker: str,
    decided_on: str,
    action: str,
    reason: str,
    recorded_at: str,
    *,
    price: float | None = None,
    intrinsic_value: float | None = None,
    mos_pct: float | None = None,
    screen_passed: bool | None = None,
    filing_read: bool = False,
) -> int:
    """Append one decision. Returns its id.

    INSERT only — there is deliberately no update or delete. A decision that
    turns out wrong is followed by another decision, and both rows are the point.
    """
    if not reason.strip():
        raise ValueError("a decision with no stated reason is not worth recording")
    with conn:
        cursor = conn.execute(
            "INSERT INTO decisions "
            "(ticker, decided_on, action, reason, price, intrinsic_value, mos_pct, "
            " screen_passed, filing_read, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(),
                decided_on,
                action,
                reason.strip(),
                price,
                intrinsic_value,
                mos_pct,
                None if screen_passed is None else int(screen_passed),
                int(filing_read),
                recorded_at,
            ),
        )
    return int(cursor.lastrowid)


def decisions(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    since: str | None = None,
) -> list[sqlite3.Row]:
    """The log, oldest first. Chronological order is the whole point of it."""
    clauses, params = [], []
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker.upper())
    if since:
        clauses.append("decided_on >= ?")
        params.append(since)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM decisions{where} ORDER BY decided_on, id", params
    ).fetchall()
