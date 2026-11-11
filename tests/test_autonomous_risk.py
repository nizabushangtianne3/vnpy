import time

from vnpy.autonomous.config import RiskLimits
from vnpy.autonomous.risk import RiskGuard
from vnpy.event import EventEngine
from vnpy.trader.constant import (
    Direction,
    Exchange,
    Offset,
    OrderType,
    Product,
    Status,
)
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_CONTRACT, EVENT_ORDER, EVENT_POSITION
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    AccountData,
    ContractData,
    OrderData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    CancelRequest,
)


class FakeGateway(BaseGateway):
    default_name: str = "FAKE"
    exchanges: list[Exchange] = [Exchange.SHFE]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        self.orders: list[OrderRequest] = []
        self.cancels: list[CancelRequest] = []

    def connect(self, setting: dict[str, str | int | float | bool]) -> None:
        return

    def close(self) -> None:
        return

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


def make_order(
    volume: float = 1,
    price: float = 3000,
    direction: Direction = Direction.LONG,
    offset: Offset = Offset.OPEN,
) -> OrderRequest:
    return OrderRequest(
        symbol="rb2410",
        exchange=Exchange.SHFE,
        direction=direction,
        type=OrderType.LIMIT,
        volume=volume,
        price=price,
        offset=offset,
    )


