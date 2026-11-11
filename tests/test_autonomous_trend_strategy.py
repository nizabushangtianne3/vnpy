"""Integration test for the autonomous reference trend strategy.

The strategy file under examples/ runs through the real CtaEngine and a fake
gateway: warmup, cross entry, fill accounting, ATR stop exit, and the
variables the autonomous adapter consumes.
"""

import importlib.util
import time
from datetime import datetime, timedelta
from pathlib import Path


from vnpy.autonomous.adapters.cta import CtaStatsAdapter
from vnpy.event import Event
from vnpy.trader.constant import (
    Direction,
    Exchange,
    Interval,
    Offset,
    Product,
)
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_TRADE
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    BarData,
    CancelRequest,
    ContractData,
    OrderRequest,
    SubscribeRequest,
    TradeData,
)


REPO_ROOT: Path = Path(__file__).resolve().parents[1]
BASE_TIME: datetime = datetime(2026, 11, 11, 9, 0)
STRATEGY_FILE: Path = (
    REPO_ROOT / "examples" / "autonomous" / "strategies" / "autonomous_trend_strategy.py"
)


def load_strategy_class():
    """Load the strategy class straight from the examples file."""
    spec = importlib.util.spec_from_file_location(
        "autonomous_trend_strategy", STRATEGY_FILE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AutonomousTrendStrategy


class FakeGateway(BaseGateway):
    default_name: str = "FAKE"
    exchanges: list[Exchange] = [Exchange.SHFE]

    def __init__(self, event_engine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        self.orders: list[OrderRequest] = []

    def connect(self, setting) -> None:
        return

    def close(self) -> None:
        return

    def subscribe(self, req: SubscribeRequest) -> None:
        return

    def send_order(self, req: OrderRequest) -> str:
        self.orders.append(req)
        return f"{self.gateway_name}.{len(self.orders)}"

    def cancel_order(self, req: CancelRequest) -> None:
        return

    def query_account(self) -> None:
        return

    def query_position(self) -> None:
        return


def make_bar(price: float, minute: int) -> BarData:
    return BarData(
        gateway_name="FAKE",
        symbol="rb2410",
        exchange=Exchange.SHFE,
        datetime=BASE_TIME + timedelta(minutes=minute),
        interval=Interval.MINUTE,
        volume=100,
        open_price=price,
        high_price=price + 2,
        low_price=price - 2,
        close_price=price,
    )


def rising_bars(start: float, step: float, count: int, minute0: int = 0):
    return [make_bar(start + index * step, minute0 + index) for index in range(count)]


def wait_until(ready, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.02)
    raise AssertionError("等待条件超时")


def test_trend_strategy_full_cycle_with_real_engine() -> None:
    strategy_class = load_strategy_class()
    main_engine = MainEngine()
    try:
        gateway = main_engine.add_gateway(FakeGateway)
        assert isinstance(gateway, FakeGateway)

        contract = ContractData(
            gateway_name="FAKE",
            symbol="rb2410",
            exchange=Exchange.SHFE,
            name="螺纹钢",
            product=Product.FUTURES,
            size=10,
            pricetick=1,
            min_volume=1,
            max_volume=100,
        )
        main_engine.event_engine.put(Event(EVENT_CONTRACT, contract))
        wait_until(lambda: main_engine.get_contract("rb2410.SHFE") is not None)

        from vnpy_ctastrategy import CtaStrategyApp

        engine = main_engine.add_app(CtaStrategyApp)
        engine.init_engine()
        engine.classes["AutonomousTrendStrategy"] = strategy_class

        engine.add_strategy(
            "AutonomousTrendStrategy",
            "auto_trend",
            "rb2410.SHFE",
            {
                "fast_window": 5,
                "slow_window": 20,
                "atr_window": 10,
                "stop_atr": 2,
                "fixed_size": 2,
                "max_position": 5,
            },
        )
        for future in engine.init_all_strategies().values():
            future.result(timeout=10)

        strategy = engine.strategies["auto_trend"]
        assert strategy.inited is True
        engine.start_all_strategies()
        assert strategy.trading is True

        # 22 根横盘 K 线完成指标预热
        for bar in rising_bars(100, 0, 22):
            strategy.on_bar(bar)
        assert strategy.am.inited is True
        assert gateway.orders == []

        # 急涨触发金叉开多，仓位受固定手数约束
        for bar in rising_bars(100, 2, 6, minute0=30):
            strategy.on_bar(bar)

        assert len(gateway.orders) >= 1
        open_order = gateway.orders[0]
        assert open_order.direction == Direction.LONG
        assert open_order.offset == Offset.OPEN
        assert open_order.volume == 2

        # 模拟成交：仓位、成本、盈亏变量随之更新
        trade = TradeData(
            gateway_name="FAKE",
            symbol="rb2410",
            exchange=Exchange.SHFE,
            orderid=str(len(gateway.orders)),
            tradeid="T1",
            direction=Direction.LONG,
            offset=Offset.OPEN,
            price=110,
            volume=2,
        )
        main_engine.event_engine.put(Event(EVENT_TRADE, trade))
        wait_until(lambda: strategy.pos == 2)

        assert strategy.avg_cost == 110
        assert "realized_pnl" in strategy.variables
        assert "equity" in strategy.variables

        # 统计适配器读取真实变量喂给自治层
        adapter = CtaStatsAdapter(engine, main_engine.event_engine)
        snapshot = adapter.collect_snapshots()[0]
        assert snapshot.strategy_name == "auto_trend"
        assert snapshot.trading is True
        assert snapshot.positions == 1

        # 急跌触发 ATR 止损全平
        orders_before = len(gateway.orders)
        for bar in rising_bars(110, -6, 5, minute0=36):
            strategy.on_bar(bar)

        closing = [
            order
            for order in gateway.orders[orders_before:]
            if order.offset in (Offset.CLOSE, Offset.CLOSETODAY)
        ]
        assert len(closing) == 1
        assert closing[0].volume == 2

        # 平仓成交计入已实现盈亏
        close_trade = TradeData(
            gateway_name="FAKE",
            symbol="rb2410",
            exchange=Exchange.SHFE,
            orderid=str(len(gateway.orders)),
            tradeid="T2",
            direction=Direction.SHORT,
            offset=Offset.CLOSETODAY,
            price=80,
            volume=2,
        )
        main_engine.event_engine.put(Event(EVENT_TRADE, close_trade))
        wait_until(lambda: strategy.realized_pnl < 0)

        assert strategy.pos == 0
        assert strategy.realized_pnl == (80 - 110) * 2
        assert strategy.avg_cost == 0

        engine.stop_all_strategies()
        engine.database.db.close()
    finally:
        main_engine.close()
