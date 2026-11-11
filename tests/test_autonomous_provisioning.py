import threading
import time
from pathlib import Path

from vnpy.autonomous.config import (
    AutonomousConfig,
    RiskLimits,
    RuntimeConfig,
    save_config,
)
from vnpy.autonomous.runner import AutonomousRunner, read_state
from vnpy.event import EventEngine
from vnpy.trader.app import BaseApp
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.constant import Exchange
from vnpy.trader.object import CancelRequest, OrderRequest, SubscribeRequest


GATEWAY_NAME: str = f"{__name__}:FakeGateway"
APP_NAME: str = f"{__name__}:FakeStrategyApp"


class FakeGateway(BaseGateway):
    default_name: str = "FAKE"
    exchanges: list[Exchange] = [Exchange.SHFE]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        self.orders: list[OrderRequest] = []

    def connect(self, setting: dict[str, str | int | float | bool]) -> None:
        return

    def close(self) -> None:
        return

    def subscribe(self, req: SubscribeRequest) -> None:
        return

    def send_order(self, req: OrderRequest) -> str:
        self.orders.append(req)
        return f"{self.gateway_name}.1"

    def cancel_order(self, req: CancelRequest) -> None:
        return

    def query_account(self) -> None:
        return

    def query_position(self) -> None:
        return


class FakeStrategyEngine(BaseEngine):
    """CTA-like engine supporting init_strategy provisioning."""

    def __init__(
        self,
        main_engine: MainEngine,
        event_engine: EventEngine,
        engine_name: str = "",
    ) -> None:
        super().__init__(main_engine, event_engine, engine_name)
        self.strategies: dict[str, object] = {}
        self.inited_engine: bool = False
        self.inited_all: bool = False
        self.started_all: bool = False
        self.stopped_all: bool = False
        self.provisioned: list[tuple[str, str, str, dict]] = []
        self.orderid_strategy_map: dict[str, object] = {}
        self.paused: list[str] = []
        self.resumed: list[str] = []

    def init_engine(self) -> None:
        self.inited_engine = True

    def init_all_strategies(self) -> None:
        self.inited_all = True

    def start_all_strategies(self) -> None:
        self.started_all = True

    def stop_all_strategies(self) -> None:
        self.stopped_all = True

    def add_strategy(
        self, class_name: str, strategy_name: str, vt_symbol: str, setting: dict
    ) -> None:
        self.provisioned.append(
            (class_name, strategy_name, vt_symbol, dict(setting))
        )
        self.strategies[strategy_name] = {"vt_symbol": vt_symbol}

    def stop_strategy(self, strategy_name: str) -> None:
        self.paused.append(strategy_name)

    def start_strategy(self, strategy_name: str) -> None:
        self.resumed.append(strategy_name)


class PlainStrategyEngine(FakeStrategyEngine):
    """Engine without the add_strategy provisioning API."""

    add_strategy = None


class FakeStrategyApp(BaseApp):
    app_name: str = "FakeStrategy"
    app_module: str = "tests.test_autonomous_provisioning"
    app_path: Path = Path(__file__).parent
    display_name: str = "FakeStrategy"
    engine_class: type[BaseEngine] = FakeStrategyEngine
    widget_name: str = "FakeWidget"
    icon_name: str = "fake.ico"


def build_config_file(tmp_path) -> str:
    config = AutonomousConfig(
        mode="paper",
        gateways={GATEWAY_NAME: {}},
        apps=[APP_NAME],
        app_settings={
            APP_NAME: {
                "strategies": [
                    {
                        "class_name": "DemoStrategy",
                        "name": "demo_trend",
                        "vt_symbol": "rb2410.SHFE",
                        "setting": {"fixed_size": 1, "class_names": ["Demo"]},
                    },
                    {"vt_symbol": "missing_name"},
                    {"class_name": "AutoNamed", "vt_symbol": "rb2601.SHFE"},
                    "not_a_dict",
                ]
            }
        },
        calendar={"profile": "full_day"},
        risk=RiskLimits(max_order_volume=5),
        runtime=RuntimeConfig(
            poll_interval_secs=0.05,
            contract_wait_secs=0,
            strategy_init_wait_secs=0,
            state_file="state.json",
            watchdog_stall_secs=3600,
        ),
    )
    path = tmp_path / "autonomous.json"
    save_config(config, path)
    return str(path)


def wait_state(path, statuses: set[str], timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    payload: dict = {}
    while time.monotonic() < deadline:
        payload = read_state(path)
        if payload.get("status") in statuses:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"等待状态 {statuses} 超时，当前状态 {payload.get('status')}")


def test_runner_provisions_strategies_from_config(tmp_path) -> None:
    config_path = build_config_file(tmp_path)
    state_path = tmp_path / "state.json"

    stop_event = threading.Event()
    runner = AutonomousRunner.from_config_file(config_path)
    runner.stop_event = stop_event

    thread = threading.Thread(target=runner.run_child, daemon=True)
    thread.start()
    try:
        wait_state(state_path, {"strategies_started", "trading"})

        engine = runner._strategy_engines[0][1]
        assert isinstance(engine, FakeStrategyEngine)

        # 引擎完成标准初始化序列
        assert engine.inited_engine is True
        assert engine.inited_all is True
        assert engine.started_all is True

        # 合法条目完成装配，非法条目被跳过且不影响其他条目
        assert engine.provisioned == [
            (
                "DemoStrategy",
                "demo_trend",
                "rb2410.SHFE",
                {"fixed_size": 1, "class_names": ["Demo"]},
            ),
            (
                "AutoNamed",
                "AutoNamed_rb2601",
                "rb2601.SHFE",
                {},
            ),
        ]
        assert sorted(engine.strategies) == ["AutoNamed_rb2601", "demo_trend"]

        # 快照适配器自动挂载，健康监控开始跟踪该策略
        assert runner._stats_adapters
    finally:
        stop_event.set()
        thread.join(timeout=30)

    assert not thread.is_alive()
    assert engine.stopped_all is True


def test_provisioning_is_idempotent_across_restarts(tmp_path) -> None:
    runner = AutonomousRunner.from_config_file(build_config_file(tmp_path))
    main_engine = MainEngine()
    try:
        event_engine = EventEngine()
        engine = FakeStrategyEngine(main_engine, event_engine, "fake")
        runner._strategy_engines = [(APP_NAME, engine)]

        runner._provision_strategies(APP_NAME, engine)
        first_count = len(engine.provisioned)
        runner._provision_strategies(APP_NAME, engine)

        # 引擎已存在的策略名被跳过，不会重复装配
        assert len(engine.provisioned) == first_count
    finally:
        main_engine.close()


def test_provisioning_skipped_when_engine_lacks_init_strategy(tmp_path) -> None:
    runner = AutonomousRunner.from_config_file(build_config_file(tmp_path))

    # 引擎没有 init_strategy 时应记录警告并返回，不抛异常
    main_engine = MainEngine()
    try:
        event_engine = EventEngine()
        engine = PlainStrategyEngine(main_engine, event_engine, "plain")
        runner._strategy_engines = [("CtaStrategy", engine)]
        runner._provision_strategies("CtaStrategy", engine)

        assert engine.provisioned == []
        assert runner.health.get_health("demo_trend") is None
    finally:
        main_engine.close()
