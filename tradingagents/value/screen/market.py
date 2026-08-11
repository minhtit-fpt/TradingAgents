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
"""

from datetime import date, timedelta

TREASURY_10Y_TICKER = "^TNX"

# Enough calendar days back to clear a long weekend plus a holiday.
_LOOKBACK_DAYS = 10


class PriceError(RuntimeError):
    """No usable price. Never substituted with a guess."""


def close(ticker: str, as_of: str | None = None) -> float:
    """Last close at or before ``as_of`` (``YYYY-MM-DD``), default today.

    Unadjusted: margin of safety compares against the price actually payable,
    not a back-adjusted series that drifts every time a dividend is paid.
    """
    end = date.fromisoformat(as_of) if as_of else date.today()
    frame = _history(ticker, end - timedelta(days=_LOOKBACK_DAYS), end + timedelta(days=1))
    return float(frame["Close"].iloc[-1])


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

    closes: dict[int, float] = {}
    for stamp, row in frame.iterrows():
        closes[stamp.year] = float(row["Close"])
    return closes


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
        frame = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=end.isoformat(),
            interval=interval,
            auto_adjust=False,
        )
    except Exception as exc:  # yfinance raises a zoo of transport-level errors
        raise PriceError(f"price lookup failed for {ticker}: {exc}") from exc

    if frame.empty or "Close" not in frame:
        raise PriceError(f"no prices for {ticker} between {start} and {end}")
    return frame.dropna(subset=["Close"])
