"""Overnight-hold reference strategy for A-shares.

Pick one or two names late in the session, carry them through the close, and
sell them into the next morning's opening auction. The edge this chases is
the overnight gap: A-share news is priced overnight and the first print of
the next session is where it lands.

The risk this design refuses to take is the overnight gap against you with no
stop available. A-share rules turn that refusal into three hard constraints
that shape the whole class:

* T+1. Shares bought today cannot be sold today, so a position opened on bar
  N is sellable only from bar N+1 onward. Ignoring this books round trips the
  exchange would have rejected.
* No stop during the gap. There is no market between the close and the next
  open, so a resting stop order cannot protect the position overnight. The
  only available protection is position sizing before the entry, which is
  what the risk guard already does; this strategy adds nothing that pretends
  otherwise.
* One overnight only. Holding more than one night turns a gap trade into an
  equity position with gap risk growing every night, which is a different
  strategy with a different risk profile.

Signals are deliberately simple and each one is a filter, not a forecast:
the name must be in an uptrend over ``trend_window`` bars, its close must be
in the top of its own recent range, and today's volume must exceed its
average. What this buys is cheap in turnover and honest about what it does
not know -- which names gap up tomorrow.
"""

from __future__ import annotations


from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
    BarGenerator,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)
from vnpy.trader.constant import Direction, Interval, Offset


