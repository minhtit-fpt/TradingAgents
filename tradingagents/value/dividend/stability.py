"""D5 -- the price-stability rank: which durable payers also sat still.

    python -m tradingagents.value.dividend.stability --size 15
    python -m tradingagents.value.dividend.stability --max-drawdown 0.30 --plain

D1 asks whether the *dividend* was durable. It asks nothing at all about the
share price, and D4 measured what that omission costs: an equal-weight book of
the whole pass list fell 37.8% through 2020 and 54.6% through 2008. The stated
requirement is a book that pays cash, moves little, falls little, and rises when
it can -- so this scores the three price properties the screen never looked at,
over the names it has already passed.

Selection, in that order:

    filter   forward yield              >= min_yield
    filter   annualised volatility      <= max_volatility
    filter   worst peak-to-trough fall  <= max_drawdown
    rank     annualised price return, highest first    ("if it rises, good")

Limits on the three requirements and a plain rank on the bonus, rather than one
weighted score. A score needs weights nobody here can defend, and it hides which
constraint actually bound -- which is the only part of the output that tells the
operator what to loosen.

The yield floor is not decoration. Ranking by return alone, the first live run
returned MSFT, V and MSI at the top and a basket yielding 1.32%: durable payers,
all of them, and an income book by no definition at all. "If it rises, good" is
a tie-breaker among names that already pay, not a reason to hold one that
barely does.

**Price only, dividends excluded.** ``auto_adjust=False``, so ``Close`` is
split-adjusted and dividend-unadjusted -- the same basis ``backtest.book_drawdown``
uses, for the same reason: income spent as it arrives is not in the account to
cushion a fall. Counting it would flatter precisely the number this exists to
give.

**What this cannot do.** It selects on a past window, so names look calm because
their window was calm, and a filter tuned on a decade holding one crash is tuned
on one crash. The basket block therefore prints the fall that was *measured* and
the share of capital that would have kept the whole portfolio inside the loss
floor -- arithmetic on what happened, never a bound on what comes next.

Names an action for nobody and writes no position, like every other surface here.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta

from ..store import db
from . import config, history, runner, weekly
from .backtest import PORTFOLIO_LOSS_FLOOR, sizing_for_floor

# Daily bars in a year. Only ever used to annualise a standard deviation.
TRADING_DAYS = 252

# A name whose series starts later than this into the window is not comparable
# with one that lived through the whole of it -- its drawdown is the drawdown of
# a shorter, luckier life. Dropped and counted, not scored.
MAX_LATE_START_DAYS = 45


class StabilityError(RuntimeError):
    """No usable price series. Never substituted with a guess."""


@dataclass(frozen=True)
class Stability:
    """One name's price behaviour over the window. All three, or the name drops."""

    ticker: str
    volatility: float
    max_drawdown: float  # negative magnitude, e.g. -0.32
    annual_return: float

    def within(self, max_volatility: float, max_drawdown: float) -> bool:
        return self.volatility <= max_volatility and abs(self.max_drawdown) <= max_drawdown


def closes(tickers: list[str], start: str, end: str):
    """Split-adjusted daily closes for many names, in one download.

    One request rather than one per name, for the reason ``history.last_closes``
    records: the alternative is pricing a slice of the pass list and calling the
    slice a ranking.
    """
    if not tickers:
        return {}
    import yfinance as yf

    try:
        frame = yf.download(
            tickers=" ".join(sorted(tickers)),
            start=start,
            end=end,
            interval="1d",
            progress=False,
            auto_adjust=False,
            threads=True,
        )
    except Exception as exc:
        raise StabilityError(f"price lookup failed: {exc}") from exc

    if frame is None or frame.empty or "Close" not in frame:
        raise StabilityError("price lookup returned nothing")

    column = frame["Close"]
    if not hasattr(column, "columns"):  # a single ticker comes back as a Series
        column = column.to_frame(name=sorted(tickers)[0])
    return {
        str(ticker): column[ticker].dropna()
        for ticker in column.columns
        if not column[ticker].dropna().empty
    }


def drawdown(curve) -> float:
    """Worst peak-to-trough fall of a price path, as a negative fraction."""
    return float((curve / curve.cummax() - 1.0).min())


