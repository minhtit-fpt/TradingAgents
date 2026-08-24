"""What the ledger adds up to: positions, income, and profit.

    python -m tradingagents.value.dividend.book
    python -m tradingagents.value.dividend.book --offline

Nothing here is stored. Every figure is folded out of ``dividend_lots`` and
``dividend_cash`` on each run, so the book cannot drift from the events that
produced it (``ledger.py``).

**Valued as of today, and only today.** A historical book would need the price,
the split basis and the dividend basis all moved to the same past date, and two
of those come from a source that back-adjusts to the present. Rather than offer
an ``--as-of`` that is subtly wrong before a split, this reports where the book
stands now.

**Splits.** Share counts are recorded as traded, so a 3-for-1 since purchase
leaves the ledger holding a third of the shares the broker now shows. Multiplying
that by today's price understates the position by two thirds — silently, which is
the worst way for a number to be wrong. Each trade is therefore moved onto the
current share basis before folding, reusing ``screen.market.split_basis_factors``.
``--offline`` skips that, and skips every figure that depends on it, rather than
printing an unadjusted one.

**Two dividend numbers, never merged.** ``recorded`` is cash you told the ledger
about. ``expected gross`` is shares held on each ex-date times the dividend per
share — before withholding tax, before timing. The gap between them is real
information (a 30% treaty withholding looks exactly like it), and averaging them
into one "income" figure would destroy it.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date

from ..screen import market
from . import history, ledger, store

# Bounds for the money-weighted return search. -99.99% is a total loss; 1000% a
# year is not a return this book will ever have to represent.
_RATE_FLOOR, _RATE_CEILING = -0.9999, 10.0
_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class Holding:
    """One position, valued. ``price`` is ``None`` under ``--offline``.

    ``annual_dps`` is ``None`` when no dividend history has been cached for the
    name, and ``0.0`` only when the cache says it paid nothing last year. The
    distinction matters: reporting an empty cache as zero income would be a
    confidently wrong number on the one line the operator reads for yield.
    """

    position: ledger.Position
    price: float | None
    annual_dps: float | None

    @property
    def ticker(self) -> str:
        return self.position.ticker

    @property
    def market_value(self) -> float | None:
        if self.price is None or not self.position.is_open:
            return 0.0 if self.price is not None else None
        return self.position.shares * self.price

    @property
    def unrealised(self) -> float | None:
        value = self.market_value
        return None if value is None else value - self.position.cost

    @property
    def annual_income(self) -> float | None:
        """Last full year's dividend per share times shares held. Not a forecast."""
        if self.annual_dps is None:
            return None
        return self.position.shares * self.annual_dps

    @property
    def yield_on_cost(self) -> float | None:
        income = self.annual_income
        if income is None or self.position.cost <= 0:
            return None
        return income / self.position.cost


@dataclass(frozen=True)
class Book:
    """The whole book. Every number derived, none stored."""

    as_of: str
    holdings: tuple[Holding, ...]
    cash: float
    deposits: float
    withdrawals: float
    dividends_recorded: float
    dividends_expected: float
    priced: bool

    @property
    def net_invested(self) -> float:
        return self.deposits - self.withdrawals

    @property
    def cost(self) -> float:
        return sum(h.position.cost for h in self.holdings)

    @property
    def market_value(self) -> float | None:
        if not self.priced:
            return None
        return sum(h.market_value or 0.0 for h in self.holdings)

    @property
    def equity(self) -> float | None:
        value = self.market_value
        return None if value is None else self.cash + value

    @property
    def realised(self) -> float:
        return sum(h.position.realised for h in self.holdings)

    @property
    def unrealised(self) -> float | None:
        if not self.priced:
            return None
        return sum(h.unrealised or 0.0 for h in self.holdings)

    @property
    def total_pnl(self) -> float | None:
        """Equity minus what you put in. Identical to realised + unrealised + dividends."""
        equity = self.equity
        return None if equity is None else equity - self.net_invested

    @property
    def annual_income(self) -> float:
        """Only the names whose history is cached. ``uncached`` names the rest."""
        return sum(h.annual_income or 0.0 for h in self.holdings)

    @property
    def uncached(self) -> tuple[str, ...]:
        """Open positions with no dividend history, so the income total is partial."""
        return tuple(
            h.ticker for h in self.holdings if h.position.is_open and h.annual_dps is None
        )


