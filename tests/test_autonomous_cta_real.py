"""Integration test against the real vnpy_ctastrategy module.

Skipped automatically when vnpy_ctastrategy is not installed, so CI stays
green without the optional app package.
"""

import time

import pytest

from vnpy.autonomous.adapters.cta import CtaStatsAdapter
from vnpy.autonomous.config import AutonomousConfig
from vnpy.autonomous.runner import AutonomousRunner, resolve_app_class
from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Offset
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_TRADE
from vnpy.trader.object import TradeData


vnpy_ctastrategy = pytest.importorskip("vnpy_ctastrategy")


class DemoStrategy(vnpy_ctastrategy.template.CtaTemplate):
    """Minimal strategy used to exercise the real engine."""

    author: str = "autonomous-test"
    parameters: list = ["fixed_size"]
    variables: list = ["fixed_size"]

    fixed_size: int = 1

    def on_init(self) -> None:
        return

    def on_start(self) -> None:
        return

    def on_stop(self) -> None:
        return


def make_trade(orderid: str) -> TradeData:
    """Build a trade whose vt_orderid matches the engine map key."""
    return TradeData(
        gateway_name="FAKE",
        symbol="rb2410",
        exchange=Exchange.SHFE,
        orderid=orderid,
        tradeid="T1",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        price=3000,
        volume=2,
    )


def test_cta_engine_provisioning_and_snapshots() -> None:
    main_engine = MainEngine()
    try:
        app_class = resolve_app_class("CtaStrategy")
        assert app_class is vnpy_ctastrategy.CtaStrategyApp

        engine = main_engine.add_app(app_class)
        engine.init_engine()

        # 引擎离线初始化：数据服务回退到 BaseDatafeed，数据库回退 SQLite
        assert engine.strategies == {}
        assert hasattr(engine, "orderid_strategy_map")

        engine.classes["DemoStrategy"] = DemoStrategy

        runner = AutonomousRunner(
            AutonomousConfig(
                gateways={},
                apps=["CtaStrategy"],
                app_settings={
                    "CtaStrategy": {
                        "strategies": [
                            {
                                "class_name": "DemoStrategy",
                                "name": "demo_trend",
                                "vt_symbol": "rb2410.SHFE",
                                "setting": {"fixed_size": 2},
                            }
                        ]
                    }
                },
            )
        )
        runner.main_engine = main_engine
        runner._strategy_engines = [("CtaStrategy", engine)]
        main_engine.event_engine.register(EVENT_TRADE, runner._process_trade_event)
        runner._provision_strategies("CtaStrategy", engine)

        assert "demo_trend" in engine.strategies
        strategy = engine.strategies["demo_trend"]
        assert isinstance(strategy, DemoStrategy)
        assert strategy.fixed_size == 2

        # 标准初始化序列：init_all_strategies -> inited
        for future in engine.init_all_strategies().values():
            future.result(timeout=10)
        assert strategy.inited is True

        engine.start_all_strategies()
        assert strategy.trading is True

        # 统计适配器读取真实 CtaTemplate 状态
        adapter = CtaStatsAdapter(engine, main_engine.event_engine)
        snapshots = adapter.collect_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].strategy_name == "demo_trend"
        assert snapshots[0].trading is True
        assert snapshots[0].positions == 0

        # 成交通过 orderid_strategy_map 归属到策略
        engine.orderid_strategy_map["FAKE.1"] = strategy
        main_engine.event_engine.put(Event(EVENT_TRADE, make_trade("1")))

        deadline = time.monotonic() + 5
        while runner.ledger.count("demo_trend") == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert runner.ledger.count("demo_trend") == 1

        # 健康熔断走真实引擎的 stop_strategy
        runner.health.set_pause_callback(runner._pause_strategy)
        runner.health.record_trade("demo_trend", -100)
        runner.health.rules.max_consecutive_losses = 1
        runner.health.record_trade("demo_trend", -100)
        assert runner.health.is_halted("demo_trend") is True
        assert strategy.trading is False

        engine.stop_all_strategies()
        assert strategy.trading is False

        # vnpy_sqlite 的 db 对象是模块级单例，释放连接避免影响后续用例
        database = getattr(engine, "database", None)
        if database is not None:
            try:
                database.db.close()
            except Exception:   # noqa: BLE001
                pass
    finally:
        main_engine.close()