def measure(ticker: str, curve, start: str) -> Stability | None:
    """Volatility, worst fall and annualised return. ``None`` when not comparable.

    Returns ``None`` rather than a partial row: a name with two years of history
    scored against names with ten would win the drawdown filter for having missed
    the crash, which is the opposite of what the filter is for.
    """
    if len(curve) < TRADING_DAYS:
        return None
    first = curve.index[0].date()
    if (first - date.fromisoformat(start)).days > MAX_LATE_START_DAYS:
        return None

    returns = curve.pct_change().dropna()
    if returns.empty:
        return None
    years = (curve.index[-1].date() - first).days / 365.25
    if years <= 0 or float(curve.iloc[0]) <= 0:
        return None

    growth = float(curve.iloc[-1]) / float(curve.iloc[0])
    return Stability(
        ticker=ticker,
        volatility=float(returns.std()) * TRADING_DAYS**0.5,
        max_drawdown=drawdown(curve),
        annual_return=growth ** (1.0 / years) - 1.0,
    )


def measure_all(curves: dict, start: str) -> tuple[list[Stability], list[str]]:
    """Score every name. Second element is the names dropped as not comparable."""
    scored, dropped = [], []
    for ticker, curve in curves.items():
        row = measure(ticker, curve, start)
        if row is None:
            dropped.append(ticker)
        else:
            scored.append(row)
    return scored, dropped


@dataclass(frozen=True)
class Cuts:
    """How many names each limit removed, in the order they were applied."""

    unyielding: int
    volatile: int
    deep: int


def select(
    scored: list[Stability],
    yields: dict[str, float | None],
    *,
    min_yield: float = config.MIN_YIELD,
    max_volatility: float = config.MAX_VOLATILITY,
    max_drawdown: float = config.MAX_DRAWDOWN,
    size: int = config.BASKET_SIZE,
) -> tuple[list[Stability], Cuts]:
    """Apply the three limits, then rank the survivors by return.

    The counts are the output's most useful line when the basket comes back
    short: they say which limit emptied it, which is the one thing a bare empty
    list cannot.

    A name whose yield is unknown is cut by the floor rather than passed by it.
    An unpriced name is not a name that pays enough; it is a name nobody
    measured, and letting it through would put a blank where the requirement is.
    """
    paying = [row for row in scored if (yields.get(row.ticker) or 0.0) >= min_yield]
    calm = [row for row in paying if row.volatility <= max_volatility]
    shallow = [row for row in calm if abs(row.max_drawdown) <= max_drawdown]
    shallow.sort(key=lambda row: row.annual_return, reverse=True)
    return shallow[:size], Cuts(
        unyielding=len(scored) - len(paying),
        volatile=len(paying) - len(calm),
        deep=len(calm) - len(shallow),
    )


def basket(curves: dict, tickers: list[str], start: str) -> Stability | None:
    """The equal-weight book itself: bought at the window's start, never rebalanced.

    Measured as one path rather than averaged across the names, because the
    number the operator asked about is the portfolio's fall, and the names do not
    all bottom on the same day. The average of the parts is always worse than the
    whole, and using it would reject baskets that were fine.
    """
    import pandas as pd

    paths = [curves[t] / float(curves[t].iloc[0]) for t in tickers if t in curves]
    if not paths:
        return None
    equity = pd.concat(paths, axis=1).ffill().dropna().mean(axis=1)
    return measure("BASKET", equity, start)


def render(
    chosen: list[Stability],
    yields: dict[str, float | None],
    book: Stability | None,
    *,
    universe: int,
    dropped: int,
    cuts: Cuts,
    window: tuple[str, str],
    floor: float,
) -> list[str]:
    lines = [
        f"PRICE STABILITY  {window[0]} -> {window[1]}",
        f"  {universe} names passed the dividend screen; {dropped} not comparable, "
        f"{cuts.unyielding} yield too little, {cuts.volatile} too volatile, "
        f"{cuts.deep} fell too far",
        "",
    ]
    if not chosen:
        lines.append("no name cleared all three limits. Loosen one, or take the empty answer.")
        return lines

    lines.append(f"{'ticker':<8}{'vol':>8}{'worst':>9}{'return/yr':>11}{'yield':>9}")
    for row in chosen:
        rate = yields.get(row.ticker)
        shown = f"{rate:.2%}" if rate is not None else "unknown"
        lines.append(
            f"{row.ticker:<8}{row.volatility:>8.1%}{row.max_drawdown:>9.1%}"
            f"{row.annual_return:>11.1%}{shown:>9}"
        )

    known = [r for r in (yields.get(row.ticker) for row in chosen) if r is not None]
    lines.append("")
    lines.append(f"BASKET  {len(chosen)} names, equal weight, held from {window[0]}")
    if known:
        lines.append(f"  yield        {sum(known) / len(known):.2%}  (mean of {len(known)} priced)")
    if book is None:
        lines.append("  not priced as a book: no overlapping series")
        return lines

    lines.append(f"  volatility   {book.volatility:.1%}/yr")
    lines.append(f"  worst fall   {book.max_drawdown:.1%}")
    lines.append(f"  return       {book.annual_return:.1%}/yr")
    lines.append(
        f"  holding the whole portfolio inside {floor:.0%} would have needed at most "
        f"{sizing_for_floor(book.max_drawdown, floor):.0%} of capital in these names, "
        "the rest in something that does not fall."
    )
    lines.append("  Measured over one past window. Not a bound on the next one.")
    return lines


