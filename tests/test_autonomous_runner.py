import json
import threading
import time
from datetime import date, datetime

import pytest

from vnpy.autonomous.config import (
    AutonomousConfig,
    AllocationConfig,
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
    restore_health_state,
    resolve_gateway_class,
)
from vnpy.autonomous.risk import RiskGuard
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Product
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    CancelRequest,
    ContractData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
)


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


def test_calendar_includes_builtin_holidays(tmp_path) -> None:
    config = AutonomousConfig(
        gateways={GATEWAY_NAME: {}},
        calendar={"profile": "cn_futures"},
    )
    path = tmp_path / "autonomous.json"
    save_config(config, path)

    runner = AutonomousRunner.from_config_file(str(path))
    assert runner.calendar.is_trading_day(date(2026, 10, 1)) is False


def test_calendar_merges_user_holiday_file(tmp_path) -> None:
    (tmp_path / "extra.json").write_text(
        json.dumps(["2027-01-01"]), encoding="utf-8"
    )
    config = AutonomousConfig(
        gateways={GATEWAY_NAME: {}},
        calendar={"profile": "cn_futures", "holidays_file": "extra.json"},
    )
    path = tmp_path / "autonomous.json"
    save_config(config, path)

    runner = AutonomousRunner.from_config_file(str(path))
    assert runner.calendar.is_trading_day(date(2027, 1, 1)) is False
    assert runner.calendar.is_trading_day(date(2026, 10, 1)) is False


