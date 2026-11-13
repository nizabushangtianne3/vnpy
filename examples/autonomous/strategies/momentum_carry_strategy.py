"""Time-series momentum with an ATR stop, built for A-share daily bars.

This replaces the overnight-hold idea, which the real-data campaign graded
``retire``: on 77 A-shares with 5 years of bars the sample-out folds lost on
two of three, and the one profitable fold carried 7456 of unrealized profit
against -6220 realized. The lesson kept from that failure is not "avoid
overnight", it is "do not build an edge on a price move you cannot size or
stop". The edge chased here is one of the most replicated findings in the
literature - an instrument's own past return continues for months - combined
with the one piece of machinery the autonomous layer already trusts, the ATR
stop.

The rules are deliberately few, and each is a filter rather than a forecast:

* Trend. Enter long only when the return over ``lookback`` bars is positive
  and the close is above its own moving average. An instrument falling against
  its own history is not a thing to be long, whatever the day's bar says.
* Volatility gate. Skip an entry whose ATR as a fraction of price has blown
  past ``vol_ceiling``. A trend in a market that moved three times its normal
  range is a market that is repricing, not trending, and the stop cannot be
  sized sensibly in it.
* ATR stop. The stop sits a multiple of ATR below the entry, and it is the
  only protection the position has - so it is set at the entry and never
  loosened.
* T+1. Shares bought today cannot be sold today, so a position is sellable
  only from the bar after the one that filled, and the volume tracked comes
  from the strategy's own fill bookkeeping. Nothing here pretends the
  exchange would accept a same-day round trip.

Position size is capped and one entry at a time: a strategy that adds into a
loser without a rule is a martingale, and the risk guard already sizes
positions properly. This adds no leverage to its decisions.
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
from vnpy.trader.constant import Direction, Offset


class MomentumCarryStrategy(CtaTemplate):
    """Long-only time-series momentum with a fixed ATR stop."""

    author = "VeighNa Autonomous"

    lookback: int = 60
    trend_window: int = 20
    atr_window: int = 20
    stop_atr: float = 2.5
    vol_ceiling: float = 0.06
    fixed_size: int = 100
    max_position: int = 100

    trend_strength: float = 0.0
    trend_ma: float = 0.0
    atr_value: float = 0.0
    vol_ratio: float = 0.0
    stop_price: float = 0.0
    last_price: float = 0.0
    entry_bar_count: int = -1
    bars_since_entry: int = 0
    _bar_count: int = 0
    _held_volume: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0
    equity: float = 0.0

    parameters = [
        "lookback",
        "trend_window",
        "atr_window",
        "stop_atr",
        "vol_ceiling",
        "fixed_size",
        "max_position",
    ]
    variables = [
        "trend_strength",
        "atr_value",
        "vol_ratio",
        "stop_price",
        "avg_cost",
        "realized_pnl",
        "equity",
    ]

    def on_init(self) -> None:
        """Warm up indicators when the strategy is initialized."""
        self.write_log("时序动量策略初始化")
        self.bg = BarGenerator(self.on_bar)
        warmup = max(self.lookback, self.trend_window, self.atr_window) + 2
        self.am = ArrayManager(warmup)
        self.load_bar(warmup)

    def on_start(self) -> None:
        """Mark the strategy as trading and push initial state."""
        self.write_log("时序动量策略启动")
        self.put_event()

    def on_stop(self) -> None:
        """Persist final state when the strategy is stopped."""
        self.write_log("时序动量策略停止")
        self.put_event()

    def on_tick(self, tick: TickData) -> None:
        """Aggregate ticks into bars."""
        self.last_price = tick.last_price
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        """Apply the entry filters, then the stop and the T+1 exit."""
        self.cancel_all()
        self._advance_bar_counter()

        am: ArrayManager = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        self.last_price = bar.close_price

        if self._held_volume > 0:
            self.bars_since_entry = self._bar_count - self.entry_bar_count
            self.trend_strength, self.trend_ma = self._trend_state(am)
            self.atr_value = float(am.atr(self.atr_window, array=True)[-1])
            if self.atr_value > 0 and self.last_price > 0:
                self.vol_ratio = self.atr_value / self.last_price
            if self._stop_hit(bar):
                self._exit_position(bar)
                return
            if self.bars_since_entry >= 1 and self._trend_broken(am, bar):
                self._exit_position(bar)
                return
        else:
            self.bars_since_entry = 0

        if self._held_volume <= 0:
            if self._entries_pass(am, bar):
                self.buy(bar.close_price, self.fixed_size)
                self.entry_bar_count = self._bar_count
                self.bars_since_entry = 0
                self.stop_price = self._entry_stop(am, bar)

        self.equity = self._mark_to_market()
        self.put_event()

    # ------------------------------------------------------------- indicators

    def _trend_state(self, am: ArrayManager) -> tuple[float, float]:
        """Return the lookback return and its average, as the trend reads it."""
        roc = am.roc(self.lookback, array=True)
        ma = am.sma(self.trend_window, array=True)
        if not len(roc) or not len(ma):
            return 0.0, 0.0
        return float(roc[-1]), float(ma[-1])

    def _entries_pass(self, am: ArrayManager, bar: BarData) -> bool:
        """Whether this bar is a legitimate entry under all three filters."""
        strength, ma_value = self._trend_state(am)
        self.trend_strength = strength
        self.trend_ma = ma_value
        self.atr_value = float(am.atr(self.atr_window, array=True)[-1])

        if self.atr_value <= 0 or bar.close_price <= 0:
            return False
        self.vol_ratio = self.atr_value / bar.close_price

        trending = strength > 0 and bar.close_price > ma_value
        calm = self.vol_ratio <= self.vol_ceiling
        return trending and calm

    def _entry_stop(self, am: ArrayManager, bar: BarData) -> float:
        """Where the stop sits for a position opened on this bar."""
        return bar.close_price - self.stop_atr * self.atr_value

    def _trend_broken(self, am: ArrayManager, bar: BarData) -> bool:
        """Whether the edge that opened the position has left it."""
        strength, ma_value = self._trend_state(am)
        return strength <= 0 or bar.close_price < ma_value

    def _stop_hit(self, bar: BarData) -> bool:
        """Whether this bar traded through the resting stop.

        The stop is the position's only protection, so it is checked against
        the bar's low rather than its close: a bar whose low crossed the stop
        and whose close recovered is still a stopped-out bar, because that is
        the order the exchange would have filled.
        """
        if self.stop_price <= 0:
            return False
        return bar.low_price <= self.stop_price

    def _exit_position(self, bar: BarData) -> None:
        """Close the whole position, at the stop when the stop is what hit."""
        if self._held_volume <= 0:
            return
        price: float = bar.open_price
        if self.stop_price > 0 and bar.open_price < self.stop_price:
            # The bar opened below the stop, so the stop is the fill.
            price = min(self.stop_price, bar.open_price)
        self.sell(price, self._held_volume)

    # --------------------------------------------------------------- plumbing

    def on_order(self, order: OrderData) -> None:
        """Track nothing on order updates."""
        return

    def on_trade(self, trade: TradeData) -> None:
        """Fold one fill into the cost basis, realized pnl and held volume."""
        self._apply_trade(trade)
        self.equity = self._mark_to_market()
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """Track nothing on local stop order updates."""
        return

    def bar_index(self) -> int:
        """Monotonic bar counter, owned by the strategy for the same reason."""
        return self._bar_count

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
            total_abs = abs(self._held_volume)
            if total_abs > 0:
                self.avg_cost = (self.avg_cost * abs(pos_after) + price * volume) / total_abs
            return

        if trade.direction == Direction.SHORT:
            self.realized_pnl += (price - self.avg_cost) * volume
            self._held_volume -= volume
        else:
            self.realized_pnl += (self.avg_cost - price) * volume
            self._held_volume += volume

        if self._held_volume == 0:
            self.avg_cost = 0.0
            self.stop_price = 0.0

    def _mark_to_market(self) -> float:
        """Realized pnl plus unrealized pnl at the last known price."""
        if self._held_volume == 0 or self.avg_cost == 0:
            return self.realized_pnl
        return self.realized_pnl + (self.last_price - self.avg_cost) * self._held_volume