def adjust_for_splits(
    ticker: str,
    trades: list[ledger.Trade],
    as_of: str,
) -> list[ledger.Trade]:
    """Move each trade onto the share basis in force today. Dollars are unchanged."""
    if not trades:
        return []
    factors = market.split_basis_factors(
        ticker, {index: trade.traded_on for index, trade in enumerate(trades)}, as_of
    )
    return [
        ledger.Trade(
            ticker=trade.ticker,
            traded_on=trade.traded_on,
            action=trade.action,
            shares=trade.shares * factors[index],
            price=trade.price / factors[index],
            fees=trade.fees,
        )
        for index, trade in enumerate(trades)
    ]


def shares_on(trades: list[ledger.Trade], day: str) -> float:
    """Shares held at the close of ``day``, from trades already on one basis."""
    upto = [trade for trade in trades if trade.traded_on <= day]
    if not upto:
        return 0.0
    folded = ledger.fold(upto)
    return next(iter(folded.values())).shares if folded else 0.0


def expected_gross(conn: sqlite3.Connection, ticker: str, trades: list[ledger.Trade]) -> float:
    """Dividends the position should have earned, gross of withholding.

    Shares held on each ex-date times the dividend per share. Both sides are on
    today's split basis — the trades because ``adjust_for_splits`` put them
    there, the per-share amounts because that is how they are cached.
    """
    return sum(
        shares_on(trades, row["ex_date"]) * float(row["dps"])
        for row in store.as_of(conn, ticker, date.today().isoformat())
    )


def annual_dps(conn: sqlite3.Connection, ticker: str, as_of: str) -> float | None:
    """Last full calendar year's dividend per share, or ``None`` if nothing is cached."""
    if not store.as_of(conn, ticker, as_of):
        return None
    return sum(history.annual(conn, ticker, as_of, history.window_years(as_of, 1)).values())


def build(
    conn: sqlite3.Connection,
    *,
    offline: bool = False,
    prices=None,
    splits=None,
    refresh: bool = False,
) -> Book:
    """Fold the ledger into a book. A missing price raises rather than defaulting."""
    as_of = date.today().isoformat()
    quote = prices if prices is not None else market.close
    adjust = splits if splits is not None else adjust_for_splits

    all_trades = ledger.trades(conn)
    by_ticker: dict[str, list[ledger.Trade]] = {}
    for trade in all_trades:
        by_ticker.setdefault(trade.ticker, []).append(trade)

    holdings: list[Holding] = []
    expected_total = 0.0
    trade_cash = 0.0

    for ticker, raw in sorted(by_ticker.items()):
        if refresh and not offline:
            history.refresh(conn, ticker)
        adjusted = raw if offline else adjust(ticker, raw, as_of)
        trade_cash += sum(trade.cash for trade in adjusted)
        position = ledger.fold(adjusted)[ticker]
        expected_total += expected_gross(conn, ticker, adjusted)
        holdings.append(
            Holding(
                position=position,
                price=None if offline else quote(ticker, as_of),
                annual_dps=annual_dps(conn, ticker, as_of),
            )
        )

    deposits = withdrawals = dividends = 0.0
    for row in ledger.cash_flows(conn):
        amount = float(row["amount"])
        if row["kind"] == "deposit":
            deposits += amount
        elif row["kind"] == "withdraw":
            withdrawals += amount
        else:
            dividends += amount

    return Book(
        as_of=as_of,
        holdings=tuple(holdings),
        cash=deposits - withdrawals + dividends + trade_cash,
        deposits=deposits,
        withdrawals=withdrawals,
        dividends_recorded=dividends,
        dividends_expected=expected_total,
        priced=not offline,
    )


def money_weighted_return(conn: sqlite3.Connection, book: Book) -> float | None:
    """Annualised return over the operator's own cash flows. ``None`` if unsolvable.

    Only money crossing the account boundary counts as a flow. A dividend stays
    inside the book, so counting it as a deposit would report every payer as
    fresh capital and drag the return toward zero.
    """
    equity = book.equity
    if equity is None:
        return None

    flows = [
        (row["happened_on"], -float(row["amount"]) if row["kind"] == "deposit"
         else float(row["amount"]))
        for row in ledger.cash_flows(conn)
        if row["kind"] in ("deposit", "withdraw")
    ]
    if not flows:
        return None
    flows.append((book.as_of, equity))
    return _irr(flows)


