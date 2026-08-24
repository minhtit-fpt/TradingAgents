"""What you actually bought, sold, paid in and were paid. Append-only.

    python -m tradingagents.value.dividend.ledger buy  --ticker PG --shares 12 --price 148.20
    python -m tradingagents.value.dividend.ledger sell --ticker PG --shares 4 --price 171.05
    python -m tradingagents.value.dividend.ledger deposit  --amount 5000
    python -m tradingagents.value.dividend.ledger dividend --ticker PG --amount 11.87
    python -m tradingagents.value.dividend.ledger list

Two tables and no third. Positions, cost basis and profit are **derived** by
folding these rows, never stored: a stored balance is a number that can disagree
with the events that produced it, and the disagreement is always discovered
later than it happened. ``decisions`` already works this way; so does this.

Append-only for the same reason it is there — no edit, no delete. A mistyped
trade is corrected by recording its reverse, which leaves both rows visible. A
ledger that can be quietly rewritten is not evidence about your past self.

Cost basis is **weighted average**, chosen deliberately (plan `dividend-screen.md`).
One basis per name, updated on every purchase. It is the simplest thing that is
correct for a book meant to be held for years, and the operator confirmed it.
Realised profit on a partial sale is therefore proceeds minus the average cost
of the shares sold, and it cannot be steered by choosing which lot to sell.

This module records. It does not price anything and it does not decide anything
— ``book.py`` values what is here, and the reasoning behind a trade belongs in
``tradingagents.value.decisions``, which is a separate command on purpose.
"""

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import store

# Shares are floats because brokers sell fractions of them. Comparing them for
# "sold everything" therefore needs a tolerance rather than ``== 0``.
DUST = 1e-9

TRADES = ("buy", "sell")
CASH_KINDS = ("deposit", "withdraw", "dividend")

class LedgerError(ValueError):
    """A row that would make the book incoherent. Never written."""


@dataclass(frozen=True)
class Trade:
    """One purchase or sale, as recorded."""

    ticker: str
    traded_on: str
    action: str
    shares: float
    price: float
    fees: float = 0.0

    @property
    def cash(self) -> float:
        """Signed effect on the account's cash: negative to buy, positive to sell."""
        gross = self.shares * self.price
        return -(gross + self.fees) if self.action == "buy" else gross - self.fees


@dataclass(frozen=True)
class Position:
    """One name after folding its trades. Closed positions are kept, not dropped.

    A book holding only what is still open is survivorship applied to yourself —
    the same argument that makes ``pass`` a first-class action in the decision
    log. The name you sold at a loss is the one worth seeing next year.
    """

    ticker: str
    shares: float
    cost: float
    realised: float
    fees: float
    first_bought: str
    last_traded: str

    @property
    def is_open(self) -> bool:
        return self.shares > DUST

    @property
    def average_cost(self) -> float | None:
        """Cost basis per share. ``None`` once the position is closed."""
        return self.cost / self.shares if self.is_open else None


def fold(trades: Sequence[Trade]) -> dict[str, Position]:
    """Trades to positions, weighted average cost, oldest trade first.

    Raises rather than tolerating a sale of shares that were never held. That is
    not a hypothetical: it is what a fat-fingered share count looks like, and
    a book that quietly carries a negative position reports a profit that never
    existed.
    """
    state: dict[str, dict] = {}

    for trade in sorted(trades, key=lambda t: (t.traded_on, t.action)):
        if trade.action not in TRADES:
            raise LedgerError(f"unknown action {trade.action!r}")
        if trade.shares <= 0 or trade.price < 0 or trade.fees < 0:
            raise LedgerError(f"{trade.ticker} {trade.traded_on}: shares must be positive")

        held = state.setdefault(
            trade.ticker,
            {"shares": 0.0, "cost": 0.0, "realised": 0.0, "fees": 0.0,
             "first": trade.traded_on, "last": trade.traded_on},
        )
        held["fees"] += trade.fees
        held["last"] = trade.traded_on

        if trade.action == "buy":
            held["shares"] += trade.shares
            held["cost"] += trade.shares * trade.price + trade.fees
            continue

        if trade.shares > held["shares"] + DUST:
            raise LedgerError(
                f"{trade.ticker} {trade.traded_on}: selling {trade.shares:g} shares "
                f"but only {held['shares']:g} held"
            )
        average = held["cost"] / held["shares"]
        held["realised"] += trade.shares * trade.price - trade.fees - trade.shares * average
        held["cost"] -= trade.shares * average
        held["shares"] -= trade.shares
        if held["shares"] <= DUST:
            # Float dust left in the basis of a position that is fully closed
            # would show up as a cost with no shares behind it.
            held["shares"] = 0.0
            held["cost"] = 0.0

    return {
        ticker: Position(
            ticker=ticker,
            shares=held["shares"],
            cost=held["cost"],
            realised=held["realised"],
            fees=held["fees"],
            first_bought=held["first"],
            last_traded=held["last"],
        )
        for ticker, held in state.items()
    }


def trades(conn: sqlite3.Connection, ticker: str | None = None) -> list[Trade]:
    """Every recorded trade, oldest first."""
    sql = "SELECT ticker, traded_on, action, shares, price, fees FROM dividend_lots"
    params: tuple = ()
    if ticker:
        sql += " WHERE ticker = ?"
        params = (ticker.upper(),)
    sql += " ORDER BY traded_on, id"
    return [Trade(*row) for row in conn.execute(sql, params)]