def wait_until(ready, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


def build_engine() -> tuple[MainEngine, FakeGateway]:
    main_engine = MainEngine()
    gateway = main_engine.add_gateway(FakeGateway)
    assert isinstance(gateway, FakeGateway)
    return main_engine, gateway


def put(main_engine: MainEngine, event_type: str, data) -> None:
    from vnpy.event import Event

    main_engine.event_engine.put(Event(event_type, data))


def seed_contract(main_engine: MainEngine) -> None:
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
    put(main_engine, EVENT_CONTRACT, contract)
    wait_until(lambda: main_engine.get_contract("rb2410.SHFE") is not None)


def seed_position(main_engine: MainEngine, volume: float, direction: Direction) -> None:
    position = PositionData(
        gateway_name="FAKE",
        symbol="rb2410",
        exchange=Exchange.SHFE,
        direction=direction,
        volume=volume,
        price=3000,
    )
    put(main_engine, EVENT_POSITION, position)
    wait_until(lambda: len(main_engine.get_all_positions()) >= 1)


def seed_active_order(main_engine: MainEngine) -> None:
    order = OrderData(
        gateway_name="FAKE",
        symbol="rb2410",
        exchange=Exchange.SHFE,
        orderid="1",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        price=3000,
        volume=1,
        status=Status.NOTTRADED,
    )
    put(main_engine, EVENT_ORDER, order)
    wait_until(lambda: len(main_engine.get_all_active_orders()) >= 1)


def seed_account(main_engine: MainEngine, balance: float) -> None:
    account = AccountData(
        gateway_name="FAKE",
        accountid="demo",
        balance=balance,
        frozen=0,
    )
    put(main_engine, EVENT_ACCOUNT, account)


def test_guard_passes_normal_order_and_blocks_oversized() -> None:
    main_engine, gateway = build_engine()
    try:
        seed_contract(main_engine)
        guard = RiskGuard(main_engine, RiskLimits(max_order_volume=5))
        guard.install()

        vt_orderid = main_engine.send_order(make_order(volume=1), "FAKE")
        assert vt_orderid == "FAKE.1"
        assert len(gateway.orders) == 1

        blocked = main_engine.send_order(make_order(volume=10), "FAKE")
        assert blocked == ""
        assert len(gateway.orders) == 1

        state = guard.snapshot()
        assert state["rejected_count"] == 1
        assert guard.armed is False
    finally:
        main_engine.close()


def test_max_order_value_uses_contract_size() -> None:
    main_engine, gateway = build_engine()
    try:
        seed_contract(main_engine)
        guard = RiskGuard(main_engine, RiskLimits(max_order_value=15000))
        guard.install()

        # 1 手 * 3000 * 10 = 30000 名义价值，超过 15000 上限
        assert main_engine.send_order(make_order(volume=1), "FAKE") == ""
        assert gateway.orders == []

        # 提高到 60000 后放行
        guard.limits.max_order_value = 60000
        assert main_engine.send_order(make_order(volume=2), "FAKE") == "FAKE.1"
    finally:
        main_engine.close()


def test_position_cap_blocks_open_orders() -> None:
    main_engine, gateway = build_engine()
    try:
        seed_contract(main_engine)
        seed_position(main_engine, volume=8, direction=Direction.LONG)
        guard = RiskGuard(main_engine, RiskLimits(max_position_volume=10))
        guard.install()

        # 净多头 8，再开 3 手将超过 10 上限
        assert main_engine.send_order(make_order(volume=3), "FAKE") == ""
        assert gateway.orders == []

        # 平仓单不受持仓上限限制
        close_order = main_engine.send_order(
            make_order(volume=8, direction=Direction.SHORT, offset=Offset.CLOSE),
            "FAKE",
        )
        assert close_order == "FAKE.1"
    finally:
        main_engine.close()


def test_daily_loss_breach_arms_and_flattens() -> None:
    main_engine, gateway = build_engine()
    try:
        seed_contract(main_engine)
        seed_position(main_engine, volume=2, direction=Direction.LONG)
        seed_active_order(main_engine)

        guard = RiskGuard(
            main_engine,
            RiskLimits(daily_loss_limit=1000, kill_switch_on_breach=True),
        )
        guard.install()
        guard.register_events(main_engine.event_engine)

        seed_account(main_engine, balance=100000)
        wait_until(lambda: guard.state.day_start_balance == 100000)

        # 亏损 1500，超过 1000 上限
        seed_account(main_engine, balance=98500)
        wait_until(lambda: guard.armed)

        # 新开仓被拦截
        assert main_engine.send_order(make_order(volume=1), "FAKE") == ""

        # 熔断平仓发送限价平仓单（价格取最新价档），
        # SHFE 上由 OffsetConverter 拆分为平今/平昨
        close_orders = [
            order for order in gateway.orders
            if order.offset in {Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY}
        ]
        assert len(close_orders) == 1
        assert close_orders[0].volume == 2
        assert close_orders[0].direction == Direction.SHORT
        assert close_orders[0].type == OrderType.LIMIT
        assert close_orders[0].price > 0
        assert close_orders[0].offset == Offset.CLOSETODAY

        # 活动委托被撤销
        assert len(gateway.cancels) == 1

        # 熔断后手动解除，恢复开仓
        guard.disarm()
        assert guard.armed is False
        assert main_engine.send_order(make_order(volume=1), "FAKE") != ""
    finally:
        main_engine.close()


def test_breach_without_flatten_blocks_all_orders() -> None:
    main_engine, gateway = build_engine()
    try:
        guard = RiskGuard(
            main_engine,
            RiskLimits(daily_loss_limit=500, kill_switch_on_breach=False),
        )
        guard.install()
        guard.register_events(main_engine.event_engine)

        seed_account(main_engine, balance=50000)
        wait_until(lambda: guard.state.day_start_balance == 50000)
        seed_account(main_engine, balance=49000)
        wait_until(lambda: guard.armed)

        assert main_engine.send_order(make_order(volume=1), "FAKE") == ""

        # kill_switch_on_breach=False 时不平仓，但允许减仓单控制风险
        assert main_engine.send_order(
            make_order(volume=1, direction=Direction.SHORT, offset=Offset.CLOSE),
            "FAKE",
        ) != ""
    finally:
        main_engine.close()


def test_reset_daily_refreshes_baseline() -> None:
    main_engine, gateway = build_engine()
    try:
        guard = RiskGuard(main_engine, RiskLimits())
        guard.install()
        guard.register_events(main_engine.event_engine)

        seed_account(main_engine, balance=200000)
        wait_until(lambda: guard.state.day_start_balance == 200000)

        seed_account(main_engine, balance=199000)
        wait_until(lambda: guard.state.current_balance == 199000)
        guard.reset_daily()

        assert guard.state.day_start_balance == 199000
        assert guard.state.peak_equity == 199000
    finally:
        main_engine.close()

def test_snapshot_includes_breached_flag() -> None:
    """Distinguishing armed (currently blocking) from breached (fired today) matters."""
    main_engine, _gateway = build_engine()
    try:
        seed_contract(main_engine)
        guard = RiskGuard(
            main_engine,
            RiskLimits(daily_loss_limit=1000, kill_switch_on_breach=False),
        )
        guard.install()

        assert guard.snapshot()["breached"] is False
        guard.arm("测试触发")
        assert guard.armed is True
        assert guard.snapshot()["breached"] is True

        guard.disarm()
        assert guard.armed is False
        # Disarm clears the block but the historical flag stays: an operator
        # must still see that the breaker fired earlier in this session.
        assert guard.snapshot()["breached"] is True

        guard.reset_daily()
        assert guard.snapshot()["breached"] is False
    finally:
        main_engine.close()
