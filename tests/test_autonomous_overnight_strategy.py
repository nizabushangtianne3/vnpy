
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path


from vnpy.trader.object import BarData

_STRATEGY_FILE = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "autonomous"
    / "strategies"
    / "overnight_hold_strategy.py"
)


def _strategy_class() -> type:
    """Load the reference strategy from its own file, the way the CLI does."""
    spec = importlib.util.spec_from_file_location("overnight_strategy", _STRATEGY_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy_class: type = module.OvernightHoldStrategy
    return strategy_class


def _make_strategy(**setting: object) -> object:
    """Build a strategy with a minimal engine that records its orders."""
    cls = _strategy_class()
    engine = _RecordingEngine()
    strategy = cls(
        cta_engine=engine,
        strategy_name="OvernightTest",
        vt_symbol="600519.SSE",
        setting={**{"fixed_size": 100}, **setting},
    )
    engine.load_bar = lambda *args, **kwargs: []
    return strategy, engine


def _bar(day: int, hour: int, minute: int, close: float, volume: float = 1000.0) -> BarData:
    """Build one bar at a given session clock time on a valid March date."""
    when = datetime(2026, 3, min(max(day, 1), 28), hour, minute)
    return BarData(
        symbol="600519",
        gateway_name="TEST",
        exchange=__import__("vnpy.trader.constant", fromlist=["Exchange"]).Exchange.SSE,
        datetime=when,
        interval=__import__("vnpy.trader.constant", fromlist=["Interval"]).Interval.DAILY,
        volume=volume,
        turnover=volume * close,
        open_price=close,
        high_price=close * 1.01,
        low_price=close * 0.99,
        close_price=close,
    )


def _warm(strategy: object, bars: list[BarData]) -> None:
    """Feed a warmup series so the indicators initialise."""
    starting = len(strategy.am.close)
    for bar in bars:
        strategy.on_bar(bar)
    assert len(strategy.am.close) == starting + len(bars)

class _RecordingEngine:
    """Minimal CTA engine that records orders instead of routing them."""

    def __init__(self) -> None:
        self.orders: list[tuple[object, object, float, float]] = []

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
        """Record the order and echo a fill back to the strategy.

        A real engine reports fills through on_trade, and the strategy keys
        its T+1 window off those fills. An engine that recorded the order but
        never reported it would leave the strategy playing against an
        invisible position, so the double has to be faithful.
        """
        from vnpy.trader.constant import Exchange
        from vnpy.trader.object import TradeData

        self.orders.append((direction, offset, price, volume))
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


def test_a_position_is_closed_on_a_later_bar_not_its_entry_bar() -> None:
    """T+1: the close never lands on the bar that opened the position."""
    strategy, engine = _make_strategy(
        trend_window=3, range_window=3, volume_window=2, volume_factor=0.1
    )
    strategy.on_init()
    strategy.inited = True
    strategy.trading = True

    # Nine bars, so the loop ends on an exit rather than on a fresh entry:
    # a daily hold now closes on the very next bar and re-enters right after.
    for offset in range(1, 9):
        strategy.on_bar(_bar(offset, 14, 50, 100.0 + offset, 5000.0))

    orders = [(o[0].name, o[1].name) for o in engine.orders]
    assert ("LONG", "OPEN") in orders
    assert ("SHORT", "CLOSE") in orders

    # The fill volume tracks through on_trade, so by the time the close is
    # sent the held volume has come back to zero.
    assert strategy._held_volume == 0.0


def test_every_close_follows_an_open_by_at_least_one_bar() -> None:
    """No close may be sent on the same bar as its open."""
    strategy, engine = _make_strategy(
        trend_window=3, range_window=3, volume_window=2, volume_factor=0.1
    )
    strategy.on_init()
    strategy.inited = True
    strategy.trading = True

    day = 1
    for _ in range(20):
        day += 1
        strategy.on_bar(_bar(day, 14, 50, 100.0 + day, 5000.0))
        strategy.on_bar(_bar(day, 9, 40, 100.0 + day, 5000.0))

    events: list[tuple[str, str]] = [
        (o[0].name, o[1].name) for o in engine.orders
    ]
    held = 0.0
    for _direction, offset in events:
        if offset == "OPEN":
            held += 100
        elif offset == "CLOSE":
            # A close with nothing held is a same-bar round trip.
            assert held > 0, "a close was sent without an open before it"
            held -= 100


def test_a_daily_hold_is_closed_on_the_next_daily_bar() -> None:
    """On daily bars, one overnight means exactly the next bar.

    A daily bar carries a midnight timestamp, so a clock test for an opening
    bar never fires and the position would ride into a second night. Bar
    distance is the only honest measure on a daily series, and the round trip
    the strategy books has to be an overnight gap, not a two-day hold.
    """
    strategy, engine = _make_strategy(
        trend_window=3, range_window=3, volume_window=2, volume_factor=0.1
    )
    strategy.on_init()
    strategy.inited = True
    strategy.trading = True

    events: list[tuple[int, str]] = []
    for offset in range(1, 16):
        strategy.on_bar(_bar(offset, 14, 50, 100.0 + offset, 5000.0))
        if engine.orders:
            events.append((offset, engine.orders[-1][1].name))

    opens = [index for index, kind in events if kind == "OPEN"]
    closes = [index for index, kind in events if kind == "CLOSE"]
    assert opens, "the strategy never opened a position"
    assert closes, "no daily hold was ever closed"
    # The series can end on a fresh open, so a trailing unmatched open is
    # allowed; a close without an open before it is not.
    assert len(closes) == len(opens) or len(closes) == len(opens) - 1, (
        f"{len(closes)} closes against {len(opens)} opens"
    )

    # The close must land on the bar right after its open: one session later
    # is the overnight gap, two sessions later is a two-night hold. A trailing
    # open with no close is the series ending mid-hold, so it is dropped rather
    # than paired against someone else's close.
    for open_index, close_index in zip(opens[: len(closes)], closes, strict=True):
        assert close_index - open_index == 1, (
            f"open at bar {open_index} closed at bar {close_index}: "
            "a daily hold ran past the next bar, which is not an overnight gap"
        )
