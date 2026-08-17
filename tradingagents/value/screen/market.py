"""Prices and the discount rate, straight from yfinance.

The isolation contract (plan section 2) forbids routing through the shared
vendor registry, so this module calls ``yfinance`` directly. Duplicating a small
price fetch is the intended trade.

**Deviation from the plan, deliberate:** section 4 sources the discount rate
from FRED ``DGS10``, which needs an API key. ``^TNX`` is the same 10-year
Treasury yield, already reachable through the dependency we must use anyway, so
this drops a required secret and a failure mode for no loss of fidelity.

Nothing here falls back to a default number. A missing price raises — a screen
that quietly values a company at a stale or invented price is worse than a
screen that stops.

**Two price series, deliberately.** yfinance back-applies every split a company
has ever done, including splits that happen *after* the date being screened:
Netflix's 2024-12-31 close reads 89.13 today because of a 10-for-1 split in
November 2025. Share counts come from EDGAR as filed and are never restated
backwards, so dividing a 2025-basis price by a 2024-basis EPS is off by the
whole split factor — and it is look-ahead too, since a future split leaks in.
So each frame carries both:

- ``Close`` — the split-adjusted series, on *today's* share basis. Continuous
  through a split, which is what a portfolio simulator needs to compute a
  return across one.
- ``AsTradedClose`` — the price actually quoted that day, on that day's basis.

Their ratio at a date is the product of every split since — which is the only
thing that lets ``split_basis_factors`` move a filed share count onto the basis
in force on the as-of date. EDGAR restates share counts forward only in the
filings that carry the year as a comparative, so a ten-year history assembled
from several 10-Ks straddles a split with a tenfold step in it.
"""

from datetime import date, timedelta

TREASURY_10Y_TICKER = "^TNX"

# Enough calendar days back to clear a long weekend plus a holiday.
_LOOKBACK_DAYS = 10

# The as-traded column added to every frame this module returns.
AS_TRADED = "AsTradedClose"


class PriceError(RuntimeError):
    """No usable price. Never substituted with a guess."""


def close(ticker: str, as_of: str | None = None) -> float:
    """Last as-traded close at or before ``as_of`` (``YYYY-MM-DD``), default today.

    The price actually payable that day — not back-adjusted for later splits,
    and not back-adjusted for dividends either. Margin of safety compares it
    against an EPS on the same share basis.
    """
    end = date.fromisoformat(as_of) if as_of else date.today()
    frame = _history(ticker, end - timedelta(days=_LOOKBACK_DAYS), end + timedelta(days=1))
    return float(frame[AS_TRADED].iloc[-1])


def annual_closes(ticker: str, years: int, as_of: str | None = None) -> dict[int, float]:
    """Last close of each calendar year, for the normalising P/E median.

    Calendar-year ends approximate fiscal-year ends. For a company closing its
    books in June the pairing is off by two quarters — acceptable for a median
    taken over a decade, and the alternative (a price lookup per period end)
    buys precision the capped terminal multiple then discards anyway.
    """
    end = date.fromisoformat(as_of) if as_of else date.today()
    start = date(end.year - years, 1, 1)
    frame = _history(ticker, start, end + timedelta(days=1), interval="1mo")
    return year_ends(frame, end)


def year_ends(frame, cutoff: date) -> dict[int, float]:
    """Last close of each calendar year, on the share basis in force at ``cutoff``.

    The as-of basis, not each year's own: these prices are divided by an EPS
    history that ``split_basis_factors`` has already moved onto the as-of basis,
    and a P/E is only meaningful when both halves count the same share.
    """
    scale = _split_ratio(frame, cutoff)
    return {
        stamp.year: float(row["Close"]) * scale
        for stamp, row in frame.iterrows()
        if stamp.date() <= cutoff
    }


def split_basis_factors(
    ticker: str, filed: dict[int, str], as_of: str | None = None
) -> dict[int, float]:
    """Per fiscal year, what to multiply its filed share count by.

    A share count is on the basis in force when its filing was made. Nvidia's
    FY2021 count was filed in February 2024 and counts pre-split shares; its
    FY2022 count came from a later 10-K and counts post-split ones. Both are
    correct as filed and they are a factor of ten apart, so a valuation that
    reads them as one series sees earnings per share collapse by 90%.
    """
    if not filed:
        return {}
    end = date.fromisoformat(as_of) if as_of else date.today()
    start = min(date.fromisoformat(day) for day in filed.values())
    frame = _history(ticker, start - timedelta(days=_LOOKBACK_DAYS), end + timedelta(days=1))
    return basis_factors(frame, filed, end)


def basis_factors(frame, filed: dict[int, str], cutoff: date) -> dict[int, float]:
    """``split_basis_factors`` against an already-fetched frame."""
    base = _split_ratio(frame, cutoff)
    return {
        year: _split_ratio(frame, date.fromisoformat(day)) / base
        for year, day in filed.items()
    }


def _split_ratio(frame, when: date) -> float:
    """Product of every split ratio after ``when``, read off the two columns."""
    rows = frame[frame.index.date <= when]
    if rows.empty:
        raise PriceError(f"no price at or before {when} to read the split basis from")
    return float(rows[AS_TRADED].iloc[-1]) / float(rows["Close"].iloc[-1])


def median_pe(closes: dict[int, float], eps: list[tuple[int, float]]) -> float | None:
    """Median of year-end price over that year's EPS, ignoring loss years."""
    ratios = sorted(
        closes[year] / value
        for year, value in eps
        if year in closes and value > 0
    )
    if not ratios:
        return None
    middle = len(ratios) // 2
    if len(ratios) % 2:
        return ratios[middle]
    return (ratios[middle - 1] + ratios[middle]) / 2


def risk_free_rate(as_of: str | None = None) -> float:
    """10-year Treasury yield as a decimal (``^TNX`` quotes 4.21 for 4.21%)."""
    return close(TREASURY_10Y_TICKER, as_of) / 100.0


def _history(ticker: str, start: date, end: date, interval: str = "1d"):
    # Imported here, not at module scope: the rest of the screen is pure and
    # should stay importable — and testable against a stub — on a machine that
    # never fetches a price.
    import yfinance as yf

    try:
        handle = yf.Ticker(ticker)
        frame = handle.history(
            start=start.isoformat(),
            end=end.isoformat(),
            interval=interval,
            auto_adjust=False,
        )
        # The whole split history, not the window's: the split that corrupts a
        # 2024 valuation is the one that happened in 2025, outside the frame.
        splits = handle.splits
    except Exception as exc:  # yfinance raises a zoo of transport-level errors
        raise PriceError(f"price lookup failed for {ticker}: {exc}") from exc

    if frame.empty or "Close" not in frame:
        raise PriceError(f"no prices for {ticker} between {start} and {end}")
    return with_as_traded(frame.dropna(subset=["Close"]), splits)


def with_as_traded(frame, splits):
    """Add the ``AS_TRADED`` column: ``Close`` with later splits undone.

    A row's price is multiplied by every split ratio dated strictly after it,
    which is precisely the back-adjustment yfinance applied on the way in.
    """
    frame = frame.copy()
    frame[AS_TRADED] = frame["Close"]
    if splits is None or len(splits) == 0:
        return frame

    for stamp, ratio in splits.items():
        if not ratio or ratio <= 0:
            continue
        earlier = frame.index.date < stamp.date()
        frame.loc[earlier, AS_TRADED] = frame.loc[earlier, AS_TRADED] * ratio
    return frame