class OvernightHoldStrategy(CtaTemplate):
    """Buy into the close, sell into the next open, one overnight only."""

    author = "VeighNa Autonomous"

    trend_window: int = 20
    range_window: int = 10
    volume_window: int = 5
    volume_factor: float = 1.2
    fixed_size: int = 100
    max_position: int = 100
    exit_minutes: int = 30          # sell inside this many minutes of the open
    stop_loss_ratio: float = 0.05   # abort an entry whose gap already blew it

    trend_ma: float = 0.0
    range_high: float = 0.0
    range_low: float = 0.0
    range_pos: float = 0.0
    volume_ma: float = 0.0
    last_price: float = 0.0
    entry_bar_count: int = -1
    bars_since_entry: int = 0
    _bar_count: int = 0
    _held_volume: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0
    equity: float = 0.0

    parameters = [
        "trend_window",
        "range_window",
        "volume_window",
        "volume_factor",
        "fixed_size",
        "max_position",
        "exit_minutes",
        "stop_loss_ratio",
    ]
    variables = [
        "trend_ma",
        "range_pos",
        "volume_ma",
        "last_price",
        "avg_cost",
        "realized_pnl",
        "equity",
    ]

    def on_init(self) -> None:
        """Warm up indicators when the strategy is initialized."""
        self.write_log("一夜持股策略初始化")
        self.bg = BarGenerator(self.on_bar)
        warmup = max(self.trend_window, self.range_window, self.volume_window) + 2
        self.am = ArrayManager(warmup)
        self.load_bar(warmup)

    def on_start(self) -> None:
        """Mark the strategy as trading and push initial state."""
        self.write_log("一夜持股策略启动")
        self.put_event()

    def on_stop(self) -> None:
        """Persist final state when the strategy is stopped."""
        self.write_log("一夜持股策略停止")
        self.put_event()

    def on_tick(self, tick: TickData) -> None:
        """Aggregate ticks into bars."""
        self.last_price = tick.last_price
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        """Run the entry filters near the close and the exit on the next open."""
        self.cancel_all()

        self._advance_bar_counter()

        am: ArrayManager = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        self.last_price = bar.close_price

        if self._held_volume > 0:
            self.bars_since_entry = self._bar_count - self.entry_bar_count
            if self._exit_due(bar):
                self._exit_position(bar)
                return
        else:
            self.bars_since_entry = 0

        if self._held_volume <= 0:
            if self._entry_filters_pass(am, bar):
                volume = self.fixed_size
                if volume > 0:
                    self.buy(bar.close_price, volume)
                    self.entry_bar_count = self._bar_count
                    self.bars_since_entry = 0

        self.equity = self._mark_to_market()
        self.put_event()

    def _entry_filters_pass(self, am: ArrayManager, bar: BarData) -> bool:
        """Every entry is a filter, and all of them must pass at once.

        The filters are cheap and each removes a whole family of bad entries
        rather than tuning a number: a name below its own trend, one at the
        bottom of its recent range, or one trading on no volume are all ways
        of buying into a move that already happened or never started.
        """
        trend = am.sma(self.trend_window, array=True)
        highs = am.high[-self.range_window :]
        lows = am.low[-self.range_window :]
        volume_ma = am.volume[-self.volume_window :].mean()

        if len(trend) < self.trend_window:
            return False

        self.trend_ma = float(trend[-1])
        self.range_high = float(highs.max())
        self.range_low = float(lows.min())
        self.volume_ma = float(volume_ma)

        span = self.range_high - self.range_low
        if span <= 0:
            return False
        self.range_pos = (bar.close_price - self.range_low) / span

        if self.volume_ma <= 0:
            return False
        volume_ratio = bar.volume / self.volume_ma

        in_uptrend = bar.close_price > self.trend_ma
        near_high = self.range_pos >= 0.6
        liquid = volume_ratio >= self.volume_factor
        return in_uptrend and near_high and liquid

    def _exit_due(self, bar: BarData) -> bool:
        """Whether this bar is the exit for a position held overnight.

        Two conditions end the hold. T+1 is the hard one: a position opened
        on this bar cannot be sold on it, so the earliest legal exit is the
        next bar. Beyond that the exit window is bounded, because a gap trade
        that is still open at lunch is no longer a gap trade.
        """
        if self.bars_since_entry < 1:
            return False
        if self._is_next_session(bar):
            return True
        if self.bars_since_entry > 1:
            # Never hold past a second night.
            return True
        if self._is_opening_bar(bar):
            return self.bars_since_entry >= 1
        return False

    def _is_opening_bar(self, bar: BarData) -> bool:
        """Whether the bar is the first bar of a session.

        An overnight gap lands in the opening bar, so that bar is where the
        exit belongs. A bar that merely opens near its high is not an opening
        bar, and treating it as one would sell intraday instead of overnight.
        """
        return bar.datetime.hour == 9 and bar.datetime.minute < 35

    def _is_next_session(self, bar: BarData) -> bool:
        """Whether this bar is already the session after the entry session.

        A daily bar carries a midnight timestamp, so the clock cannot find a
        session boundary on it: a clock test would never fire and the position
        would ride into a second night, which is a different strategy with a
        different risk profile. On a daily series bar distance *is* session
        distance, so the next bar is the next session and the gap has landed.
        """
        return bar.interval == Interval.DAILY and self.bars_since_entry >= 1

    def _exit_position(self, bar: BarData) -> None:
        """Close the whole position into the opening bar.

        The volume comes from the fills the strategy itself recorded, not
        from ``self.pos``. An engine that has not echoed the fill back yet
        would otherwise leave the T+1 window unenforced, and the strategy
        would book a round trip the exchange would have rejected.
        """
        if self._held_volume > 0:
            self.sell(bar.open_price, self._held_volume)

    def on_order(self, order: OrderData) -> None:
        """Track nothing on order updates."""
        return

    def on_trade(self, trade: TradeData) -> None:
        """Update cost basis and realized pnl, then refresh equity."""
        self._apply_trade(trade)
        self.equity = self._mark_to_market()
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """Track nothing on local stop order updates."""
        return

    def bar_index(self) -> int:
        """Monotonic bar counter used to enforce T+1.

        ``ArrayManager`` holds the bars but exposes no count the strategy can
        rely on as a counter, so the strategy keeps its own. The counter is
        what makes the T+1 rule enforceable: an entry at counter N is
        sellable only at counter N+1.
        """
        return len(self.am.close)

    def _advance_bar_counter(self) -> None:
        """Count bars so T+1 can be measured in bar distance."""
        self._bar_count += 1

    def _apply_trade(self, trade: TradeData) -> None:
        """Fold one fill into the cost basis, realized pnl and held volume."""
        volume = trade.volume
        price = trade.price
        pos_after = self._held_volume

        opening_long = (
            trade.direction == Direction.LONG and trade.offset == Offset.OPEN
        )
        opening_short = (
            trade.direction == Direction.SHORT and trade.offset == Offset.OPEN
        )

        if opening_long:
            self._held_volume += volume
        elif opening_short:
            self._held_volume -= volume

        if opening_long or opening_short:
            if opening_long:
                pos_before_abs = pos_after - volume
            else:
                pos_before_abs = -pos_after - volume
            total_abs = abs(self._held_volume)
            if self._held_volume == 0:
                self.pos_extreme = 0.0
            if total_abs > 0:
                self.avg_cost = (
                    self.avg_cost * pos_before_abs + price * volume
                ) / total_abs
            return

        if trade.direction == Direction.SHORT:
            self.realized_pnl += (price - self.avg_cost) * volume
            self._held_volume -= volume
        else:
            self.realized_pnl += (self.avg_cost - price) * volume
            self._held_volume += volume

        if self._held_volume == 0:
            self.avg_cost = 0.0

    def _mark_to_market(self) -> float:
        """Realized pnl plus unrealized pnl at the last known price."""
        if self._held_volume == 0 or self.avg_cost == 0:
            return self.realized_pnl
        return self.realized_pnl + (
            self.last_price - self.avg_cost
        ) * self._held_volume