def _irr(flows: list[tuple[str, float]]) -> float | None:
    """Bisection on the discount rate. ``None`` when no sign change brackets a root."""
    start = min(date.fromisoformat(day) for day, _ in flows)
    spans = [((date.fromisoformat(day) - start).days / _DAYS_PER_YEAR, amount)
             for day, amount in flows]

    def npv(rate: float) -> float:
        return sum(amount / (1 + rate) ** years for years, amount in spans)

    low, high = _RATE_FLOOR, _RATE_CEILING
    if npv(low) * npv(high) > 0:
        return None
    for _ in range(200):
        middle = (low + high) / 2
        if npv(low) * npv(middle) <= 0:
            high = middle
        else:
            low = middle
    return (low + high) / 2


def render(book: Book, irr: float | None) -> list[str]:
    """Four blocks: positions, income, cash, return."""
    if not book.holdings and not book.deposits:
        return ["the book is empty; record a deposit and a buy first"]

    money = "{:>12,.2f}".format
    lines = [f"dividend book as of {book.as_of}", ""]

    lines.append("POSITIONS")
    if not book.priced:
        lines.append("  offline: shares are as recorded, unadjusted for any split "
                     "since purchase; no prices, no market value")
    for holding in book.holdings:
        position = holding.position
        if not position.is_open:
            lines.append(f"  {position.ticker:<6} closed, realised {money(position.realised)}")
            continue
        average = position.average_cost or 0.0
        row = f"  {position.ticker:<6} {position.shares:>10,.4f} sh  cost {money(average)}/sh"
        if holding.price is not None:
            change = (holding.unrealised or 0.0) / position.cost if position.cost else 0.0
            row += (f"  now {money(holding.price)}/sh  value {money(holding.market_value)}"
                    f"  {change:+.1%}")
        lines.append(row)
        if position.realised:
            lines.append(f"         realised on part-sales {money(position.realised)}")

    lines += ["", "INCOME (last full year's rate, not a forecast)"]
    for holding in book.holdings:
        if not holding.position.is_open:
            continue
        if holding.annual_income is None:
            lines.append(f"  {holding.ticker:<6} no dividend history cached")
            continue
        on_cost = holding.yield_on_cost
        lines.append(
            f"  {holding.ticker:<6} {money(holding.annual_income)}/yr"
            + (f"  yield on cost {on_cost:.2%}" if on_cost is not None else "")
        )
    lines.append(f"  {'total':<6} {money(book.annual_income)}/yr")
    if book.uncached:
        lines.append(f"  total excludes {', '.join(book.uncached)}; cache their history with"
                     " --refresh")
    lines.append(f"  received, recorded {money(book.dividends_recorded)}")
    lines.append(f"  expected gross to date {money(book.dividends_expected)}"
                 "   (before withholding tax and payment timing)")

    lines += [
        "",
        "CASH",
        f"  paid in           {money(book.deposits)}",
        f"  taken out         {money(book.withdrawals)}",
        f"  dividends in      {money(book.dividends_recorded)}",
        f"  balance           {money(book.cash)}",
        "",
        "RETURN",
        f"  net invested      {money(book.net_invested)}",
    ]
    if book.priced:
        lines += [
            f"  market value      {money(book.market_value)}",
            f"  equity            {money(book.equity)}",
            f"  realised          {money(book.realised)}",
            f"  unrealised        {money(book.unrealised)}",
            f"  dividends         {money(book.dividends_recorded)}",
            f"  total P&L         {money(book.total_pnl)}",
        ]
        lines.append(
            f"  money-weighted    {irr:>11.2%}/yr" if irr is not None
            else "  money-weighted    not solvable from these flows"
        )
    else:
        lines.append("  offline: equity, P&L and return need prices")

    lines += [
        "",
        "This is a record of what you did, not a recommendation about what to do next.",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="override the store path")
    parser.add_argument("--offline", action="store_true",
                        help="ledger figures only: no prices, no split adjustment")
    parser.add_argument("--refresh", action="store_true",
                        help="fetch the dividend history of every name held first")
    args = parser.parse_args(argv)

    conn = store.connect(args.db)
    try:
        try:
            book = build(conn, offline=args.offline, refresh=args.refresh)
        except (market.PriceError, history.DividendError, ledger.LedgerError) as exc:
            print(f"cannot value the book: {exc}", file=sys.stderr)
            print("re-run with --offline for the figures that need no price", file=sys.stderr)
            return 2
        for line in render(book, money_weighted_return(conn, book)):
            print(line)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
