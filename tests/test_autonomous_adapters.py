import time

from vnpy.autonomous.adapters.cta import CtaStatsAdapter
from vnpy.autonomous.config import HealthRules
from vnpy.autonomous.event import EVENT_STRATEGY_SNAPSHOT, StrategySnapshot
from vnpy.autonomous.health import HealthMonitor
from vnpy.event import EventEngine


class FakeStrategy:
    def __init__(
        self,
        name: str,
        trading: bool = True,
        inited: bool = True,
        pos: float = 0,
        trades: int = 0,
        variables: dict | None = None,
    ) -> None:
        self.strategy_name = name
        self.trading = trading
        self.inited = inited
        self.pos = pos
        self.trades = trades
        self.variables = variables if variables is not None else {}


class FakeCtaEngine:
    def __init__(self, strategies: dict) -> None:
        self.strategies = strategies


def wait_until(ready, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


def test_adapter_collects_strategy_state() -> None:
    engine = FakeCtaEngine(
        {
            "trend": FakeStrategy(
                "trend", pos=2, trades=5, variables={"realized_pnl": 120.5, "equity": 10120.5}
            ),
            "momentum": FakeStrategy("momentum", trading=False, pos=0),
        }
    )
    adapter = CtaStatsAdapter(engine, EventEngine())

    snapshots = adapter.collect_snapshots()
    by_name = {snapshot.strategy_name: snapshot for snapshot in snapshots}

    assert by_name["trend"].trading is True
    assert by_name["trend"].positions == 1
    assert by_name["trend"].trades == 5
    assert by_name["trend"].realized_pnl == 120.5
    assert by_name["trend"].equity == 10120.5
    assert by_name["momentum"].trading is False
    assert by_name["momentum"].realized_pnl is None


def test_adapter_publishes_events() -> None:
    event_engine = EventEngine()
    event_engine.start()
    try:
        engine = FakeCtaEngine({"trend": FakeStrategy("trend", pos=1)})
        adapter = CtaStatsAdapter(engine, event_engine)

        received: list[StrategySnapshot] = []
        event_engine.register(
            EVENT_STRATEGY_SNAPSHOT,
            lambda event: received.append(event.data),
        )

        published = adapter.publish_snapshots()
        assert len(published) == 1
        wait_until(lambda: len(received) == 1)
        assert received[0].strategy_name == "trend"
    finally:
        event_engine.stop()


def test_adapter_ignores_engines_without_strategies() -> None:
    class PlainEngine:
        pass

    adapter = CtaStatsAdapter(PlainEngine(), EventEngine())
    assert adapter.collect_snapshots() == []


def test_pnl_delta_feeds_health_monitor() -> None:
    monitor = HealthMonitor(HealthRules(max_consecutive_losses=3, halt_cooldown_secs=0))
    monitor.register_strategy("trend")
    engine = FakeCtaEngine(
        {
            "trend": FakeStrategy(
                "trend", variables={"realized_pnl": 0.0}
            ),
        }
    )
    adapter = CtaStatsAdapter(engine, EventEngine())

    last_pnl: dict[str, float] = {}

    def process(snapshot: StrategySnapshot) -> None:
        name = snapshot.strategy_name
        last = last_pnl.get(name)
        last_pnl[name] = snapshot.realized_pnl or 0.0
        if last is None or snapshot.realized_pnl is None:
            return
        delta = snapshot.realized_pnl - last
        if abs(delta) < 1e-9:
            return
        monitor.record_trade(name, delta)

    for snapshot in adapter.collect_snapshots():
        process(snapshot)

    # 初始播种不计为成交
    assert monitor.get_health("trend").trades == 0

    engine.strategies["trend"].variables = {"realized_pnl": -50}
    for snapshot in adapter.collect_snapshots():
        process(snapshot)
    assert monitor.get_health("trend").trades == 1
    assert monitor.get_health("trend").consecutive_losses == 1


def test_snapshot_error_counts_as_error() -> None:
    monitor = HealthMonitor(HealthRules(max_error_count=2, halt_cooldown_secs=0))
    monitor.record_error("trend")
    monitor.record_error("trend")

    assert monitor.is_halted("trend") is True
