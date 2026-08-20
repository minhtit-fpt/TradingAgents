"""The daily run: screen, alert on anything at the trigger, always heartbeat.

    python -m tradingagents.value.jobs.daily
    python -m tradingagents.value.jobs.daily --as-of 2026-11-14 --dry-run

Ordering is the cost model, same as the screen's. The numeric pass is free and
runs over everything; the filing read costs money and runs only on names that
have *newly* triggered, so a name sitting below its entry price for a month is
read once rather than thirty times.

What this job does not do is decide. The alert fires on arithmetic — price
against an intrinsic value the screen already computed — and phases 4b and 6
stay closed: the filing read is attached to a message already committed, never
consulted about whether to send it.

The heartbeat is not optional. This screen's normal state is silence, so a dead
cron and a quiet market produce the same empty inbox; one line a day is what
tells them apart.
"""

import argparse
import contextlib
import sqlite3
import sys
from datetime import date, datetime, timezone

from ..alerts import message, telegram
from ..config import ANALYST_MODEL, HISTORY_YEARS, RUN_BUDGET_USD, VIOLATION_TOLERANCE
from ..edgar.client import SecClient, SecRequestError
from ..llm.budget import Budget, BudgetError
from ..report import read_filing
from ..screen import market
from ..screen.runner import Outcome, run
from ..store import db
from .bootstrap import ingest

# A 10-K lands once a year, so facts older than this are stale rather than
# merely current. Refreshing on that clock costs a handful of companyfacts
# fetches a day and avoids building a daily-index crawler for a signal that
# changes four times a year — the price is what moves the margin of safety
# daily, and the price needs no EDGAR call at all.
STALE_AFTER_DAYS = 400


def stale_tickers(conn: sqlite3.Connection, as_of: str, limit: int) -> list[str]:
    """Names whose newest fact predates the staleness window, oldest first."""
    rows = conn.execute(
        "SELECT ticker, MAX(filed) AS newest FROM facts GROUP BY ticker "
        "HAVING julianday(?) - julianday(newest) > ? ORDER BY newest LIMIT ?",
        (as_of, STALE_AFTER_DAYS, limit),
    ).fetchall()
    return [row["ticker"] for row in rows]


def refresh(conn: sqlite3.Connection, as_of: str, limit: int, *, client=None) -> list[str]:
    """Re-ingest the stalest names. Failures are reported, never swallowed."""
    notes: list[str] = []
    tickers = stale_tickers(conn, as_of, limit)
    if not tickers:
        return notes
    client = client or SecClient()
    refreshed = 0
    for ticker in tickers:
        cik = db.cik_for(conn, ticker)
        if cik is None:
            notes.append(f"{ticker}: no CIK on file, not refreshed")
            continue
        try:
            ingest(conn, client, ticker, cik)
        except (SecRequestError, ValueError) as exc:
            notes.append(f"{ticker}: refresh failed — {exc}")
        else:
            refreshed += 1
    if refreshed:
        notes.append(f"refreshed {refreshed} stale name(s)")
    return notes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def alert_once(
    conn: sqlite3.Connection,
    outcome: Outcome,
    as_of: str,
    *,
    with_filing: bool = True,
    client=None,
    budget: Budget | None = None,
    llm=None,
    model: str = ANALYST_MODEL,
    sender=telegram.send,
    now=_now,
    record: bool = True,
) -> str:
    """Queue, compose, send, confirm — in that order. Returns a status note.

    The queue row is written before anything is composed or sent, so a crash
    anywhere after it leaves an unconfirmed row the next run retries. Writing it
    afterwards would lose the alert on every failure, which is the one outcome a
    dedupe table must not produce.

    ``record=False`` is the dry run, and it skips the table entirely in both
    directions. Claiming the dedupe row without sending would let a rehearsal
    silence the real run that follows it — the same lost alert by another route.
    """
    valuation = outcome.valuation
    if record and not db.queue_alert(
        conn, outcome.ticker, as_of, valuation.margin_of_safety, valuation.price, now()
    ):
        return ""

    assessment = None
    note = ""
    if with_filing:
        try:
            assessment, _label, _url, _missing, note = read_filing(
                conn, outcome, as_of, client=client, budget=budget, llm=llm, model=model
            )
        except BudgetError as exc:
            # The budget stops the reading, never the alert. A briefing that
            # could not be paid for is a missing paragraph, not a missing alert.
            note = f"budget exhausted: {exc}"

    text = message.trigger_alert(outcome, as_of, assessment, note)
    if sender(text):
        if record:
            db.confirm_alert(conn, outcome.ticker, as_of, now())
        return f"alerted {outcome.ticker}"
    return f"composed {outcome.ticker} (not sent)"


