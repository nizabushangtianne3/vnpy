"""Reference CTA strategy for the autonomous trading runner.

Copy this file into ``.vntrader/strategies/`` so the CtaEngine class scanner
picks it up, then reference ``AutonomousTrendStrategy`` from the
``app_settings.CtaStrategy.strategies`` list of your autonomous config.

The strategy is a dual moving-average trend follower with two features the
autonomous layer needs:

* ``realized_pnl`` and ``equity`` are published through ``variables`` so the
  CtaStatsAdapter feeds per-strategy pnl into the health monitor and the
  capital allocator (snapshot driven circuit breaker and sizing).
* A position cap and a bar-based ATR stop keep a single strategy within the
  limits the risk guard expects, even before the guard sees the order.
"""

import numpy as np

from vnpy.trader.constant import Direction, Offset
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


class AutonomousTrendStrategy(CtaTemplate):
    """Dual MA trend strategy with ATR stop and autonomous-loop reporting."""

    author: str = "VeighNa Autonomous"

    fast_window: int = 5
    slow_window: int = 20
    atr_window: int = 10
    stop_atr: float = 2.0
    fixed_size: int = 1
    max_position: int = 5

    fast_ma0: float = 0.0
    fast_ma1: float = 0.0
    slow_ma0: float = 0.0
    slow_ma1: float = 0.0
    atr_value: float = 0.0
    avg_cost: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0
    equity: float = 0.0

    parameters = [
        "fast_window",
        "slow_window",
        "atr_window",
        "stop_atr",
        "fixed_size",
        "max_position",
    ]
    variables = [
        "fast_ma0",
        "slow_ma0",
        "atr_value",
        "avg_cost",
        "last_price",
        "realized_pnl",
        "equity",
    ]

    def on_init(self) -> None:
        """Warm up indicators when the strategy is initialized."""
        self.write_log("策略初始化")

        self.bg: BarGenerator = BarGenerator(self.on_bar)
        warmup: int = max(self.slow_window, self.atr_window) + 2
        self.am: ArrayManager = ArrayManager(warmup)
        self.load_bar(warmup)

    def on_start(self) -> None:
        """Mark the strategy as trading and push initial state."""
        self.write_log("策略启动")
        self.put_event()

    def on_stop(self) -> None:
        """Persist final state when the strategy is stopped."""
        self.write_log("策略停止")
        self.put_event()

    def on_tick(self, tick: TickData) -> None:
        """Aggregate ticks into bars."""
        self.last_price = tick.last_price
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        """Trade moving-average crosses and enforce the ATR stop."""
        self.cancel_all()

        am: ArrayManager = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        self.last_price = bar.close_price

        fast_ma: np.ndarray = am.sma(self.fast_window, array=True)
        slow_ma: np.ndarray = am.sma(self.slow_window, array=True)
        atr_value: np.ndarray = am.atr(self.atr_window, array=True)

        self.fast_ma0 = float(fast_ma[-1])
        self.fast_ma1 = float(fast_ma[-2])
        self.slow_ma0 = float(slow_ma[-1])
        self.slow_ma1 = float(slow_ma[-2])
        self.atr_value = float(atr_value[-1])

        cross_over: bool = (
            self.fast_ma0 > self.slow_ma0 and self.fast_ma1 <= self.slow_ma1
        )
        cross_below: bool = (
            self.fast_ma0 < self.slow_ma0 and self.fast_ma1 >= self.slow_ma1
        )

        # ATR bar stop first: protecting capital outranks new signals.
        # A live stop is a resting order: it fills when the bar trades
        # through the stop, not when the bar closes below it. Judging on the
        # close alone skips every stop that recovered within the bar, which
        # hides exactly the drawdowns the stop exists to cut.
        if self.pos > 0 and self.avg_cost > 0:
            stop_price: float = self.avg_cost - self.stop_atr * self.atr_value
            if bar.low_price <= stop_price:
                self.sell(max(stop_price, bar.open_price), self.pos)
                self.put_event()
                return
        if self.pos < 0 and self.avg_cost > 0:
            stop_price = self.avg_cost + self.stop_atr * self.atr_value
            if bar.high_price >= stop_price:
                self.cover(min(stop_price, bar.open_price), -self.pos)
                self.put_event()
                return

        # A confirmed cross flips the position outright; while the trend keeps
        # running the strategy keeps adding up to the cap, because a trend
        # follower earns on scale, not on a single lot. Gating on the cross
        # alone pins the position at one lot and makes max_position dead code.
        if cross_over:
            if self.pos < self.max_position:
                if self.pos < 0:
                    self.cover(bar.close_price, -self.pos)
                self.buy(bar.close_price, self._open_volume(1))
        elif cross_below:
            if self.pos > -self.max_position:
                if self.pos > 0:
                    self.sell(bar.close_price, self.pos)
                self.short(bar.close_price, self._open_volume(-1))
        elif self.fast_ma0 > self.slow_ma0 and 0 < self.pos < self.max_position:
            self.buy(bar.close_price, self._open_volume(1))
        elif self.fast_ma0 < self.slow_ma0 and -self.max_position < self.pos < 0:
            self.short(bar.close_price, self._open_volume(-1))

        self.equity = self._mark_to_market()
        self.put_event()

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

    def _open_volume(self, direction: int) -> float:
        """Volume that keeps the projected net position inside the cap."""
        room: float
        if direction > 0:
            room = float(self.max_position) - max(self.pos, 0.0)
        else:
            room = float(self.max_position) + min(self.pos, 0.0)
        return max(min(float(self.fixed_size), room), 0.0)

    def _apply_trade(self, trade: TradeData) -> None:
        """Fold one fill into the cost basis and realized pnl."""
        volume: float = trade.volume
        price: float = trade.price
        pos_after: float = self.pos

        opening_long: bool = (
            trade.direction == Direction.LONG and trade.offset == Offset.OPEN
        )
        opening_short: bool = (
            trade.direction == Direction.SHORT and trade.offset == Offset.OPEN
        )

        if opening_long or opening_short:
            if opening_long:
                pos_before_abs: float = pos_after - volume
            else:
                pos_before_abs = -pos_after - volume
            total_abs: float = abs(pos_after)
            if total_abs > 0:
                self.avg_cost = (
                    self.avg_cost * pos_before_abs + price * volume
                ) / total_abs
            return

        # Reducing trades realize pnl at the tracked cost basis, converted to
        # money with the contract multiplier.
        size: int = self._contract_size()
        if trade.direction == Direction.SHORT:
            self.realized_pnl += (price - self.avg_cost) * volume * size
        else:
            self.realized_pnl += (self.avg_cost - price) * volume * size

        if pos_after == 0:
            self.avg_cost = 0.0

    def _mark_to_market(self) -> float:
        """Realized pnl plus unrealized pnl at the last known price.

        Both are money, not ticks: the contract multiplier turns a price move
        into cash. Health rules and the capital allocator read ``equity`` as a
        balance, so an unscaled value would size positions off a number that is
        off by the size factor.
        """
        if self.pos == 0 or self.avg_cost == 0:
            return self.realized_pnl

        size: int = self._contract_size()
        return self.realized_pnl + (
            self.last_price - self.avg_cost
        ) * self.pos * size

    def _contract_size(self) -> int:
        """Contract multiplier, 1 when the engine cannot provide it."""
        try:
            size: int = int(self.get_size())
        except Exception:
            size = 1
        return size or 1
