"""The tables this feature owns, created on connect rather than in the shared DDL.

They share the value store's SQLite file — the screen reads EDGAR facts from it
on every run, and a second file would mean a second connection to keep in step
for no gain. It does **not** share the schema string: ``value/store/db.py`` is
untouched, and this DDL runs from here, idempotently, whenever the dividend
screen opens a connection.

Point-in-time comes for free. A dividend is public knowledge on its ex-date, so
a screen as of a date filters on ``ex_date`` and nothing leaks.
"""

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..store import db

# The amounts are on yfinance's split-adjusted basis (today's share count), not
# the basis in force on the ex-date. That is the right basis *here* and the
# wrong one in ``screen/market.py``: this table is only ever read as a ratio
# between its own years, and a uniform rescaling cancels out of a ratio.
# Never divide a price from ``market.close()`` by a figure from this table.
SCHEMA = """
CREATE TABLE IF NOT EXISTS dividends (
    ticker     TEXT NOT NULL,
    ex_date    TEXT NOT NULL,
    dps        REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (ticker, ex_date)
);

CREATE TABLE IF NOT EXISTS dividend_lots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    traded_on   TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    shares      REAL    NOT NULL,
    price       REAL    NOT NULL,
    fees        REAL    NOT NULL,
    note        TEXT    NOT NULL,
    recorded_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS dividend_lots_by_ticker
    ON dividend_lots (ticker, traded_on);

-- Money crossing the account boundary, plus dividends landing inside it. A
-- dividend is not an external flow: it is the book paying itself, and counting
-- it as a deposit would make every payer look like fresh capital.
CREATE TABLE IF NOT EXISTS dividend_cash (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    happened_on TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    ticker      TEXT,
    amount      REAL    NOT NULL,
    note        TEXT    NOT NULL,
    recorded_at TEXT    NOT NULL
);

-- What the weekly job has already said, so it says it once. Keyed on the
-- *signature* rather than the date: a name whose payout ratio broke in March is
-- still broken in April, and repeating it every week trains the operator to
-- ignore the message. A second criterion breaking is a new signature and a new
-- alert, which is the change worth waking up for.
--
-- queued_at is written before the send and sent_at only after it confirms, so an
-- outage leaves a row the next run retries rather than an alert nobody saw.
CREATE TABLE IF NOT EXISTS dividend_alerts (
    ticker    TEXT NOT NULL,
    signature TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    sent_at   TEXT,
    PRIMARY KEY (ticker, signature)
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """The value store, plus this feature's own tables."""
    conn = db.connect(path)
    conn.executescript(SCHEMA)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Add the tables to a connection someone else opened. Idempotent."""
    conn.executescript(SCHEMA)
    return conn


def upsert(
    conn: sqlite3.Connection,
    ticker: str,
    rows: Iterable[tuple[str, float]],
    fetched_at: str,
) -> int:
    """Cache ``(ex_date, dps)`` pairs. Re-fetching the same history changes nothing."""
    payload = [(ticker.upper(), ex_date, float(dps), fetched_at) for ex_date, dps in rows]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO dividends (ticker, ex_date, dps, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            payload,
        )
    return len(payload)


def announced(conn: sqlite3.Connection, ticker: str, signature: str) -> bool:
    """True when this exact thing has already been **delivered** for this name.

    Delivered, not merely queued. A row whose ``sent_at`` is still null is an
    alert that was composed and never arrived, and treating it as said would
    silence it forever — the one outcome a dedupe table must not produce.
    """
    row = conn.execute(
        "SELECT sent_at FROM dividend_alerts WHERE ticker = ? AND signature = ?",
        (ticker.upper(), signature),
    ).fetchone()
    return row is not None and row["sent_at"] is not None


def claim(conn: sqlite3.Connection, ticker: str, signature: str, queued_at: str) -> bool:
    """Reserve the right to say it. False only once it has actually been sent."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO dividend_alerts (ticker, signature, queued_at) "
            "VALUES (?, ?, ?)",
            (ticker.upper(), signature, queued_at),
        )
    return not announced(conn, ticker, signature)


def confirm(conn: sqlite3.Connection, ticker: str, signature: str, sent_at: str) -> None:
    """Mark a claimed alert as actually delivered."""
    with conn:
        conn.execute(
            "UPDATE dividend_alerts SET sent_at = ? WHERE ticker = ? AND signature = ?",
            (sent_at, ticker.upper(), signature),
        )


def cached(conn: sqlite3.Connection) -> list[str]:
    """Every name with a cached dividend history."""
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT ticker FROM dividends ORDER BY ticker"
    )]


def stalest(conn: sqlite3.Connection, limit: int) -> list[str]:
    """Cached names whose dividend history was fetched longest ago, oldest first."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT ticker, MIN(fetched_at) AS oldest FROM dividends "
            "GROUP BY ticker ORDER BY oldest LIMIT ?",
            (limit,),
        )
    ]


def as_of(conn: sqlite3.Connection, ticker: str, as_of_date: str) -> list[sqlite3.Row]:
    """Every dividend whose ex-date had already passed on ``as_of_date``, oldest first."""
    return conn.execute(
        "SELECT ex_date, dps FROM dividends WHERE ticker = ? AND ex_date <= ? ORDER BY ex_date",
        (ticker.upper(), as_of_date),
    ).fetchall()