def retry_unsent(
    conn: sqlite3.Connection, today: str, *, sender=telegram.send, now=_now
) -> list[str]:
    """Re-send alerts a previous run queued but never confirmed."""
    notes = []
    for row in db.unsent_alerts(conn):
        if row["trigger_date"] == today:
            continue  # this run is still working on it
        text = (
            f"{row['ticker']} at {row['mos_pct']:+.1%} MoS — {row['trigger_date']} "
            "(delayed: a previous run queued this and could not send it)\n"
            f"price {row['price']:,.2f}\n\n{message.SIZING_NOTE}\n{message.DISCLAIMER}\n"
            f"Full dossier: python -m tradingagents.value.report --ticker {row['ticker']}"
        )
        if sender(text):
            db.confirm_alert(conn, row["ticker"], row["trigger_date"], now())
            notes.append(f"re-sent {row['ticker']} from {row['trigger_date']}")
    return notes


def daily(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    years: int = HISTORY_YEARS,
    tolerance: int = VIOLATION_TOLERANCE,
    prices=market,
    with_filing: bool = True,
    refresh_limit: int = 5,
    client=None,
    budget: Budget | None = None,
    llm=None,
    model: str = ANALYST_MODEL,
    sender=telegram.send,
    now=_now,
    record: bool = True,
) -> list[str]:
    """One full pass. Returns every note produced, heartbeat last."""
    notes: list[str] = []
    if refresh_limit:
        notes.extend(refresh(conn, as_of, refresh_limit, client=client))
    if record:
        notes.extend(retry_unsent(conn, as_of, sender=sender, now=now))

    outcomes = run(conn, as_of, years=years, tolerance=tolerance, prices=prices, record=record)
    for outcome in outcomes:
        if not outcome.triggered:
            continue
        note = alert_once(
            conn, outcome, as_of,
            with_filing=with_filing, client=client, budget=budget,
            llm=llm, model=model, sender=sender, now=now, record=record,
        )
        if note:
            notes.append(note)

    heartbeat = message.heartbeat(outcomes, as_of, notes)
    sender(heartbeat)
    notes.append(heartbeat)
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--years", type=int, default=HISTORY_YEARS)
    parser.add_argument("--tolerance", type=int, default=VIOLATION_TOLERANCE)
    parser.add_argument("--no-filing", action="store_true",
                        help="alert without the 10-K briefing (spends nothing)")
    parser.add_argument("--budget", type=float, default=RUN_BUDGET_USD,
                        help="USD cap for this run; fails closed")
    parser.add_argument("--refresh-limit", type=int, default=5,
                        help="how many stale names to re-ingest; 0 to skip")
    parser.add_argument("--model", default=ANALYST_MODEL)
    parser.add_argument("--dry-run", action="store_true",
                        help="compose everything, send nothing, record nothing")
    parser.add_argument("--db", default=None, help="override the store path")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    sender = (
        (lambda text: telegram.send(text, dry_run=True)) if args.dry_run else telegram.send
    )
    try:
        notes = daily(
            conn,
            args.as_of,
            years=args.years,
            tolerance=args.tolerance,
            with_filing=not args.no_filing,
            refresh_limit=args.refresh_limit,
            budget=Budget(run_cap_usd=args.budget) if not args.no_filing else None,
            model=args.model,
            sender=sender,
            record=not args.dry_run,
        )
    except Exception as exc:  # the cron's last word before it dies silently
        print(f"daily run failed: {exc}", file=sys.stderr)
        # A failing run must still say so out loud — but if the channel is what
        # broke, stderr above is already the record. The original failure is the
        # one worth reporting, not a second one about not being able to report it.
        with contextlib.suppress(Exception):
            sender(f"{args.as_of} FAILED — {type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()

    for note in notes:
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
