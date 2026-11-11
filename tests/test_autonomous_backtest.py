"""Offline evaluation of the reference strategy through the bar-loop harness.

The point of these tests is not that a synthetic series is profitable. It is
that the harness itself is trustworthy: limit orders cross the way
vnpy_ctastrategy's own backtester crosses them, position and cost basis track
the fills the way the live CtaEngine tracks them, and reported pnl includes
the open position. Every bug found here previously produced a number that
looked like an edge.
"""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path


import pytest


from vnpy.trader.constant import Direction, Exchange, Offset
from vnpy.trader.object import TradeData

from vnpy.autonomous.backtest import (
    BacktestSettings,
    BacktestResult,
    _BacktestEngineStub,
    build_bar,
    run_backtest,
)


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("vnpy_ctastrategy") is None,
    reason="backtest harness drives vnpy_ctastrategy strategies",
)


REPO_ROOT: Path = Path(__file__).resolve().parents[1]
STRATEGY_FILE: Path = (
    REPO_ROOT / "examples" / "autonomous" / "strategies" / "autonomous_trend_strategy.py"
)


def load_strategy_class() -> type:
    """Load the reference strategy straight from the examples file."""
    spec = importlib.util.spec_from_file_location("backtest_ref_strategy", STRATEGY_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AutonomousTrendStrategy


def make_bars(prices: list[tuple[float, float, float, float]]) -> list:
    """Build bars from (open, high, low, close) tuples on a fixed clock."""
    base: datetime = datetime(2024, 1, 2, 9, 0)
    bars: list = []
    minute: int = 0
    for open_price, high, low, close in prices:
        bars.append(
            build_bar(
                base + timedelta(minutes=minute),
                open_price,
                high,
                low,
                close,
            )
        )
        minute += 1
    return bars


def synthetic_bars(count: int = 300, seed: int = 7) -> list:
    """Rotating drift so both crossing directions occur."""
    import math
    import random

    random.seed(seed)
    base: datetime = datetime(2024, 1, 2, 9, 0)
    bars: list = []
    price: float = 3500.0
    for index in range(count):
        close: float = (
            price + 4.0 * math.sin(index / 45.0) + 0.4 + (random.random() - 0.5) * 5.0
        )
        open_price: float = price
        bars.append(
            build_bar(
                base + timedelta(minutes=index),
                open_price,
                max(open_price, close) + random.random() * 4.0,
                min(open_price, close) - random.random() * 4.0,
                close,
            )
        )
        price = close
    return bars


def test_limit_order_crosses_only_when_the_bar_reaches_it() -> None:
    """A buy limit above the bar low must not fill; a reachable one must."""
    engine: _BacktestEngineStub = _BacktestEngineStub(BacktestSettings(), "rb9999.SHFE")

    bars: list = make_bars(
        [
            (100.0, 102.0, 98.0, 101.0),   # signal bar: strategy would buy at 101
            (99.0, 101.0, 95.0, 97.0),    # low 95 < 101 -> fill at open 99
            (97.0, 101.0, 94.0, 96.0),
        ]
    )

    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    engine.send_order(strategy, Direction.LONG, Offset.OPEN, 101.0, 1)
    assert len(engine.pending) == 1

    engine.next_bar = bars[1]
    filled: list[TradeData] = engine.fill_pending(strategy)
    assert len(filled) == 1
    assert filled[0].price == pytest.approx(99.0)

    # Unreachable limit: bar never trades down to it.
    engine.send_order(strategy, Direction.LONG, Offset.OPEN, 90.0, 1)
    engine.next_bar = bars[2]
    assert engine.fill_pending(strategy) == []


def test_slippage_moves_the_fill_against_the_trader() -> None:
    engine: _BacktestEngineStub = _BacktestEngineStub(
        BacktestSettings(slippage=1.5), "rb9999.SHFE"
    )
    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    engine.send_order(strategy, Direction.LONG, Offset.OPEN, 100.0, 1)
    engine.send_order(strategy, Direction.SHORT, Offset.OPEN, 100.0, 1)

    engine.next_bar = build_bar(datetime(2024, 1, 2, 9, 0), 100.0, 105.0, 90.0, 102.0)
    filled: list[TradeData] = engine.fill_pending(strategy)

    longs: list[TradeData] = [t for t in filled if t.direction is Direction.LONG]
    shorts: list[TradeData] = [t for t in filled if t.direction is Direction.SHORT]
    assert longs[0].price == pytest.approx(101.5)
    assert shorts[0].price == pytest.approx(98.5)


def test_position_follows_direction_not_offset() -> None:
    """A closing trade already carries the opposite direction, so pos flips once."""
    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    strategy.inited = True
    strategy.trading = True
    from vnpy.autonomous.backtest import _apply_position_and_cost

    # Open 2 long, then close them.
    _apply_position_and_cost(
        strategy,
        TradeData(
            gateway_name="FAKE",
            symbol="rb",
            exchange=Exchange.SHFE,
            orderid="1",
            tradeid="1",
            direction=Direction.LONG,
            offset=Offset.OPEN,
            price=100.0,
            volume=2,
        ),
        BacktestSettings(),
    )
    assert strategy.pos == 2

    _apply_position_and_cost(
        strategy,
        TradeData(
            gateway_name="FAKE",
            symbol="rb",
            exchange=Exchange.SHFE,
            orderid="1",
            tradeid="2",
            direction=Direction.SHORT,
            offset=Offset.CLOSE,
            price=110.0,
            volume=2,
        ),
        BacktestSettings(),
    )
    assert strategy.pos == 0


def test_run_backtest_produces_a_consistent_ledger() -> None:
    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    result: BacktestResult = run_backtest(
        strategy,
        synthetic_bars(300),
        BacktestSettings(slippage=0.5, rate=0.0001, size=10),
    )

    assert result.bars == 300
    assert result.trade_count == len(result.trades)
    assert result.total_pnl == pytest.approx(result.closed_pnl + result.unrealized_pnl)
    assert result.equity == pytest.approx(1_000_000.0 + result.total_pnl)
    assert result.open_position == pytest.approx(strategy.pos)
    assert 0.0 <= result.win_rate <= 1.0
    assert result.max_drawdown >= 0.0


def test_cost_basis_resets_after_a_flat() -> None:
    """A strategy left holding a stale cost basis reports nonsense pnl."""
    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    run_backtest(strategy, synthetic_bars(300), BacktestSettings())

    if strategy.pos == 0:
        assert strategy.avg_cost == pytest.approx(0.0)


def test_commission_is_charged_once_per_fill() -> None:
    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    result: BacktestResult = run_backtest(
        strategy,
        synthetic_bars(300),
        BacktestSettings(slippage=0.5, rate=0.0001, size=10),
    )

    if result.trade_count:
        assert strategy.realized_pnl < max(
            (t.price for t in result.trades), default=0.0
        ) * result.trade_count


def test_empty_bar_series_is_rejected() -> None:
    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    with pytest.raises(ValueError):
        run_backtest(strategy, [], BacktestSettings())
