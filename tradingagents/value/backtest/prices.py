"""Every price a backtest needs, fetched once per ticker and kept in memory.

``screen.market`` fetches per call, which is right for one daily screen and
catastrophic for a replay: forty rebalance dates times a universe would be tens
of thousands of round-trips for data that never changes. ``History`` fetches a
ticker's whole span on first touch and answers every later question from it.

It is deliberately **duck-typed to the ``market`` module** — same ``close``,
``annual_closes`` and ``risk_free_rate`` signatures, same ``PriceError`` — so
``runner.screen_one(..., prices=history)`` swaps it in with no change to the
screen. That parameter exists precisely for this.

Fetching is lazy for a reason that is also the cost model: ``screen_one`` only
asks for a price *after* a company clears the thirteen criteria, so only the
handful of survivors is ever downloaded, not the universe.

Tickers whose prices cannot be fetched are recorded in ``missing`` rather than
skipped quietly. That list is the survivorship-bias estimate the report must
print (plan section 10): a delisted company keeps its EDGAR filings forever but
loses its yfinance price series, so silence here reads as "never existed".
"""

from datetime import date, timedelta
from typing import Any

from ..screen.market import (
    AS_TRADED,
    TREASURY_10Y_TICKER,
    PriceError,
    _history,
    basis_factors,
    year_ends,
)


class History:
    """Cached price frames over one backtest span.

    ``start`` must already include the valuation lookback — the screen asks for
    ten years of year-end closes at the *first* rebalance date, not at the last.
    """

    def __init__(
        self,
        start: str,
        end: str,
        *,
        interval: str = "1d",
        fetch=_history,
    ) -> None:
        self._start = date.fromisoformat(start)
        self._end = date.fromisoformat(end)
        self._interval = interval
        self._fetch = fetch
        self._frames: dict[str, Any] = {}
        self._missing: dict[str, str] = {}

    @property
    def missing(self) -> dict[str, str]:
        """Tickers with no usable price series, and why — the bias estimate."""
        return dict(self._missing)

    @property
    def fetched(self) -> tuple[str, ...]:
        return tuple(sorted(self._frames))

    def frame(self, ticker: str):
        """The whole span for ``ticker``. One fetch per ticker, ever."""
        key = ticker.upper()
        if key in self._missing:
            # A second attempt would hit the same wall and cost another
            # round-trip; the first failure is the answer.
            raise PriceError(self._missing[key])
        if key not in self._frames:
            try:
                self._frames[key] = self._fetch(
                    key, self._start, self._end + timedelta(days=1), self._interval
                )
            except PriceError as exc:
                self._missing[key] = str(exc)
                raise
        return self._frames[key]

    def close(self, ticker: str, as_of: str | None = None) -> float:
        """Last as-traded close at or before ``as_of``. Raises rather than guessing.

        ``AS_TRADED``, not ``Close``: the valuation divides this by an EPS taken
        from the filings as they stood, so a split that happened after ``as_of``
        must not have been applied to it. ``frame()`` still hands the simulator
        the adjusted column, which is the one that survives a split cleanly.
        """
        rows = self._until(ticker, as_of)
        if rows.empty:
            raise PriceError(f"no price for {ticker} at or before {as_of or self._end}")
        return float(rows[AS_TRADED].iloc[-1])

    def annual_closes(self, ticker: str, years: int, as_of: str | None = None) -> dict[int, float]:
        """Last close of each of the ``years`` calendar years up to ``as_of``."""
        cutoff = date.fromisoformat(as_of) if as_of else self._end
        closes = year_ends(self.frame(ticker), cutoff)
        return {
            year: close for year, close in closes.items() if year > cutoff.year - years
        }

    def split_basis_factors(
        self, ticker: str, filed: dict[int, str], as_of: str | None = None
    ) -> dict[int, float]:
        """Per fiscal year, what to multiply its filed share count by."""
        cutoff = date.fromisoformat(as_of) if as_of else self._end
        return basis_factors(self.frame(ticker), filed, cutoff) if filed else {}

    def risk_free_rate(self, as_of: str | None = None) -> float:
        """10-year Treasury yield **as it stood on that date**, not today's.

        Discounting a 2016 valuation at a 2026 yield is look-ahead bias wearing
        a very ordinary-looking disguise.
        """
        return self.close(TREASURY_10Y_TICKER, as_of) / 100.0

    def _until(self, ticker: str, as_of: str | None):
        frame = self.frame(ticker)
        cutoff = date.fromisoformat(as_of) if as_of else self._end
        return frame[frame.index.date <= cutoff]
