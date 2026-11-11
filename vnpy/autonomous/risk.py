"""Pre-trade risk guard and kill switch for autonomous trading."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_ORDER, EVENT_POSITION, EVENT_TRADE
from vnpy.trader.logger import logger
from vnpy.trader.object import (
    AccountData,
    CancelRequest,
    OrderData,
    OrderRequest,
    PositionData,
    TradeData,
)

from .config import RiskLimits


ACTIVE_STATUSES: frozenset[Status] = frozenset(
    {Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED}
)


@dataclass
class RiskState:
    """Mutable risk state, guarded by a lock in RiskGuard."""

    armed: bool = False
    armed_reason: str = ""
    day_start_balance: float | None = None
    peak_equity: float = 0.0
    current_balance: float = 0.0
    last_account_time: datetime | None = None
    breached: bool = False
    order_count: int = 0
    rejected_count: int = 0


class RiskGuard:
    """Enforce pre-trade limits and daily loss / drawdown circuit breakers.

    The guard wraps ``MainEngine.send_order`` so every order, no matter which
    module sends it, passes the risk checks first.
    """

    def __init__(
        self,
        main_engine: MainEngine,
        limits: RiskLimits,
        notify: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a guard bound to a running MainEngine."""
        self.main_engine: MainEngine = main_engine
        self.limits: RiskLimits = limits
        self.state: RiskState = RiskState()
        self._lock: threading.RLock = threading.RLock()
        self._original_send_order: Callable[[OrderRequest, str], str] | None = None
        self._notify: Callable[[str], None] | None = notify
        self._clock: Callable[[], datetime] = clock or datetime.now

    # ------------------------------------------------------------------ setup

    def install(self) -> None:
        """Wrap MainEngine.send_order with the risk check."""
        if self._original_send_order is not None:
            return

        original: Callable[[OrderRequest, str], str] = self.main_engine.send_order
        self._original_send_order = original

        def guarded_send_order(req: OrderRequest, gateway_name: str) -> str:
            ok, reason = self.check_order(req, gateway_name)
            if not ok:
                with self._lock:
                    self.state.rejected_count += 1
                self._emit(reason)
                return ""
            return original(req, gateway_name)

        self.main_engine.send_order = guarded_send_order    # type: ignore[method-assign]

    def uninstall(self) -> None:
        """Restore the original MainEngine.send_order."""
        original: Callable[[OrderRequest, str], str] | None = self._original_send_order
        if original is not None:
            self.main_engine.send_order = original          # type: ignore[assignment]
            self._original_send_order = None

    def register_events(self, event_engine: EventEngine) -> None:
        """Register order, trade, account and position event handlers."""
        event_engine.register(EVENT_ORDER, self.process_order_event)
        event_engine.register(EVENT_TRADE, self.process_trade_event)
        event_engine.register(EVENT_ACCOUNT, self.process_account_event)
        event_engine.register(EVENT_POSITION, self.process_position_event)

    # ---------------------------------------------------------------- checks

    def is_open_order(self, req: OrderRequest) -> bool:
        """Whether the request opens a new position."""
        return req.offset == Offset.OPEN

    def check_order(self, req: OrderRequest, gateway_name: str) -> tuple[bool, str]:
        """Return (ok, reason) for a pending order request."""
        with self._lock:
            if self.state.armed and self.limits.allow_reduce_when_armed:
                if self.is_open_order(req):
                    return False, f"风控熔断已触发，禁止新开仓：{self.state.armed_reason}"
            elif self.state.armed:
                return False, f"风控熔断已触发，禁止报单：{self.state.armed_reason}"

        limits = self.limits

        if req.volume <= 0:
            return False, "委托数量必须大于0"

        contract = self.main_engine.get_contract(req.vt_symbol)

        if limits.max_order_volume > 0 and req.volume > limits.max_order_volume:
            return (
                False,
                f"单笔委托数量{req.volume}超过上限{limits.max_order_volume}",
            )

        if contract is not None:
            if limits.max_order_volume > 0 and req.volume > (contract.max_volume or req.volume):
                return False, f"单笔委托数量超过合约上限{contract.max_volume}"

            if req.volume < contract.min_volume:
                return False, f"单笔委托数量小于合约最小申报单位{contract.min_volume}"

            notional: float = req.price * req.volume * contract.size
        else:
            notional = req.price * req.volume

        if limits.max_order_value > 0 and notional > limits.max_order_value:
            return (
                False,
                f"单笔委托名义价值{notional:.0f}超过上限{limits.max_order_value:.0f}",
            )

        if limits.max_active_orders > 0:
            active: int = len(self.main_engine.get_all_active_orders())
            if active >= limits.max_active_orders:
                return (
                    False,
                    f"活动委托数量{active}达到上限{limits.max_active_orders}",
                )

        ok_position: bool
        reason_position: str
        ok_position, reason_position = self._check_position(req)
        if not ok_position:
            return False, reason_position

        with self._lock:
            self.state.order_count += 1
        return True, ""

    def _check_position(self, req: OrderRequest) -> tuple[bool, str]:
        """Check the projected net position against the per-symbol cap."""
        limits = self.limits
        cap: float = limits.symbol_position_limits.get(
            req.vt_symbol, limits.max_position_volume
        )
        if cap <= 0:
            return True, ""

        positions: list[PositionData] = self.main_engine.get_all_positions()
        net: float = 0.0
        position: PositionData
        for position in positions:
            if position.vt_symbol != req.vt_symbol:
                continue
            if position.direction == Direction.LONG:
                net += position.volume
            else:
                net -= position.volume

        projected: float = net
        if self.is_open_order(req):
            if req.direction == Direction.LONG:
                projected = net + req.volume
            elif req.direction == Direction.SHORT:
                projected = net - req.volume

        if abs(projected) > cap:
            return (
                False,
                f"{req.vt_symbol} 预计净头寸{projected:g}超过上限{cap:g}",
            )
        return True, ""

    # --------------------------------------------------------------- events

    def process_order_event(self, event: Event) -> None:
        """Track order status for diagnostics."""
        order: OrderData = event.data
        if order.status == Status.REJECTED:
            self._emit(f"委托被拒单：{order.vt_orderid}")

    def process_trade_event(self, event: Event) -> None:
        """Update daily counters on fills."""
        _trade: TradeData = event.data

    def process_account_event(self, event: Event) -> None:
        """Update equity baseline, peak and check daily loss limits."""
        account: AccountData = event.data
        now: datetime = self._clock()
        with self._lock:
            self.state.current_balance = account.balance
            self.state.last_account_time = now
            if self.state.day_start_balance is None:
                self.state.day_start_balance = account.balance
            if account.balance > self.state.peak_equity:
                self.state.peak_equity = account.balance

            baseline: float = self.state.day_start_balance or 0.0
            daily_pnl: float = account.balance - baseline
            drawdown: float = self.state.peak_equity - account.balance

        limits = self.limits
        if limits.daily_loss_limit > 0 and daily_pnl <= -limits.daily_loss_limit:
            self._breach(f"当日亏损{daily_pnl:.2f}达到上限{limits.daily_loss_limit:.2f}")
            return

        if limits.max_drawdown > 0 and drawdown >= limits.max_drawdown:
            self._breach(
                f"权益回撤{drawdown:.2f}达到上限{limits.max_drawdown:.2f}"
            )

    def process_position_event(self, event: Event) -> None:
        """Position updates are consumed through OmsEngine queries."""
        _position: PositionData = event.data

    # ------------------------------------------------------------ controls

    def reset_daily(self) -> None:
        """Reset daily counters at the start of a trading session."""
        with self._lock:
            self.state.day_start_balance = self.state.current_balance or None
            self.state.peak_equity = self.state.current_balance
            self.state.breached = False
            self._emit("风控日内计数器已重置")

    @property
    def armed(self) -> bool:
        """Whether the kill switch blocks new entries."""
        with self._lock:
            return self.state.armed

    def disarm(self) -> None:
        """Manually release the kill switch."""
        with self._lock:
            self.state.armed = False
            self.state.armed_reason = ""
        self._emit("风控熔断已手动解除")

    def arm(self, reason: str) -> None:
        """Manually trigger the kill switch."""
        self._breach(reason)

    def _breach(self, reason: str) -> None:
        """Trigger the circuit breaker and optionally flatten positions."""
        with self._lock:
            if self.state.armed:
                return
            self.state.armed = True
            self.state.breached = True
            self.state.armed_reason = reason

        self._emit(f"风控熔断触发：{reason}")

        if self.limits.kill_switch_on_breach:
            self.flatten_positions()

    def flatten_positions(self) -> None:
        """Cancel active orders and close all open positions."""
        self.cancel_all_active_orders()

        positions: list[PositionData] = self.main_engine.get_all_positions()
        position: PositionData
        for position in positions:
            if position.volume <= 0:
                continue

            if position.direction == Direction.LONG:
                close_direction: Direction = Direction.SHORT
            else:
                close_direction = Direction.LONG

            req: OrderRequest = OrderRequest(
                symbol=position.symbol,
                exchange=position.exchange,
                direction=close_direction,
                type=OrderType.MARKET,
                volume=position.volume,
                price=0,
                offset=Offset.CLOSE,
                reference="risk_flatten",
            )
            original: Callable[[OrderRequest, str], str] | None = self._original_send_order
            if original is not None:
                original(req, position.gateway_name)

    def cancel_all_active_orders(self) -> None:
        """Cancel every active order through the main engine."""
        orders: list[OrderData] = self.main_engine.get_all_active_orders()
        order: OrderData
        for order in orders:
            req: CancelRequest = CancelRequest(
                orderid=order.orderid,
                symbol=order.symbol,
                exchange=order.exchange,
            )
            self.main_engine.cancel_order(req, order.gateway_name)

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable risk state snapshot."""
        with self._lock:
            state: dict[str, object] = {
                "armed": self.state.armed,
                "breached": self.state.breached,
                "armed_reason": self.state.armed_reason,
                "day_start_balance": self.state.day_start_balance,
                "current_balance": self.state.current_balance,
                "peak_equity": self.state.peak_equity,
                "order_count": self.state.order_count,
                "rejected_count": self.state.rejected_count,
                "last_account_time": (
                    self.state.last_account_time.isoformat()
                    if self.state.last_account_time
                    else None
                ),
            }
        return state

    # ------------------------------------------------------------- internal

    def _emit(self, message: str) -> None:
        """Log and optionally forward a guard event."""
        logger.warning(f"[风控] {message}")
        notify: Callable[[str], None] | None = self._notify
        if notify is not None:
            notify(message)