@dataclass(frozen=True)
class Selection:
    """Everything one D5 run decided, before any of it is turned into text.

    ``run`` renders it; ``brief`` reads the same object to decide which names the
    LLM is asked about. Separated so the two surfaces cannot disagree about what
    the basket was -- the alternative is a second copy of the wiring below, which
    is how two answers to "what did the screen choose" start to exist.
    """

    chosen: list[Stability]
    yields: dict[str, float | None]
    book: Stability | None
    cuts: Cuts
    universe: int
    dropped: int
    window: tuple[str, str]
    # The screened outcome per chosen name, so a caller that needs the dividend
    # criteria behind a pick does not have to screen the name a second time.
    passes: dict[str, "runner.Outcome"]


def selection(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    years: int = config.STABILITY_YEARS,
    min_yield: float = config.MIN_YIELD,
    max_volatility: float = config.MAX_VOLATILITY,
    max_drawdown: float = config.MAX_DRAWDOWN,
    size: int = config.BASKET_SIZE,
    fetch=closes,
    last=history.last_closes,
) -> Selection:
    """Screen offline, price the pass list once, rank. No rendering, no LLM."""
    passes, _ = weekly.candidates(conn, as_of, exclude=set())
    tickers = [outcome.ticker for outcome in passes]
    start = (date.fromisoformat(as_of) - timedelta(days=round(365.25 * years))).isoformat()
    curves = fetch(tickers, start, as_of)

    scored, not_comparable = measure_all(curves, start)

    # Priced before the cut, not after it. Yield is one of the three limits now,
    # and pricing only the survivors would mean filtering on a column that does
    # not exist yet -- the same slice-and-call-it-a-ranking defect D3 already hit.
    prices = last([row.ticker for row in scored]) if scored else {}
    yields: dict[str, float | None] = {
        row.ticker: weekly.forward_yield(conn, row.ticker, prices.get(row.ticker))
        for row in scored
    }

    chosen, cuts = select(
        scored,
        yields,
        min_yield=min_yield,
        max_volatility=max_volatility,
        max_drawdown=max_drawdown,
        size=size,
    )

    return Selection(
        chosen=chosen,
        yields=yields,
        book=basket(curves, [row.ticker for row in chosen], start),
        cuts=cuts,
        universe=len(tickers),
        # Names the screen passed but the download never returned are as
        # uncomparable as the ones that started late, and are counted with them.
        dropped=len(not_comparable) + len(tickers) - len(curves),
        window=(start, as_of),
        passes={outcome.ticker: outcome for outcome in passes},
    )


def run(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    years: int = config.STABILITY_YEARS,
    min_yield: float = config.MIN_YIELD,
    max_volatility: float = config.MAX_VOLATILITY,
    max_drawdown: float = config.MAX_DRAWDOWN,
    size: int = config.BASKET_SIZE,
    floor: float = PORTFOLIO_LOSS_FLOOR,
    fetch=closes,
    last=history.last_closes,
) -> list[str]:
    """Select, then describe what was selected."""
    picked = selection(
        conn,
        as_of,
        years=years,
        min_yield=min_yield,
        max_volatility=max_volatility,
        max_drawdown=max_drawdown,
        size=size,
        fetch=fetch,
        last=last,
    )
    return render(
        picked.chosen,
        picked.yields,
        picked.book,
        universe=picked.universe,
        dropped=picked.dropped,
        cuts=picked.cuts,
        window=picked.window,
        floor=floor,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rank durable payers by how still the price sat")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--years", type=int, default=config.STABILITY_YEARS)
    parser.add_argument("--min-yield", type=float, default=config.MIN_YIELD)
    parser.add_argument("--max-volatility", type=float, default=config.MAX_VOLATILITY)
    parser.add_argument("--max-drawdown", type=float, default=config.MAX_DRAWDOWN)
    parser.add_argument("--size", type=int, default=config.BASKET_SIZE)
    parser.add_argument("--loss-floor", type=float, default=PORTFOLIO_LOSS_FLOOR)
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        lines = run(
            conn,
            args.as_of,
            years=args.years,
            min_yield=args.min_yield,
            max_volatility=args.max_volatility,
            max_drawdown=args.max_drawdown,
            size=args.size,
            floor=args.loss_floor,
        )
    except StabilityError as exc:
        print(f"no verdict: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
