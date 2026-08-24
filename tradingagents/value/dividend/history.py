"""Per-share dividend history, cached by ex-date.

Sourced from ``yfinance`` rather than EDGAR, deliberately. EDGAR's
``DividendsPaid`` is a total cash outflow on the cash-flow statement; turning it
into a per-share figure means dividing by a filed share count, and filed share
counts straddle splits on different bases (see ``screen/market.py``). yfinance
publishes the dividend per share directly, back-adjusted uniformly across the
whole history, so a year-on-year comparison is exact without any basis repair.

Two rules the rest of the module depends on:

- **Only fully elapsed calendar years count.** On 2026-08-24 the 2026 dividends
  are two payments into a four-payment year. Comparing that against 2025 reads
  as a 50% cut on every quarterly payer alive, so the current year is excluded
  rather than annualised — a guess about the two payments still to come is
  exactly the invented number this codebase refuses elsewhere.
- **A year inside the window with no cached payment is a year that paid
  nothing.** The window is dense by construction; absence is the signal.
"""

from datetime import date, datetime, timezone

from . import config, store


class DividendError(RuntimeError):
    """No usable dividend history. Never substituted with a guess."""


def window_years(as_of: str, years: int = config.HISTORY_YEARS) -> tuple[int, ...]:
    """The ``years`` fully elapsed calendar years ending before ``as_of``."""
    last = date.fromisoformat(as_of).year - 1
    return tuple(range(last - years + 1, last + 1))


def fetch(ticker: str) -> list[tuple[str, float]]:
    """``(ex_date, dps)`` for the whole listed history, oldest first."""
    # Imported here, not at module scope, so the criteria stay importable and
    # testable on a machine that never reaches the network.
    import yfinance as yf

    try:
        series = yf.Ticker(ticker).dividends
    except Exception as exc:  # yfinance raises a zoo of transport-level errors
        raise DividendError(f"dividend lookup failed for {ticker}: {exc}") from exc

    if series is None or len(series) == 0:
        raise DividendError(f"no dividend history for {ticker}")
    return [
        (stamp.date().isoformat(), float(amount))
        for stamp, amount in series.items()
        if amount and amount > 0
    ]


def refresh(conn, ticker: str, *, fetcher=fetch, now: str | None = None) -> int:
    """Fetch and cache one name's history. Returns the number of payments cached."""
    stamp = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return store.upsert(conn, ticker, fetcher(ticker), stamp)


def annual(conn, ticker: str, as_of: str, window: tuple[int, ...]) -> dict[int, float]:
    """Dividends per share summed by calendar year of ex-date, over ``window``.

    Years in the window with no payment are present with a value of ``0.0``, not
    absent: the "never missed a year" criterion is about exactly those years, and
    a dict that silently omits them would score a payer that stopped in 2020 as
    clean.
    """
    totals = dict.fromkeys(window, 0.0)
    for row in store.as_of(conn, ticker, as_of):
        year = int(row["ex_date"][:4])
        if year in totals:
            totals[year] += float(row["dps"])
    return totals
