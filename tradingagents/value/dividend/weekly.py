"""The weekly run: check what is held, name what broke, always say something.

    python -m tradingagents.value.dividend.weekly
    python -m tradingagents.value.dividend.weekly --dry-run

Weekly, not daily, and a **report** rather than a rebalance. Payout ratio moves
once a year and coverage four times, so a book that re-sorts itself every Monday
is churn wearing a schedule. What does change between Mondays is the price, and
this job deliberately does not act on it.

Order is the priority order, because a message is read from the top:

1. **breaks** — a name you hold whose dividend screen no longer passes;
2. **candidates** — names that pass and you do not hold, new ones marked;
3. **the book** — what it is worth, what it pays, what it has made.

Nothing here writes a position, proposes a size, or names an action. It reports
arithmetic about names the operator chose, exactly like ``jobs/daily.py``, and
for the reasons phases 4b and 6 established.

**The message is unconditional.** A week with no breaks and no new candidates
still sends. Silence is this job's normal state, so a dead cron and a healthy
portfolio produce the same empty inbox, and one line a week is what tells them
apart.

**Repetition is not.** A payout that broke in March is still broken in April;
saying so every Monday trains the operator to stop reading. Breaks are announced
once per distinct set of failed criteria, and a *second* criterion breaking is a
new signature and a new alert — that is the change worth interrupting for.
"""

import argparse
import contextlib
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..alerts import telegram
from . import book, config, history, ledger, runner, store

# How many cached dividend histories to re-fetch per run, oldest first. The
# universe is refreshed a slice at a time rather than all at once: a weekly job
# that fetches a thousand tickers is a weekly job that gets rate-limited.
REFRESH_LIMIT = 20