def cash_flows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every deposit, withdrawal and dividend, oldest first."""
    return conn.execute(
        "SELECT happened_on, kind, ticker, amount, note FROM dividend_cash "
        "ORDER BY happened_on, id"
    ).fetchall()


def record_trade(
    conn: sqlite3.Connection,
    ticker: str,
    action: str,
    shares: float,
    price: float,
    *,
    fees: float = 0.0,
    traded_on: str | None = None,
    note: str = "",
    now: str | None = None,
) -> int:
    """Append one trade, after checking the book still folds. Returns the row id."""
    if action not in TRADES:
        raise LedgerError(f"unknown action {action!r}; expected one of {', '.join(TRADES)}")
    when = traded_on or date.today().isoformat()
    ticker = ticker.upper()
    candidate = Trade(ticker, when, action, float(shares), float(price), float(fees))

    # The fold is the validator. Back-dating a sale must not invalidate a later
    # one either, so the whole name is replayed rather than just its total.
    fold([*trades(conn, ticker), candidate])

    with conn:
        cursor = conn.execute(
            "INSERT INTO dividend_lots "
            "(ticker, traded_on, action, shares, price, fees, note, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, when, action, float(shares), float(price), float(fees), note,
             now or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return int(cursor.lastrowid)


def record_cash(
    conn: sqlite3.Connection,
    kind: str,
    amount: float,
    *,
    ticker: str | None = None,
    happened_on: str | None = None,
    note: str = "",
    now: str | None = None,
) -> int:
    """Append one cash event. Amounts are magnitudes; ``kind`` carries the sign."""
    if kind not in CASH_KINDS:
        raise LedgerError(f"unknown kind {kind!r}; expected one of {', '.join(CASH_KINDS)}")
    if amount <= 0:
        raise LedgerError("amount must be positive; the kind decides its direction")
    if kind == "dividend" and not ticker:
        raise LedgerError("a dividend needs the ticker that paid it")

    with conn:
        cursor = conn.execute(
            "INSERT INTO dividend_cash "
            "(happened_on, kind, ticker, amount, note, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (happened_on or date.today().isoformat(), kind,
             ticker.upper() if ticker else None, float(amount), note,
             now or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return int(cursor.lastrowid)


def render(conn: sqlite3.Connection) -> list[str]:
    """The raw log, chronological, both tables interleaved by date."""
    rows = [
        (t.traded_on, f"{t.ticker:<6} {t.action:<8} {t.shares:>10,.4f} @ {t.price:>10,.2f}"
                      + (f"  fees {t.fees:,.2f}" if t.fees else ""))
        for t in trades(conn)
    ]
    rows += [
        (row["happened_on"],
         f"{(row['ticker'] or '—'):<6} {row['kind']:<8} {row['amount']:>10,.2f}"
         + (f"  {row['note']}" if row["note"] else ""))
        for row in cash_flows(conn)
    ]
    if not rows:
        return ["ledger is empty"]
    return [f"{len(rows)} entries, oldest first"] + [
        f"{when}  {line}" for when, line in sorted(rows)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="override the store path")
    sub = parser.add_subparsers(dest="command", required=True)

    for action in TRADES:
        cmd = sub.add_parser(action, help=f"record a {action}")
        cmd.add_argument("--ticker", required=True)
        cmd.add_argument("--shares", required=True, type=float)
        cmd.add_argument("--price", required=True, type=float, help="per share, as traded")
        cmd.add_argument("--fees", type=float, default=0.0)
        cmd.add_argument("--on", default=None, help="trade date; default today")
        cmd.add_argument("--note", default="")

    for kind in CASH_KINDS:
        cmd = sub.add_parser(kind, help=f"record a {kind}")
        cmd.add_argument("--amount", required=True, type=float, help="positive magnitude")
        cmd.add_argument("--ticker", default=None,
                         help="required for a dividend: which name paid it")
        cmd.add_argument("--on", default=None, help="default today")
        cmd.add_argument("--note", default="")

    sub.add_parser("list", help="print the raw log")

    args = parser.parse_args(argv)
    conn = store.connect(args.db)
    try:
        if args.command == "list":
            for line in render(conn):
                print(line)
            return 0

        try:
            if args.command in TRADES:
                row_id = record_trade(
                    conn, args.ticker, args.command, args.shares, args.price,
                    fees=args.fees, traded_on=args.on, note=args.note,
                )
                print(f"recorded #{row_id}: {args.ticker.upper()} {args.command} "
                      f"{args.shares:g} @ {args.price:,.2f}")
                print("record why, separately: python -m tradingagents.value.decisions record "
                      f"--ticker {args.ticker.upper()} --action "
                      f"{'buy' if args.command == 'buy' else 'sell'} --why \"...\"")
            else:
                row_id = record_cash(
                    conn, args.command, args.amount,
                    ticker=args.ticker, happened_on=args.on, note=args.note,
                )
                print(f"recorded #{row_id}: {args.command} {args.amount:,.2f}")
        except LedgerError as exc:
            print(f"not recorded: {exc}", file=sys.stderr)
            return 2
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
