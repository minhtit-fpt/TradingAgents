"""The decision log — what you decided, when, and why.

    python -m tradingagents.value.decisions record --ticker PG --action pass \\
        --why "entry price is 130.49; not paying 143 for it"
    python -m tradingagents.value.decisions list --ticker PG

Phase 7 assembles the evidence and phase 8 pushes it, but neither leaves any
trace of what the operator did with it. Without this file, three years of
decisions are three years of nothing: a portfolio with no recorded reasoning and,
worse, no record of the names that were looked at and declined. Those declined
names are the counterfactual — a journal holding only the purchases is
survivorship bias applied to oneself.

So ``pass`` is a first-class action here, not an afterthought, and ``--why`` is
required rather than optional. The numbers are snapshotted at record time because
the store moves on, and a review in 2029 should not have to reconstruct what the
screen said today.

Append-only: no edit, no delete. Changing your mind writes another row.
"""

import argparse
import sqlite3
import sys
from datetime import date, datetime, timezone

from . import report
from .store import db

# Small on purpose. A vocabulary that grows to twenty verbs stops being
# comparable across years, which is the only thing this log is for.
ACTIONS = ("buy", "add", "trim", "sell", "pass", "watch")


def record(
    conn: sqlite3.Connection,
    ticker: str,
    action: str,
    reason: str,
    *,
    decided_on: str | None = None,
    snapshot: bool = True,
    filing_read: bool = False,
    prices=None,
    now: str | None = None,
) -> int:
    """Append one decision, with the numbers as they stand. Returns the row id.

    A snapshot that cannot be taken — no network, a name the store has never
    seen — is recorded as absent rather than substituted. A decision logged with
    an invented price would be worse than one logged with none.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}")
    when = decided_on or date.today().isoformat()

    price = intrinsic = mos = None
    passed: bool | None = None
    if snapshot:
        kwargs = {"prices": prices} if prices is not None else {}
        dossier = report.build(conn, ticker, when, with_filing=False, **kwargs)
        outcome = dossier.outcome
        if outcome.screen is not None:
            passed = outcome.screen.passed
        if outcome.valuation is not None:
            price = outcome.valuation.price
            intrinsic = outcome.valuation.intrinsic_value
            mos = outcome.valuation.margin_of_safety

    return db.record_decision(
        conn,
        ticker,
        when,
        action,
        reason,
        now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        price=price,
        intrinsic_value=intrinsic,
        mos_pct=mos,
        screen_passed=passed,
        filing_read=filing_read,
    )


def render(rows: list[sqlite3.Row]) -> list[str]:
    """A chronological dump, and deliberately nothing cleverer than that.

    What the right review looks like is unknowable until there are rows in here;
    building a report against an empty table would build the wrong one.
    """
    if not rows:
        return ["no decisions recorded yet"]

    lines = [f"{len(rows)} decision(s), oldest first"]
    for row in rows:
        numbers = "no snapshot"
        if row["price"] is not None and row["intrinsic_value"] is not None:
            mos = f"{row['mos_pct']:+.1%}" if row["mos_pct"] is not None else "—"
            numbers = (
                f"price {row['price']:,.2f} vs value {row['intrinsic_value']:,.2f}, MoS {mos}"
            )
        screen = {None: "screen ?", 1: "screen passed", 0: "screen failed"}[row["screen_passed"]]
        lines.append(
            f"{row['decided_on']}  {row['ticker']:<6} {row['action']:<5}  {numbers}; {screen}"
            f"{'; filing read' if row['filing_read'] else ''}"
        )
        lines.append(f"        why: {row['reason']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="override the store path")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("record", help="append one decision")
    add.add_argument("--ticker", required=True)
    add.add_argument("--action", required=True, choices=ACTIONS)
    add.add_argument("--why", required=True, help="required: a decision with no reason is noise")
    add.add_argument("--on", default=None, help="decision date; default today")
    add.add_argument("--filing-read", action="store_true",
                     help="record that the 10-K read informed this decision")
    add.add_argument("--no-snapshot", action="store_true",
                     help="skip the price/valuation lookup (offline)")

    show = sub.add_parser("list", help="read the log back")
    show.add_argument("--ticker", default=None)
    show.add_argument("--since", default=None, help="YYYY-MM-DD")

    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        if args.command == "record":
            try:
                row_id = record(
                    conn,
                    args.ticker,
                    args.action,
                    args.why,
                    decided_on=args.on,
                    snapshot=not args.no_snapshot,
                    filing_read=args.filing_read,
                )
            except (ValueError, LookupError) as exc:
                print(f"not recorded: {exc}", file=sys.stderr)
                return 2
            print(f"recorded #{row_id}: {args.ticker.upper()} {args.action}")
            return 0

        for line in render(db.decisions(conn, ticker=args.ticker, since=args.since)):
            print(line)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
