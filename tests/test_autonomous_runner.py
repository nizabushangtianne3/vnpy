import json
import threading
import time
from datetime import datetime

from vnpy.autonomous.config import (
    AutonomousConfig,
    RiskLimits,
    RuntimeConfig,
    save_config,
)
from vnpy.autonomous.runner import (
    AppResolutionError,
    AutonomousRunner,
    TradeLedger,
    import_component,
    read_state,
    resolve_gateway_class,
)
from vnpy.event import EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import CancelRequest, OrderRequest, SubscribeRequest


GATEWAY_NAME: str = f"{__name__}:FakeGateway"


class FakeGateway(BaseGateway):
    """In-memory gateway used by the autonomous runner test."""

    default_name: str = "FAKE"
    exchanges: list[Exchange] = [Exchange.SHFE]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        self.orders: list[OrderRequest] = []
        self.cancels: list[CancelRequest] = []
        self.closed: bool = False

    def connect(self, setting: dict[str, str | int | float | bool]) -> None:
        return

    def close(self) -> None:
        self.closed = True

    def subscribe(self, req: SubscribeRequest) -> None:
        return

    def send_order(self, req: OrderRequest) -> str:
        self.orders.append(req)
        return f"{self.gateway_name}.{len(self.orders)}"

    def cancel_order(self, req: CancelRequest) -> None:
        self.cancels.append(req)

    def query_account(self) -> None:
        return

    def query_position(self) -> None:
        return


def make_order(volume: float = 1) -> OrderRequest:
    return OrderRequest(
        symbol="rb2410",
        exchange=Exchange.SHFE,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=volume,
        price=3000,
        offset=Offset.OPEN,
    )


def build_config_file(tmp_path) -> str:
    config = AutonomousConfig(
        mode="paper",
        gateways={GATEWAY_NAME: {"用户名": "demo"}},
        apps=[],
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


def test_import_component_and_registry_errors() -> None:
    gateway_class = import_component(GATEWAY_NAME)
    assert gateway_class is FakeGateway

    try:
        import_component("no_such_module:Fake")
    except AppResolutionError as exc:
        assert "导入失败" in str(exc)
    else:
        raise AssertionError("缺失模块应当抛出 AppResolutionError")

    try:
        resolve_gateway_class("CTP")
    except AppResolutionError:
        # 本机未安装 vnpy_ctp 时应给出可读错误
        pass
    else:
        # 安装了 vnpy_ctp 时解析成功，两种情况都允许
        pass


def test_runner_child_lifecycle_with_fake_gateway(tmp_path) -> None:
    config_path = build_config_file(tmp_path)
    state_path = tmp_path / "state.json"

    stop_event = threading.Event()
    runner = AutonomousRunner.from_config_file(config_path)
    runner.stop_event = stop_event

    thread = threading.Thread(target=runner.run_child, daemon=True)
    thread.start()
    try:
        payload = wait_state(state_path, {"strategies_started", "trading"})
        assert payload["mode"] == "paper"
        assert payload["session"] == "all_day"

        main_engine = runner.main_engine
        assert main_engine is not None
        gateway = main_engine.get_gateway(GATEWAY_NAME)
        assert isinstance(gateway, FakeGateway)

        # 正常委托通过风控直达网关
        vt_orderid = main_engine.send_order(make_order(1), GATEWAY_NAME)
        assert vt_orderid.startswith(GATEWAY_NAME)
        assert len(gateway.orders) == 1

        # 超限委托被风控拦截
        blocked = main_engine.send_order(make_order(10), GATEWAY_NAME)
        assert blocked == ""
        assert len(gateway.orders) == 1
    finally:
        stop_event.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    final = read_state(state_path)
    assert final["status"] in ("stop_requested", "stopped")
    assert gateway.closed is True


def test_calendar_built_from_config_profile(tmp_path) -> None:
    config = AutonomousConfig(
        gateways={GATEWAY_NAME: {}},
        calendar={"profile": "cn_futures_midnight"},
    )
    path = tmp_path / "autonomous.json"
    save_config(config, path)

    runner = AutonomousRunner.from_config_file(str(path))
    assert runner.calendar.is_open(datetime(2026, 10, 9, 0, 30))
    assert runner.state_path == tmp_path / "autonomous_state.json"


def test_trade_ledger_counts_total() -> None:
    ledger = TradeLedger()
    base = datetime(2026, 10, 9, 9, 30)
    ledger.record("trend", base)
    ledger.record("trend", base)
    ledger.record("momentum", base)

    assert ledger.count("trend") == 2
    assert ledger.total() == 3


def test_read_state_missing_file(tmp_path) -> None:
    assert read_state(tmp_path / "missing.json") == {}

    payload = {"status": "trading"}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_state(path) == payload
