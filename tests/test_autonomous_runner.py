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
from vnpy.autonomous.risk import RiskGuard
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
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
        assert runner._active_session_day is not None

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
        shutdown_started = time.monotonic()
        thread.join(timeout=30)
        elapsed = time.monotonic() - shutdown_started

    assert not thread.is_alive()

    # 未配置邮件时不得拉起 SMTP 线程，关机必须迅速
    assert elapsed < 10
    final = read_state(state_path)
    assert final["status"] in ("stop_requested", "stopped")
    assert gateway.closed is True


def test_notify_without_email_uses_wechat_only(tmp_path) -> None:
    from types import SimpleNamespace

    from vnpy.trader.setting import SETTINGS

    config_path = build_config_file(tmp_path)
    runner = AutonomousRunner.from_config_file(config_path)

    sent: list[str] = []
    wechat_messages: list[str] = []
    runner.main_engine = SimpleNamespace(
        send_notification=lambda content, subject=None: sent.append(content),
        get_engine=lambda name: (
            SimpleNamespace(
                creds="creds",
                user_id="user",
                send_wechat=wechat_messages.append,
            )
            if name == "wechat"
            else None
        ),
    )

    saved_server: object = SETTINGS.get("email.server")
    saved_username: object = SETTINGS.get("email.username")
    saved_receiver: object = SETTINGS.get("email.receiver")
    try:
        SETTINGS["email.server"] = ""
        SETTINGS["email.username"] = "user@example.com"
        SETTINGS["email.receiver"] = "user@example.com"
        runner._notify("测试消息")
        assert sent == []
        assert wechat_messages == ["VeighNa Autonomous Trading\n测试消息"]

        SETTINGS["email.server"] = "smtp.example.com"
        runner._notify("第二条")
        assert sent == ["第二条"]
    finally:
        SETTINGS["email.server"] = saved_server
        SETTINGS["email.username"] = saved_username
        SETTINGS["email.receiver"] = saved_receiver


def test_session_start_resets_guard_daily_baseline(tmp_path) -> None:
    config_path = build_config_file(tmp_path)
    runner = AutonomousRunner.from_config_file(config_path)

    main_engine = MainEngine()
    try:
        from vnpy.event import Event
        from vnpy.trader.event import EVENT_ACCOUNT
        from vnpy.trader.object import AccountData

        guard = RiskGuard(main_engine, runner.config.risk)
        guard.install()
        guard.register_events(main_engine.event_engine)
        runner.main_engine = main_engine
        runner.guard = guard

        main_engine.event_engine.put(
            Event(
                EVENT_ACCOUNT,
                AccountData(
                    gateway_name="FAKE",
                    accountid="demo",
                    balance=100000,
                    frozen=0,
                ),
            )
        )
        deadline = time.monotonic() + 2
        while guard.state.current_balance != 100000 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert guard.state.current_balance == 100000

        # 盘中把基准改到别的值，时段开始时应重置回当前权益
        guard.state.day_start_balance = 80000
        runner._on_session_start()
        assert guard.state.day_start_balance == 100000
    finally:
        main_engine.close()


def test_atomic_write_json_survives_concurrent_readers(tmp_path) -> None:
    from vnpy.autonomous.runner import atomic_write_json

    target = tmp_path / "state.json"
    atomic_write_json(target, {"n": 0})

    stop = threading.Event()
    problems: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            if not read_state(target):
                problems.append("empty")
            time.sleep(0.01)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for index in range(80):
            atomic_write_json(target, {"n": index})
    finally:
        stop.set()
        thread.join(timeout=5)

    assert problems == []
    assert read_state(target)["n"] == 79


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


def test_snapshot_cumulative_trades_do_not_inflate_health() -> None:
    """Cumulative snapshots must not be recorded as per-trade pnl.

    The snapshot adapter publishes a running ``trades`` counter and a
    cumulative ``realized_pnl``. Recording the cumulative value each time
    the counter advances would multiply daily loss by the poll rate and
    trip the health breaker far too early.
    """
    runner = AutonomousRunner(
        AutonomousConfig(gateways={}),
    )
    from vnpy.autonomous.event import StrategySnapshot
    from vnpy.event import Event

    for trades, pnl in ((0, 0.0), (1, -50.0), (2, -100.0)):
        runner._process_snapshot_event(
            Event(
                "eStrategySnapshot",
                StrategySnapshot(
                    strategy_name="trend",
                    trades=trades,
                    realized_pnl=pnl,
                    equity=1000.0,
                ),
            )
        )

    health = runner.health.get_health("trend")
    assert health is not None
    # 初始播种不计，两笔成交各按增量 -50 记录
    assert health.trades == 2
    assert health.realized_pnl == -100.0
    assert health.consecutive_losses == 2
