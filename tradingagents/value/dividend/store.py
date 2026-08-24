"""The one table this feature owns, created on connect rather than in the DDL.

It shares the value store's SQLite file — the screen reads EDGAR facts from it
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
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """The value store, plus this feature's own table."""
    conn = db.connect(path)
    conn.executescript(SCHEMA)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Add the table to a connection someone else opened. Idempotent."""
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


def as_of(conn: sqlite3.Connection, ticker: str, as_of_date: str) -> list[sqlite3.Row]:
    """Every dividend whose ex-date had already passed on ``as_of_date``, oldest first."""
    return conn.execute(
        "SELECT ex_date, dps FROM dividends WHERE ticker = ? AND ex_date <= ? ORDER BY ex_date",
        (ticker.upper(), as_of_date),
    ).fetchall()
