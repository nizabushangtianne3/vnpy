from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

_STRATEGY_FILE = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "autonomous"
    / "strategies"
    / "momentum_carry_strategy.py"
)


def _strategy_class() -> type:
    """Load the reference strategy from its own file, the way the CLI does."""
    spec = importlib.util.spec_from_file_location("momentum_strategy", _STRATEGY_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy_class: type = module.MomentumCarryStrategy
    return strategy_class


class _RecordingEngine:
    """Minimal CTA engine that records orders and echoes fills."""

    def __init__(self) -> None:
        self.orders: list[tuple[str, str, float, float]] = []

    def cancel_order(self, strategy, vt_orderid):
        return None

    def cancel_all(self, strategy):
        return None

    def write_log(self, msg, strategy):
        return None

    def put_strategy_event(self, strategy):
        return None

    def send_email(self, msg, strategy):
        return None

    def get_pricetick(self, strategy):
        return 0.01

    def get_size(self, strategy):
        return 1

    def get_engine_type(self):
        return None

    def load_bar(self, *args, **kwargs):
        return []

    def send_order(self, strategy, direction, offset, price, volume, stop, lock, net):
        from vnpy.trader.object import TradeData

        self.orders.append((direction.name, offset.name, price, volume))
        strategy.on_trade(
            TradeData(
                gateway_name="TEST",
                symbol="600519",
                exchange=Exchange.SSE,
                orderid=f"order-{len(self.orders)}",
                tradeid=f"trade-{len(self.orders)}",
                direction=direction,
                offset=offset,
                price=price,
                volume=volume,
                datetime=datetime.now(),
            )
        )
        return [f"order-{len(self.orders)}"]


def _make_strategy(**setting: object) -> tuple[object, _RecordingEngine]:
    cls = _strategy_class()
    engine = _RecordingEngine()
    strategy = cls(
        cta_engine=engine,
        strategy_name="MomentumTest",
        vt_symbol="600519.SSE",
        setting={"fixed_size": 100, **setting},
    )
    engine.load_bar = lambda *args, **kwargs: []
    strategy.on_init()
    strategy.inited = True
    strategy.trading = True
    return strategy, engine


def _bar(step: int, close: float, low: float | None = None,
         high: float | None = None) -> BarData:
    """One daily bar in a synthetic series."""
    base = datetime(2026, 1, 1)
    return BarData(
        symbol="600519",
        exchange=Exchange.SSE,
        datetime=base + timedelta(days=step),
        interval=Interval.DAILY,
        volume=10000.0,
        turnover=close * 10000.0,
        open_price=close,
        high_price=high if high is not None else close * 1.01,
        low_price=low if low is not None else close * 0.99,
        close_price=close,
        gateway_name="TEST",
    )


def test_an_uptrend_opens_a_position() -> None:
    """A calm advancing series is the shape the filters are built to trade."""
    strategy, engine = _make_strategy(lookback=20, trend_window=10, atr_window=10)
    for step in range(80):
        strategy.on_bar(_bar(step, 100.0 + step * 0.5))
    assert any(kind == "OPEN" for _d, kind, _p, _v in engine.orders), (
        "an uptrend never opened a position"
    )


def test_a_tight_volatility_gate_refuses_every_entry() -> None:
    """The gate has to be able to say no, or it is decoration."""
    strategy, engine = _make_strategy(
        lookback=20, trend_window=10, atr_window=10, vol_ceiling=0.0001
    )
    for step in range(80):
        strategy.on_bar(_bar(step, 100.0 + step * 0.5))
    assert not engine.orders, "a ceiling of 0.01% still opened a position"


def test_the_stop_closes_the_position_it_protected() -> None:
    """A bar through the stop must close the position it was set for."""
    strategy, engine = _make_strategy(
        lookback=20, trend_window=10, atr_window=10, stop_atr=2.0
    )
    for step in range(60):
        strategy.on_bar(_bar(step, 100.0 + step * 0.5))
    assert any(kind == "OPEN" for _d, kind, _p, _v in engine.orders)

    stop = float(strategy.stop_price)
    assert stop > 0, "an opened position has to carry a stop"
    crash = 100.0 + 59 * 0.5 - 50.0
    strategy.on_bar(_bar(60, crash, low=crash * 0.9, high=crash))
    assert any(kind == "CLOSE" for _d, kind, _p, _v in engine.orders), (
        "a bar through the stop did not close the position"
    )


def test_no_daily_round_trip_is_ever_booked() -> None:
    """T+1: a position opened on a bar cannot close on that same bar."""
    strategy, engine = _make_strategy(lookback=20, trend_window=10, atr_window=10)
    held = 0.0
    for step in range(120):
        price = 100.0 + step * 0.4 if step % 7 else 100.0 + step * 0.4 - 30.0
        strategy.on_bar(_bar(step, price))
        if not engine.orders:
            continue
        _d, kind, _p, _v = engine.orders[-1]
        if kind == "OPEN":
            held += 100
        elif kind == "CLOSE":
            assert held > 0, "a close was booked with nothing held"
            held -= 100
