"""Portfolio accounting for the replay, on ``backtrader``.

``backtrader`` is already a declared dependency of this repository and unused by
the other two subsystems, so the plan (section 10) says to reuse it rather than
write an engine. What it supplies is exactly the awkward half: position
tracking through rebalances, a commission model, drawdown, and per-trade
statistics that hand-rolled equity-curve arithmetic silently gets wrong.

What it does **not** do here is decide anything. The screen has already run by
the time this module is called, and the strategy below only executes a
precomputed schedule of ``(date, tickers)``. Keeping the screen out of the event
loop is what makes the sensitivity grid cheap: the same schedule re-simulates at
a different trigger level without re-screening or re-fetching a single price.

Orders fill at the **next** bar's open, backtrader's default. A rebalance
decided from Friday's close cannot be executed at Friday's close.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import backtrader as bt

Schedule = Sequence[tuple[str, tuple[str, ...]]]

CLOCK_FEED = "__clock__"

# Orders fill at the next bar's open, which is not the price the target was sized
# from. Aiming at 100% invested therefore gets rejected for margin roughly
# whenever the market opens up — silently, as a skipped rebalance. Hold back 2%.
CASH_BUFFER = 0.02


@dataclass(frozen=True)
class Result:
    """One simulated run. Every field is a number the plan asks the report for."""

    start_value: float
    end_value: float
    years: float
    max_drawdown: float
    trades: int
    winners: int
    average_bars_held: float
    rejected: int = 0

    @property
    def cagr(self) -> float | None:
        """None rather than 0.0 when the span is too short to annualise."""
        if self.years <= 0 or self.start_value <= 0 or self.end_value <= 0:
            return None
        return (self.end_value / self.start_value) ** (1 / self.years) - 1

    @property
    def total_return(self) -> float:
        return self.end_value / self.start_value - 1

    @property
    def hit_rate(self) -> float | None:
        """Share of closed trades that made money, or None if none closed."""
        if not self.trades:
            return None
        return self.winners / self.trades


class Rebalance(bt.Strategy):
    """Hold the scheduled names, equally weighted, until the next rebalance."""

    params = (("schedule", ()),)

    def __init__(self) -> None:
        self._pending = list(self.p.schedule)
        self._feeds = {data._name: data for data in self.datas if data._name != CLOCK_FEED}
        self.rejected = 0

    def notify_order(self, order) -> None:
        """Count orders the broker refused.

        A rejected order is a rebalance that did not happen, and backtrader's
        only complaint is a flatter equity curve. Counting them here is what
        keeps "the screen held cash" distinguishable from "the buy bounced".
        """
        if order.status in (order.Margin, order.Rejected, order.Canceled):
            self.rejected += 1

    def next(self) -> None:
        today = self.datas[0].datetime.date(0)
        target = self._due(today)
        if target is None:
            return

        for name, feed in self._feeds.items():
            if name not in target and self.getposition(feed).size:
                self.close(data=feed)

        tradable = [name for name in target if len(self._feeds.get(name, ()))]
        if not tradable:
            return
        weight = (1.0 - CASH_BUFFER) / len(tradable)

        # Sells before buys, always. Orders fill at the next open in submission
        # order, and a buy is sized off portfolio *value* while the broker funds
        # it out of *cash*. A name entering the portfolio leads the target list —
        # the report ranks by margin of safety — so its buy would otherwise be
        # checked before the reductions that release the cash for it, and the
        # broker refuses it for margin. The only symptom is a rebalance that
        # silently did not happen, which is why `rejected` is counted at all.
        target_value = weight * self.broker.getvalue()
        for name in sorted(tradable, key=lambda n: self._position_value(n) < target_value):
            self.order_target_percent(data=self._feeds[name], target=weight)

    def _position_value(self, name: str) -> float:
        """What the position in ``name`` is worth at today's close, or 0.0."""
        feed = self._feeds[name]
        return self.getposition(feed).size * feed.close[0]

    def _due(self, today: date) -> tuple[str, ...] | None:
        """The most recent schedule entry now due, or None if none is.

        Consumes every entry at or before ``today``: a rebalance date landing on
        a holiday must still happen on the next session, not be skipped.
        """
        target = None
        while self._pending and date.fromisoformat(self._pending[0][0]) <= today:
            target = self._pending.pop(0)[1]
        return target


def simulate(
    frames: dict[str, Any],
    schedule: Schedule,
    clock: Any,
    *,
    start: str,
    end: str,
    start_cash: float,
    commission: float,
) -> Result:
    """Run one schedule through backtrader and return its statistics."""
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(start_cash)
    cerebro.broker.setcommission(commission=commission)

    # The clock feed is datas[0] and is never traded: it gives the run a
    # complete calendar even in stretches when the screen holds nothing at all,
    # which — this being a value screen — is most of them.
    cerebro.adddata(_feed(clock, start, end), name=CLOCK_FEED)

    held = {ticker for _, tickers in schedule for ticker in tickers}
    for ticker in sorted(held):
        frame = frames.get(ticker)
        if frame is None:
            continue
        window = _feed(frame, start, end)
        if window is None:
            continue
        cerebro.adddata(window, name=ticker)

    cerebro.addstrategy(Rebalance, schedule=tuple(schedule))
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    strategies = cerebro.run()
    strategy = strategies[0]
    analyzers = strategy.analyzers
    drawdown = analyzers.drawdown.get_analysis()
    trades = analyzers.trades.get_analysis()

    span = date.fromisoformat(end) - date.fromisoformat(start)
    return Result(
        start_value=start_cash,
        end_value=cerebro.broker.getvalue(),
        years=span.days / 365.25,
        max_drawdown=float(drawdown.get("max", {}).get("drawdown", 0.0) or 0.0) / 100.0,
        trades=int(trades.get("total", {}).get("closed", 0) or 0),
        winners=int(trades.get("won", {}).get("total", 0) or 0),
        average_bars_held=float(trades.get("len", {}).get("average", 0.0) or 0.0),
        rejected=strategy.rejected,
    )


def _feed(frame: Any, start: str, end: str):
    """Trim a frame to the run window and hand it to backtrader.

    The frames carry a decade of extra history because the *valuation* needs it;
    feeding that to the broker would start the equity curve ten years early.
    """
    window = frame[
        (frame.index.date >= date.fromisoformat(start))
        & (frame.index.date <= date.fromisoformat(end))
    ]
    if window.empty:
        return None
    window = window.copy()
    if window.index.tz is not None:
        # backtrader converts the index with date2num, which has no notion of a
        # timezone; a tz-aware index silently shifts bars across a day boundary.
        window.index = window.index.tz_localize(None)
    return bt.feeds.PandasData(dataname=window)
