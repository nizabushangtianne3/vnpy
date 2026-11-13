"""Pre-trade risk guard and kill switch for autonomous trading."""

from __future__ import annotations

import threading
from typing import Any
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_ORDER, EVENT_POSITION, EVENT_TRADE
from vnpy.trader.logger import logger
from vnpy.trader.object import (
    AccountData,
    CancelRequest,
    ContractData,
    OrderData,
    OrderRequest,
    PositionData,
    TradeData,
)
from vnpy.trader.utility import round_to

from .config import RiskLimits


# A-share exchanges trade T+1: shares bought today cannot be sold until the
# next session. Futures and options have no such rule, so a guard that fired
# on every exchange would reject legitimate closes of a futures position.
T_PLUS_ONE_EXCHANGES: frozenset[str] = frozenset({"SSE", "SZSE", "BSE"})


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
        self._bought_today: dict[str, float] = {}
        self._filled_day: date | None = None

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

        ok_t1: bool
        reason_t1: str
        ok_t1, reason_t1 = self._check_t_plus_one(req)
        if not ok_t1:
            return False, reason_t1

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

    def _check_t_plus_one(self, req: OrderRequest) -> tuple[bool, str]:
        """Refuse to sell A-shares bought on today's session.

        The reference strategy enforces T+1 against its own fills, which is the
        only enforcement the backtest has. Live trading needs a second one,
        because the paper account and most stock gateways do not know the rule
        and would happily accept an order the exchange rejects the next
        morning -- at which point the position is real and unsellable. This
        guard is that second enforcement, and it keys off the fills the guard
        itself saw so it holds even when a strategy miscounts.
        """
        if not self.limits.enforce_t_plus_one:
            return True, ""
        if req.direction != Direction.SHORT or req.offset != Offset.CLOSE:
            return True, ""
        if not self._is_stock_exchange(req.vt_symbol):
            return True, ""

        with self._lock:
            self._roll_over_filled_day()
            bought: float = self._bought_today.get(req.vt_symbol, 0.0)

        if bought > 0:
            return (
                False,
                f"{req.vt_symbol} T+1 限制：当日买入 {bought:g} 股不能当日卖出",
            )
        return True, ""

    def _is_stock_exchange(self, vt_symbol: str) -> bool:
        """Whether this vt_symbol trades on a T+1 stock exchange."""
        if "." not in vt_symbol:
            return False
        exchange_name: str = vt_symbol.split(".")[-1].upper()
        return exchange_name in T_PLUS_ONE_EXCHANGES

    def _roll_over_filled_day(self) -> None:
        """Clear today's fills once the session date changes.

        Called with the lock held. Yesterday's buys became sellable at this
        session's close, so keeping them would freeze a position that is in
        fact free.
        """
        today: date = self._clock().date()
        if self._filled_day != today:
            self._filled_day = today
            self._bought_today.clear()

    # --------------------------------------------------------------- events

    def process_order_event(self, event: Event) -> None:
        """Track order status for diagnostics."""
        order: OrderData = event.data
        if order.status == Status.REJECTED:
            self._emit(f"委托被拒单：{order.vt_orderid}")

    def process_trade_event(self, event: Event) -> None:
        """Record today's buys so the next session's sells can be checked."""
        trade: TradeData = event.data
        with self._lock:
            self._roll_over_filled_day()
            if trade.direction == Direction.LONG and trade.offset == Offset.OPEN:
                self._bought_today[trade.vt_symbol] = (
                    self._bought_today.get(trade.vt_symbol, 0.0) + trade.volume
                )

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

    def flatten_positions(self, strategy_name: str | None = None) -> None:
        """Cancel active orders and close all open positions."""
        self.cancel_all_active_orders()

        positions: list[PositionData] = self.main_engine.get_all_positions()
        position: PositionData
        for position in positions:
            if position.volume <= 0:
                continue

            if strategy_name is not None:
                if not self._owns_position(position, strategy_name):
                    continue

            if position.direction == Direction.LONG:
                close_direction: Direction = Direction.SHORT
            else:
                close_direction = Direction.LONG

            contract: ContractData | None = self.main_engine.get_contract(
                position.vt_symbol
            )
            price: float = self._flatten_price(position, contract)

            original_req: OrderRequest = OrderRequest(
                symbol=position.symbol,
                exchange=position.exchange,
                direction=close_direction,
                type=OrderType.LIMIT,
                volume=position.volume,
                price=price,
                offset=Offset.CLOSE,
                reference="risk_flatten",
            )

            if strategy_name is not None:
                original_req.reference = f"CtaStrategy_{strategy_name}"

            # The offset conversion happens here, not inside send_order.
            # SHFE/INE require splitting a close into close-today and
            # close-yesterday before each request reaches the gateway.
            oms_engine: object | None = self.main_engine.get_engine("oms")
            convert: Any = getattr(oms_engine, "convert_order_request", None)
            if callable(convert):
                req_list: list[OrderRequest] = convert(
                    original_req, position.gateway_name, False, False
                )
            else:
                req_list = [original_req]
            req: OrderRequest
            for req in req_list:
                # Reduce-only close orders must not be blocked by the
                # breaker itself when allow_reduce_when_armed is False.
                ok, reason = self.check_order(req, position.gateway_name)
                if not ok:
                    logger.warning(
                        "[风控] 熔断平仓委托被拦截：%s", reason
                    )
                    continue
                vt_orderid: str = self.main_engine.send_order(
                    req, position.gateway_name
                )
                if not vt_orderid:
                    continue
                self.main_engine.update_order_request(
                    req, vt_orderid, position.gateway_name
                )

    def _owns_position(self, position: PositionData, strategy_name: str) -> bool:
        """Whether a strategy owns this position through its CTA engine.

        ``PositionData`` carries no owning strategy, so the guard resolves
        ownership the way the CTA engine does: the strategy must trade the
        same contract and hold volume in the same direction. Without this
        link a per-strategy halt can only flatten the whole account, which
        would punish unrelated strategies for one strategy's breach.
        """
        engine: Any = self._cta_engine()
        if engine is None:
            return False

        strategies: Any = getattr(engine, "symbol_strategy_map", None)
        if not isinstance(strategies, dict):
            return False

        candidates: Any = strategies.get(position.vt_symbol)
        if not candidates:
            return False

        strategy: Any
        for strategy in candidates:
            name: Any = getattr(strategy, "strategy_name", None)
            if name != strategy_name:
                continue
            pos_value: Any = getattr(strategy, "pos", 0.0)
            try:
                pos: float = float(pos_value)
            except (TypeError, ValueError):
                pos = 0.0
            if position.direction == Direction.LONG:
                return pos > 0
            return pos < 0
        return False

    def _cta_engine(self) -> Any:
        """Return the CTA strategy engine registered on the main engine."""
        get_engine: Any = getattr(self.main_engine, "get_engine", None)
        if not callable(get_engine):
            return None
        try:
            return get_engine("cta")
        except Exception:   # noqa: BLE001
            return None

    def _flatten_price(
        self, position: PositionData, contract: ContractData | None
    ) -> float:
        """Return a marketable limit price for an urgent close.

        Uses the opposite side of the book (crossing the spread) so the
        order fills immediately like a market order, but remains legal on
        SHFE/INE which reject true market orders. Falls back to last price
        or the recorded position price when no tick is available.
        """
        if contract is not None and contract.pricetick > 0:
            tick = self.main_engine.get_tick(position.vt_symbol)
            if tick is not None:
                if position.direction == Direction.LONG:
                    base_price: float = tick.bid_price_1 or tick.last_price
                else:
                    base_price = tick.ask_price_1 or tick.last_price
                if base_price > 0:
                    return round_to(base_price, contract.pricetick)

            base_price = position.price
            if base_price > 0:
                return round_to(base_price, contract.pricetick)

        return position.price

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

    def restore(self, payload: dict[str, object]) -> None:
        """Restore persisted risk counters, e.g. after a child restart.

        Only accepted when the snapshot belongs to the same trading day:
        a stale payload from yesterday must not seed today's baseline or
        keep a stale kill switch armed.
        """
        session_day: str = str(payload.get("session_day", ""))
        if not session_day:
            return
        if self._clock().date().isoformat() != session_day:
            return

        with self._lock:
            armed = bool(payload.get("armed"))
            day_start_balance = payload.get("day_start_balance")
            peak_equity = payload.get("peak_equity")
            current_balance = payload.get("current_balance")

            self.state.armed = armed
            self.state.armed_reason = (
                str(payload.get("armed_reason", "")) if armed else ""
            )
            self.state.breached = bool(payload.get("breached"))
            if isinstance(day_start_balance, (int, float)):
                self.state.day_start_balance = float(day_start_balance)
            if isinstance(peak_equity, (int, float)):
                self.state.peak_equity = float(peak_equity)
            if isinstance(current_balance, (int, float)):
                self.state.current_balance = float(current_balance)

    # ------------------------------------------------------------- internal

    def _emit(self, message: str) -> None:
        """Log and optionally forward a guard event."""
        logger.warning(f"[风控] {message}")
        notify: Callable[[str], None] | None = self._notify
        if notify is not None:
            notify(message)