@dataclass(frozen=True)
class Break:
    """A held name whose screen no longer passes, and what it failed on."""

    ticker: str
    failed: tuple[str, ...]
    quality: float

    @property
    def signature(self) -> str:
        return ",".join(self.failed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def held(conn: sqlite3.Connection) -> list[str]:
    """Tickers with an open position, from the ledger and nowhere else."""
    return sorted(
        ticker
        for ticker, position in ledger.fold(ledger.trades(conn)).items()
        if position.is_open
    )


def refresh(conn: sqlite3.Connection, tickers: list[str], *, fetcher=history.fetch) -> list[str]:
    """Re-fetch dividend history. Failures are reported, never swallowed."""
    notes: list[str] = []
    done = 0
    for ticker in tickers:
        try:
            history.refresh(conn, ticker, fetcher=fetcher)
        except history.DividendError as exc:
            notes.append(f"{ticker}: history not refreshed — {exc}")
        else:
            done += 1
    if done:
        notes.append(f"refreshed {done} dividend history/histories")
    return notes


def breaks(
    conn: sqlite3.Connection,
    tickers: list[str],
    as_of: str,
    *,
    years: int = config.HISTORY_YEARS,
) -> tuple[list[Break], list[str]]:
    """Screen what is held. Returns the failures and the names with no verdict."""
    broken: list[Break] = []
    unknown: list[str] = []
    for ticker in tickers:
        outcome = runner.screen_one(conn, ticker, as_of, offline=True, years=years)
        if outcome.result is None:
            unknown.append(f"{ticker}: {outcome.error}")
            continue
        if not outcome.result.passed:
            broken.append(
                Break(ticker, outcome.result.failed_criteria, outcome.result.quality)
            )
    return broken, unknown


def candidates(
    conn: sqlite3.Connection,
    as_of: str,
    exclude: set[str],
    *,
    years: int = config.HISTORY_YEARS,
) -> tuple[list[runner.Outcome], int]:
    """Names that pass and are not held. Also how many had no history to screen.

    Offline on purpose: the cache is filled a slice at a time by ``refresh``, and
    a run that fetched every uncached name would be a run that gets banned.
    """
    passes: list[runner.Outcome] = []
    skipped = 0
    for ticker in store.cached(conn):
        if ticker in exclude:
            continue
        outcome = runner.screen_one(conn, ticker, as_of, offline=True, years=years)
        if outcome.result is None:
            skipped += 1
        elif outcome.result.passed:
            passes.append(outcome)
    passes.sort(key=lambda outcome: outcome.result.quality, reverse=True)
    return passes, skipped


def forward_yield(
    conn: sqlite3.Connection, ticker: str, price: float | None
) -> float | None:
    """Last full year's dividend per share over **today's** price, or ``None``.

    Today's price specifically, even when the screen runs with a past ``--as-of``.
    The ``dividends`` table is back-adjusted to the present, and only at the
    latest bar does every price basis coincide with it; before an intervening
    split the two sit on different bases and their ratio is a split factor, not a
    yield (``store.py``, the DDL note). Labelling the column as today's is
    cheaper than a basis repair and cannot be silently wrong.

    A rate, not a forecast: what the last twelve months would pay at this price,
    and the board can change it. ``None`` means unknown and renders as such -- a
    0.00% yield that actually means "no data" is the confidently wrong number
    this module refuses everywhere else.
    """
    dps = book.annual_dps(conn, ticker, date.today().isoformat())
    if dps is None or price is None or price <= 0:
        return None
    return dps / price


def rank_by_yield(
    conn: sqlite3.Connection,
    passes: list[runner.Outcome],
    *,
    closes=history.last_closes,
) -> tuple[list[runner.Outcome], dict[str, float | None]]:
    """Re-sort the pass list by income, and say what each name yields.

    The screen's own order is clean-share, which answers "how durable" and not
    "how much". A book bought to be spent from is asking the second question, so
    it decides the order.

    The whole list is priced in **one** download rather than a slice of it name
    by name. An earlier version priced the first 25 by clean-share, which sounded
    like a cost control and was in fact an alphabetical cut: over a hundred names
    tie at 100% clean, so the slice ran BR, CBSH, CDW, CSL … and every high
    yielder later in the alphabet was ranked as unknown. A ranking over a
    quarter of the list is not a ranking.

    Names with no price keep their clean-share order behind the priced ones
    rather than being dropped.
    """
    prices = closes([outcome.ticker for outcome in passes])
    yields = {
        outcome.ticker: forward_yield(conn, outcome.ticker, prices.get(outcome.ticker))
        for outcome in passes
    }
    ranked = sorted(
        passes,
        key=lambda outcome: (
            yields[outcome.ticker] if yields[outcome.ticker] is not None else -1.0
        ),
        reverse=True,
    )
    return ranked, yields


def compose(
    as_of: str,
    broken: list[Break],
    new_breaks: set[str],
    fresh: list[runner.Outcome],
    new_names: set[str],
    the_book: book.Book | None,
    irr: float | None,
    notes: list[str],
    yields: dict[str, float | None] | None = None,
) -> str:
    """The whole week in one message. Breaks first because they are read first."""
    lines = [f"dividend weekly — {as_of}"]

    lines.append("")
    if broken:
        lines.append(f"BROKEN ({len(broken)} held)")
        for item in sorted(broken, key=lambda b: b.ticker):
            mark = " NEW" if item.ticker in new_breaks else ""
            lines.append(f"  {item.ticker}{mark}: {', '.join(item.failed)} "
                         f"(clean {item.quality:.0%})")
        lines.append("  A break is arithmetic about the payout, not an instruction to sell.")
    else:
        lines.append("BROKEN: none — every held name still passes")

    lines.append("")
    if fresh:
        shown = fresh[:10]
        rates = yields or {}
        lines.append(f"CANDIDATES ({len(fresh)} pass, not held)")
        for outcome in shown:
            mark = " NEW" if outcome.ticker in new_names else ""
            rate = rates.get(outcome.ticker)
            income = f"yield {rate:.2%}" if rate is not None else "yield unknown"
            lines.append(
                f"  {outcome.ticker}{mark}: clean {outcome.result.quality:.0%}, {income}"
            )
        if len(fresh) > len(shown):
            lines.append(f"  … and {len(fresh) - len(shown)} more")
    else:
        lines.append("CANDIDATES: none pass this week")

    lines.append("")
    if the_book is None:
        lines.append("BOOK: not valued this run")
    elif not the_book.holdings:
        lines.append("BOOK: empty")
    else:
        lines.append(f"BOOK  value {the_book.market_value:,.2f}"
                     if the_book.market_value is not None else "BOOK")
        lines.append(f"  income {the_book.annual_income:,.2f}/yr"
                     + (f", excludes {', '.join(the_book.uncached)}"
                        if the_book.uncached else ""))
        if the_book.total_pnl is not None:
            lines.append(f"  P&L {the_book.total_pnl:+,.2f} on "
                         f"{the_book.net_invested:,.2f} invested")
        if irr is not None:
            lines.append(f"  money-weighted {irr:+.2%}/yr")

    if notes:
        lines += ["", "notes: " + "; ".join(notes)]
    lines += [
        "",
        "Evidence for your decision. Not a recommendation, not an order, not sized.",
        "Record what you do: python -m tradingagents.value.dividend.ledger …",
    ]
    return "\n".join(lines)


def _unannounced(
    conn: sqlite3.Connection,
    pairs: list[tuple[str, str]],
    stamp: str,
    record: bool,
) -> set[str]:
    """Which of these this run may say, claiming them when it is recording.

    A dry run reads the table and writes nothing. Claiming a row it will never
    send would let a rehearsal silence the real run that follows it.
    """
    allowed: set[str] = set()
    for ticker, signature in pairs:
        may_say = (
            store.claim(conn, ticker, signature, stamp)
            if record
            else not store.announced(conn, ticker, signature)
        )
        if may_say:
            allowed.add(ticker)
    return allowed


def weekly(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    years: int = config.HISTORY_YEARS,
    refresh_limit: int = REFRESH_LIMIT,
    sender=telegram.send,
    now=_now,
    record: bool = True,
    prices=None,
    splits=None,
    fetcher=history.fetch,
    closes=history.last_closes,
) -> str:
    """One full pass. Returns the message, which is always composed and always sent."""
    notes: list[str] = []
    positions = held(conn)

    if refresh_limit:
        slice_ = [t for t in store.stalest(conn, refresh_limit) if t not in positions]
        notes.extend(refresh(conn, positions + slice_, fetcher=fetcher))
    elif positions:
        notes.extend(refresh(conn, positions, fetcher=fetcher))

    broken, unknown = breaks(conn, positions, as_of, years=years)
    notes.extend(unknown)
    fresh, skipped = candidates(conn, as_of, set(positions), years=years)
    yields: dict[str, float | None] = {}
    try:
        fresh, yields = rank_by_yield(conn, fresh, closes=closes)
    except history.DividendError as exc:
        # A dead price feed degrades to a note, exactly as the book does below:
        # the breaks are the part worth waking up for and they need no price.
        notes.append(f"candidate yields unavailable: {exc}")
    if skipped:
        notes.append(f"{skipped} name(s) skipped: no dividend history cached yet")

    # Claim before sending, exactly as the daily job does: a row written after a
    # failed send is an alert nobody ever sees again.
    stamp = now()
    new_breaks = _unannounced(
        conn, [(item.ticker, item.signature) for item in broken], stamp, record
    )
    new_names = _unannounced(
        conn, [(outcome.ticker, "candidate") for outcome in fresh], stamp, record
    )

    the_book: book.Book | None = None
    irr = None
    try:
        the_book = book.build(conn, prices=prices, splits=splits)
        irr = book.money_weighted_return(conn, the_book)
    except Exception as exc:  # a dead price feed must not suppress the breaks
        notes.append(f"book not valued: {type(exc).__name__}: {exc}")

    text = compose(
        as_of, broken, new_breaks, fresh, new_names, the_book, irr, notes, yields
    )
    if sender(text) and record:
        for ticker in new_breaks:
            signature = next(b.signature for b in broken if b.ticker == ticker)
            store.confirm(conn, ticker, signature, stamp)
        for ticker in new_names:
            store.confirm(conn, ticker, "candidate", stamp)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--years", type=int, default=config.HISTORY_YEARS)
    parser.add_argument("--refresh-limit", type=int, default=REFRESH_LIMIT,
                        help="cached histories to re-fetch; 0 for held names only")
    parser.add_argument("--dry-run", action="store_true",
                        help="compose and print, send nothing, record nothing")
    parser.add_argument("--db", default=None, help="override the store path")
    args = parser.parse_args(argv)

    conn = store.connect(args.db)
    sender = (
        (lambda text: telegram.send(text, dry_run=True)) if args.dry_run else telegram.send
    )
    try:
        text = weekly(
            conn,
            args.as_of,
            years=args.years,
            refresh_limit=args.refresh_limit,
            sender=sender,
            record=not args.dry_run,
        )
    except Exception as exc:  # the cron's last word before it dies silently
        print(f"weekly run failed: {exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            sender(f"dividend weekly {args.as_of} FAILED — {type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