def test_restore_health_reapplies_today_halt(tmp_path) -> None:
    """A halted strategy must stay halted across a same-day child restart."""
    config = AutonomousConfig(gateways={GATEWAY_NAME: {}})
    path = tmp_path / "autonomous.json"
    save_config(config, path)
    runner = AutonomousRunner.from_config_file(str(path))
    runner.health.register_strategy("trend")
    runner.health.halt("trend", "连续亏损", datetime(2026, 10, 9, 10, 0))
    state = {
        "session_day": "2026-10-09",
        "health": runner.health.snapshot(),
    }
    (tmp_path / "autonomous_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )

    # Simulate a fresh child process: new monitor, same state file.
    fresh_runner = AutonomousRunner.from_config_file(str(path))
    assert fresh_runner.health.is_halted("trend") is False
    frozen_now = datetime(2026, 10, 9, 12, 0)
    fresh_runner.health._clock = lambda: frozen_now  # type: ignore[method-assign]
    restore_health_state(fresh_runner.health, runner.state_path)
    assert fresh_runner.health.is_halted("trend") is True
    health = fresh_runner.health.get_health("trend")
    assert health is not None
    assert health.halt_reason == "连续亏损"
    assert health.halted_at is not None
    assert health.halted_at.isoformat().startswith("2026-10-09")


def test_restore_health_stale_day_ignored(tmp_path) -> None:
    """Yesterday's halt payload must not bleed into today."""
    config = AutonomousConfig(gateways={GATEWAY_NAME: {}})
    path = tmp_path / "autonomous.json"
    save_config(config, path)
    runner = AutonomousRunner.from_config_file(str(path))
    stale_state = tmp_path / "stale.json"
    stale_state.write_text(
        json.dumps(
            {
                "session_day": "2020-01-01",
                "health": {
                    "trend": {
                        "halted": True,
                        "halt_reason": "昨日熔断",
                        "halted_at": "2020-01-01T10:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    restore_health_state(runner.health, stale_state)
    assert runner.health.is_halted("trend") is False


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


def test_pause_strategy_flattens_strategy_positions(tmp_path) -> None:
    """Halting a strategy must also close its open risk, not just stop it."""
    from types import SimpleNamespace

    config_path = build_config_file(tmp_path)
    runner = AutonomousRunner.from_config_file(config_path)

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
            net_position=False,
        )
        from vnpy.event import Event
        from vnpy.trader.event import EVENT_CONTRACT

        main_engine.event_engine.put(Event(EVENT_CONTRACT, contract))
        deadline = time.monotonic() + 2
        while main_engine.get_contract("rb2410.SHFE") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert main_engine.get_contract("rb2410.SHFE") is not None

        position = PositionData(
            gateway_name="FAKE",
            symbol="rb2410",
            exchange=Exchange.SHFE,
            direction=Direction.LONG,
            volume=3,
            price=3000,
        )
        from vnpy.trader.event import EVENT_POSITION

        main_engine.event_engine.put(Event(EVENT_POSITION, position))
        deadline = time.monotonic() + 2
        while not main_engine.get_all_positions() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert main_engine.get_all_positions()

        guard = RiskGuard(main_engine, runner.config.risk)
        guard.install()
        runner.main_engine = main_engine
        runner.guard = guard
        runner.health.register_strategy("trend")

        # A CTA-like engine that reports the trend strategy holding +3.
        trend_strategy = SimpleNamespace(strategy_name="trend", pos=3, vt_symbol="rb2410.SHFE")
        other_strategy = SimpleNamespace(strategy_name="other", pos=0, vt_symbol="rb2410.SHFE")
        fake_engine = SimpleNamespace(
            close=lambda: None,
            strategies={"trend": trend_strategy, "other": other_strategy},
            symbol_strategy_map={"rb2410.SHFE": [trend_strategy, other_strategy]},
        )
        main_engine.engines["cta"] = fake_engine
        runner._strategy_engines = [("CtaStrategy", fake_engine)]

        runner._pause_strategy("trend", "连续亏损达到上限")

        close_orders = [
            order for order in gateway.orders
            if order.offset in {Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY}
        ]
        # SHFE 平仓会被 OffsetConverter 拆分；拆分前后各做一次风控校验，
        # 但真正到达网关、属于该策略的平仓单只有一笔。
        assert close_orders
        assert all(order.direction == Direction.SHORT for order in close_orders)
        assert all(order.reference == "CtaStrategy_trend" for order in close_orders)
        assert sum(order.volume for order in close_orders) == 3
        assert guard.state.order_count >= len(close_orders)
    finally:
        main_engine.close()


def test_pause_strategy_without_position_sends_no_close(tmp_path) -> None:
    """A halted but flat strategy must not generate a stray close order."""
    from types import SimpleNamespace

    config_path = build_config_file(tmp_path)
    runner = AutonomousRunner.from_config_file(config_path)

    main_engine = MainEngine()
    try:
        gateway = main_engine.add_gateway(FakeGateway)
        assert isinstance(gateway, FakeGateway)

        from vnpy.event import Event
        from vnpy.trader.event import EVENT_CONTRACT

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
            net_position=False,
        )
        main_engine.event_engine.put(Event(EVENT_CONTRACT, contract))
        deadline = time.monotonic() + 2
        while main_engine.get_contract("rb2410.SHFE") is None and time.monotonic() < deadline:
            time.sleep(0.05)

        guard = RiskGuard(main_engine, runner.config.risk)
        guard.install()
        runner.main_engine = main_engine
        runner.guard = guard

        flat_strategy = SimpleNamespace(strategy_name="trend", pos=0, vt_symbol="rb2410.SHFE")
        fake_engine = SimpleNamespace(
            close=lambda: None,
            strategies={"trend": flat_strategy},
            symbol_strategy_map={"rb2410.SHFE": [flat_strategy]},
        )
        main_engine.engines["cta"] = fake_engine
        runner._strategy_engines = [("CtaStrategy", fake_engine)]

        runner._pause_strategy("trend", "连续亏损达到上限")

        assert gateway.orders == []
    finally:
        main_engine.close()


def test_allocate_capital_writes_targets_to_state(tmp_path) -> None:
    """The portfolio config must reach a per-strategy number at runtime.

    Recording equity samples without calling allocate() leaves the portfolio
    config decorative: nothing downstream can size off it.
    """
    from types import SimpleNamespace

    config = AutonomousConfig(
        gateways={GATEWAY_NAME: {}},
        apps=["CtaStrategy"],
        portfolio=AllocationConfig(
            method="equal",
            max_weight=0.5,
            min_weight=0.0,
            cash_fraction=0.0,
        ),
    )
    path = tmp_path / "autonomous.json"
    save_config(config, path)
    runner = AutonomousRunner.from_config_file(str(path))

    main_engine = MainEngine()
    try:
        two_strategies: list[SimpleNamespace] = [
            SimpleNamespace(strategy_name="trend", pos=0, vt_symbol="rb2410.SHFE"),
            SimpleNamespace(strategy_name="carry", pos=0, vt_symbol="cu2406.SHFE"),
        ]
        fake_engine = SimpleNamespace(
            close=lambda: None,
            strategies={
                "trend": two_strategies[0],
                "carry": two_strategies[1],
            },
        )
        main_engine.engines["cta"] = fake_engine
        runner.main_engine = main_engine
        runner._strategy_engines = [("CtaStrategy", fake_engine)]

        # One live account with 100k equity, no cash reserve, 2 strategies
        # capped at 0.5 each: the cap cannot hold the whole mass, so each
        # lands at the cap and the remainder stays unallocated.
        from vnpy.event import Event
        from vnpy.trader.event import EVENT_ACCOUNT
        from vnpy.trader.object import AccountData

        main_engine.event_engine.put(
            Event(
                EVENT_ACCOUNT,
                AccountData(
                    gateway_name="FAKE",
                    accountid="demo",
                    balance=100000.0,
                    frozen=0,
                ),
            )
        )
        deadline = time.monotonic() + 2
        while not main_engine.get_all_accounts() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert main_engine.get_all_accounts()

        runner._allocate_capital()

        assert set(runner._allocation) == {"trend", "carry"}
        assert runner._allocation["trend"] == pytest.approx(50000.0, abs=0.01)
        assert runner._allocation["carry"] == pytest.approx(50000.0, abs=0.01)

        payload = runner._state_payload("trading")
        assert payload["allocation"] == runner._allocation
    finally:
        main_engine.close()


def test_allocate_capital_needs_account_and_strategy(tmp_path) -> None:
    """No equity or no strategy means no allocation, and no crash."""
    config = AutonomousConfig(gateways={GATEWAY_NAME: {}})
    path = tmp_path / "autonomous.json"
    save_config(config, path)
    runner = AutonomousRunner.from_config_file(str(path))

    main_engine = MainEngine()
    try:
        runner.main_engine = main_engine
        runner._strategy_engines = []
        runner._allocate_capital()
        assert runner._allocation == {}

        from types import SimpleNamespace

        fake_engine = SimpleNamespace(close=lambda: None, strategies={})
        main_engine.engines["cta"] = fake_engine
        runner._strategy_engines = [("CtaStrategy", fake_engine)]
        runner._allocate_capital()
        assert runner._allocation == {}
    finally:
        main_engine.close()
