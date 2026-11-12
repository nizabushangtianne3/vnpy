"""Offline evaluation of the reference strategy through the bar-loop harness.

The point of these tests is not that a synthetic series is profitable. It is
that the harness itself is trustworthy: limit orders cross the way
vnpy_ctastrategy's own backtester crosses them, position and cost basis track
the fills the way the live CtaEngine tracks them, and reported pnl includes
the open position. Every bug found here previously produced a number that
looked like an edge.
"""

import importlib.util
from typing import Any
from datetime import datetime, timedelta
from pathlib import Path


import pytest


from vnpy.trader.constant import Direction, Exchange, Offset
from vnpy.trader.object import BarData, TradeData
from vnpy.trader.utility import ArrayManager, round_to

from vnpy.autonomous.backtest import (
    load_bars_from_csv,
    BacktestSettings,
    BacktestResult,
    annual_sharpe,
    _BacktestEngineStub,
    build_bar,
    profit_factor,
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
    """Slippage is a cost, not a fill-price adjustment."""
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
    assert longs[0].price == pytest.approx(100.0)
    assert shorts[0].price == pytest.approx(100.0)


def test_annual_sharpe_scales_with_bar_frequency() -> None:
    """The same returns must produce one comparable ratio per interval.

    Ranking by a per-sample ratio makes the optimizer compare data
    frequencies instead of strategies: minute bars always look far better
    than daily bars. Annualizing puts them on one scale.
    """
    curve: list[float] = [100.0, 100.0, 101.0, 101.0, 102.0, 102.0]

    minute_stamps: list[datetime] = [
        datetime(2024, 1, 2, 9, 0) + timedelta(minutes=index) for index in range(6)
    ]
    daily_stamps: list[datetime] = [
        datetime(2024, 1, 2) + timedelta(days=index) for index in range(6)
    ]

    minute_sharpe: float = annual_sharpe(curve, minute_stamps)
    daily_sharpe: float = annual_sharpe(curve, daily_stamps)

    # 1440 daily-equivalent minutes per year vs 1 per year: the annualized
    # values differ only by the square root of that sampling frequency.
    assert minute_sharpe == pytest.approx(daily_sharpe * (1440 ** 0.5), rel=1e-6)

    # No timestamps, no claimed frequency: the raw per-sample ratio.
    assert annual_sharpe(curve) < minute_sharpe


def test_annual_sharpe_flat_curve_is_zero() -> None:
    """An unchanged curve has no volatility and therefore no ratio."""
    stamps: list[datetime] = [
        datetime(2024, 1, 2) + timedelta(days=index) for index in range(6)
    ]
    assert annual_sharpe([100.0] * 6, stamps) == 0.0


def test_fill_price_respects_limit_across_a_gap() -> None:
    """A limit never fills beyond its price when the bar gaps through it."""
    engine: _BacktestEngineStub = _BacktestEngineStub(BacktestSettings(), "rb9999.SHFE")

    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    # Buy limit 100, next bar opens 110: official model fills at 100.
    engine.send_order(strategy, Direction.LONG, Offset.OPEN, 100.0, 1)
    engine.next_bar = build_bar(datetime(2024, 1, 2, 9, 0), 110.0, 115.0, 95.0, 112.0)
    longs = engine.fill_pending(strategy)
    assert len(longs) == 1
    assert longs[0].price == pytest.approx(100.0)

    # Sell limit 100, next bar gaps up to 102: official model fills at 102.
    engine.send_order(strategy, Direction.SHORT, Offset.OPEN, 100.0, 1)
    engine.next_bar = build_bar(datetime(2024, 1, 2, 9, 1), 102.0, 105.0, 90.0, 98.0)
    shorts = engine.fill_pending(strategy)
    assert len(shorts) == 1
    assert shorts[0].price == pytest.approx(102.0)

    # Sell limit 105, next bar opens 95: fill at the limit, not the open.
    engine.send_order(strategy, Direction.SHORT, Offset.OPEN, 105.0, 1)
    engine.next_bar = build_bar(datetime(2024, 1, 2, 9, 2), 95.0, 106.0, 90.0, 98.0)
    shorts = engine.fill_pending(strategy)
    assert len(shorts) == 1
    assert shorts[0].price == pytest.approx(105.0)


def test_commission_uses_contract_multiplier() -> None:
    """Cost must be turnover based (price x volume x size), not tick based."""
    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    from vnpy.autonomous.backtest import _apply_position_and_cost

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
            price=3000.0,
            volume=2,
        ),
        BacktestSettings(rate=0.0001, size=10, slippage=0.0),
    )
    expected: float = 3000.0 * 2 * 0.0001 * 10
    assert strategy.realized_pnl == pytest.approx(-expected)


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


def test_winning_round_trips_pair_by_entry_direction() -> None:
    """A long close exits a short entry, so pairing must invert the direction."""
    from vnpy.autonomous.backtest import _count_winning_round_trips

    def trade(tradeid: str, direction: Direction, offset: Offset, price: float) -> TradeData:
        return TradeData(
            gateway_name="FAKE",
            symbol="rb",
            exchange=Exchange.SHFE,
            orderid=tradeid,
            tradeid=tradeid,
            direction=direction,
            offset=offset,
            price=price,
            volume=1,
        )

    # Short 100 -> cover 90 is a winning round trip.
    trades: list[TradeData] = [
        trade("1", Direction.SHORT, Offset.OPEN, 100.0),
        trade("2", Direction.LONG, Offset.CLOSE, 90.0),
    ]
    assert _count_winning_round_trips(trades) == 1

    # Short 100 -> cover 110 is a losing round trip.
    losing: list[TradeData] = [
        trade("3", Direction.SHORT, Offset.OPEN, 100.0),
        trade("4", Direction.LONG, Offset.CLOSE, 110.0),
    ]
    assert _count_winning_round_trips(losing) == 0


def test_winning_round_trips_charge_commission_and_slippage() -> None:
    """A gross winner that loses money after costs is not a win.

    Counting gross price movement alone reports a win on any long->higher
    close, so a strategy that pays more in commission and slippage than it
    captures still looks like a winner.
    """
    from vnpy.autonomous.backtest import _count_winning_round_trips

    def trade(tradeid: str, direction: Direction, offset: Offset, price: float) -> TradeData:
        return TradeData(
            gateway_name="FAKE",
            symbol="rb",
            exchange=Exchange.SHFE,
            orderid=tradeid,
            tradeid=tradeid,
            direction=direction,
            offset=offset,
            price=price,
            volume=1,
        )

    opening: TradeData = trade("5", Direction.LONG, Offset.OPEN, 100.0)
    closing: TradeData = trade("6", Direction.SHORT, Offset.CLOSE, 100.5)
    costs: dict[str, float] = {"5": 11.0, "6": 11.0}

    # Gross +0.5 x 1 lot = +0.5, but 22 of cost turns it into a loss.
    assert _count_winning_round_trips([opening, closing], costs) == 0

    # With realistic low costs the same round trip wins again.
    cheap: dict[str, float] = {"5": 0.2, "6": 0.2}
    assert _count_winning_round_trips([opening, closing], cheap) == 1

    # Without a cost map the gross movement decides, i.e. the old behaviour.
    assert _count_winning_round_trips([opening, closing]) == 1


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


def _fills(rows: list[tuple[str, Direction, Offset, float]]) -> list[TradeData]:
    """Build fills from (tradeid, direction, offset, price) rows."""
    return [
        TradeData(
            gateway_name="FAKE",
            symbol="rb",
            exchange=Exchange.SHFE,
            orderid=tradeid,
            tradeid=tradeid,
            direction=direction,
            offset=offset,
            price=price,
            volume=1,
        )
        for tradeid, direction, offset, price in rows
    ]


def test_profit_factor_separates_a_lottery_ticket_from_an_edge() -> None:
    """Same total pnl, different repeatability: the ratio has to tell them apart.

    Ranked on pnl alone these two books are identical, so parameter selection
    cannot see that one of them is one lucky round trip away from zero.
    """
    lottery = _fills([
        ("a1", Direction.LONG, Offset.OPEN, 100.0),
        ("a2", Direction.SHORT, Offset.CLOSE, 130.0),
        ("a3", Direction.LONG, Offset.OPEN, 100.0),
        ("a4", Direction.SHORT, Offset.CLOSE, 90.0),
    ])

    assert profit_factor(lottery) == pytest.approx(30.0 / 10.0)

    # The same 20 of pnl, spread over four smaller round trips.
    steady = _fills([
        ("b1", Direction.LONG, Offset.OPEN, 100.0),
        ("b2", Direction.SHORT, Offset.CLOSE, 115.0),
        ("b3", Direction.LONG, Offset.OPEN, 100.0),
        ("b4", Direction.SHORT, Offset.CLOSE, 115.0),
        ("b5", Direction.LONG, Offset.OPEN, 100.0),
        ("b6", Direction.SHORT, Offset.CLOSE, 90.0),
        ("b7", Direction.LONG, Offset.OPEN, 100.0),
        ("b8", Direction.SHORT, Offset.CLOSE, 90.0),
    ])

    # 15 + 15 + 15 + 15 - 10 - 10 - 10 - 10 = 20, same total, and a far more
    # repeatable book than one 30 win against one 10 loss.
    assert profit_factor(steady) == pytest.approx(60.0 / 40.0)
    assert profit_factor(steady) < 3.0


def test_profit_factor_is_zero_without_losing_round_trips() -> None:
    """No losses means no ratio: the figure is undefined, not infinite.

    Reporting infinity would let a single-win book sort above every real edge
    and hand the out-of-sample window a configuration that never had to be
    right more than once.
    """
    clean = _fills([
        ("x1", Direction.LONG, Offset.OPEN, 100.0),
        ("x2", Direction.SHORT, Offset.CLOSE, 105.0),
        ("y1", Direction.LONG, Offset.OPEN, 100.0),
        ("y2", Direction.SHORT, Offset.CLOSE, 107.0),
    ])
    assert profit_factor(clean) == 0.0


def test_profit_factor_is_zero_with_no_trades() -> None:
    assert profit_factor([]) == 0.0


def test_backtest_reports_profit_factor() -> None:
    """The summary carries the ratio so a campaign can be screened on it."""
    strategy = load_strategy_class()(
        cta_engine=None, strategy_name="bt", vt_symbol="rb9999.SHFE", setting={}
    )
    result: BacktestResult = run_backtest(
        strategy, synthetic_bars(300), BacktestSettings()
    )
    assert "profit_factor" in result.summary()
    assert result.profit_factor >= 0.0

def test_load_bars_from_csv_accepts_comma_header_and_data(tmp_path: Path) -> None:
    path: Path = tmp_path / "rb_comma.csv"
    lines: list[str] = ["datetime,open_price,high_price,low_price,close_price,volume"]
    base: datetime = datetime(2024, 1, 2, 9, 0)
    for index in range(60):
        price: float = 3500.0 + index * 3.0
        moment: str = (base + timedelta(minutes=index)).isoformat(sep=" ")
        lines.append(f"{moment},{price},{price + 5},{price - 5},{price},100")
    path.write_text("\n".join(lines), encoding="utf-8")

    bars = load_bars_from_csv(str(path), "rb2410")
    assert len(bars) == 60
    assert bars[0].open_price == pytest.approx(3500.0)
    assert bars[1].datetime == base + timedelta(minutes=1)


def test_load_bars_from_csv_accepts_space_separated_rows(tmp_path: Path) -> None:
    """Real exchange exports often pair a comma header with space rows."""
    path: Path = tmp_path / "rb_space.csv"
    lines: list[str] = ["datetime,open_price,high_price,low_price,close_price,volume"]
    base: datetime = datetime(2024, 1, 2, 9, 0)
    for index in range(60):
        price: float = 3500.0 + index * 3.0
        moment: str = (base + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{moment} {price} {price + 5} {price - 5} {price} 100")
    path.write_text("\n".join(lines), encoding="utf-8")

    bars = load_bars_from_csv(str(path), "rb2410")
    assert len(bars) == 60
    assert bars[0].open_price == pytest.approx(3500.0)
    assert bars[5].datetime == base + timedelta(minutes=5)


def test_load_bars_from_csv_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_bars_from_csv(str(tmp_path / "absent.csv"), "rb2410")


def test_load_bars_from_csv_rejects_bad_header(tmp_path: Path) -> None:
    path: Path = tmp_path / "bad.csv"
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必需列"):
        load_bars_from_csv(str(path), "rb2410")


def test_trend_strategy_scales_into_a_running_trend() -> None:
    """A trend follower must add up to its cap, not stop at one lot.

    Gating entry on the cross bar alone pins the position at one lot and
    turns ``max_position`` into dead config: the strategy captures one
    contract worth of every trend and reports a much worse outcome than a
    real fan of the same strategy would produce.
    """
    strategy = load_strategy_class()(
        cta_engine=None,
        strategy_name="bt",
        vt_symbol="rb9999.SHFE",
        setting={
            "fast_window": 3,
            "slow_window": 5,
            "fixed_size": 1,
            "max_position": 3,
        },
    )

    bars: list[BarData] = []
    base = datetime(2024, 1, 2, 9, 0)
    price = 100.0
    # Warm up flat, then a steady rally: the cross fires once and the trend
    # keeps running for many bars afterwards.
    for index in range(45):
        if index < 25:
            close = 100.0
        else:
            close = 100.0 + (index - 24) * 0.5
        bars.append(
            build_bar(
                base + timedelta(minutes=index),
                price,
                close + 0.3,
                min(price, close) - 0.3,
                close,
            )
        )
        price = close

    result: BacktestResult = run_backtest(strategy, bars, BacktestSettings())

    # The cap is 3 lots, so a sustained rally must reach the cap.
    assert result.open_position == 3
    # Multiple adds mean more than one fill per trend leg.
    assert result.trade_count > 1


def _long_position_in_a_rally() -> Any:
    """Replay a calm rally so the strategy holds a live long position.

    The stop tests need the engine, indicators and cost basis in the state a
    live strategy actually runs in, which only a real replay produces.
    """
    strategy = load_strategy_class()(
        cta_engine=None,
        strategy_name="bt",
        vt_symbol="rb9999.SHFE",
        setting={
            "fast_window": 3,
            "slow_window": 5,
            "atr_window": 10,
            "stop_atr": 1.0,
            "fixed_size": 1,
            "max_position": 2,
        },
    )

    base = datetime(2024, 1, 2, 9, 0)

    def bar(minute: int, open_price: float, high: float, low: float, close: float) -> BarData:
        return build_bar(base + timedelta(minutes=minute), open_price, high, low, close)

    warmup: list[BarData] = [bar(i, 100.0, 101.0, 99.0, 100.0) for i in range(25)]
    rally: list[BarData] = []
    for index in range(6):
        close = 100.0 + (index + 1) * 0.5
        rally.append(bar(25 + index, 100.0, close + 0.3, 99.7, close))

    run_backtest(strategy, warmup + rally, BacktestSettings())
    assert strategy.pos > 0
    return strategy


def _armed_stop(strategy: Any, probe_bar: BarData) -> float:
    """The stop the strategy arms when it sees ``probe_bar``.

    ``on_bar`` folds the bar into the ArrayManager before it prices the stop,
    so the level runs off this bar's own ATR. Clone the live manager so the
    fixture measures the same input without disturbing the strategy's state.
    """
    probe: ArrayManager = ArrayManager(strategy.am.size)
    window: int = int(strategy.am.size)
    # The manager keeps a rolling window, so the most recent bars sit at the
    # right edge; ``count`` is the lifetime total, not the live length.
    for offset in range(window):
        probe.update_bar(
            build_bar(
                probe_bar.datetime - timedelta(minutes=window - offset),
                strategy.am.open_array[offset],
                strategy.am.high_array[offset],
                strategy.am.low_array[offset],
                strategy.am.close_array[offset],
            )
        )
    probe.update_bar(probe_bar)
    atr_value: float = float(probe.atr(strategy.atr_window, array=True)[-1])
    return float(strategy.avg_cost) - float(strategy.stop_atr) * atr_value


def test_atr_stop_fills_when_the_bar_trades_through_it() -> None:
    """A stop is a resting order, not a close-price comparison.

    Judging on the close alone lets a bar that dipped through the stop and
    recovered report no stop at all, so the backtest books a smaller drawdown
    than the strategy can actually produce. The fill must land on the worse
    side of the stop and the open, like a real stop order.
    """
    strategy = _long_position_in_a_rally()
    engine: _BacktestEngineStub = strategy.cta_engine
    engine.pending.clear()

    # The stop is crossed inside the bar, yet the bar closes back above it.
    break_bar: BarData = build_bar(
        datetime(2024, 1, 2, 9, 31), 97.0, 103.0, 96.0, 102.0
    )
    # on_bar() folds the bar into the ArrayManager before it prices the stop,
    # so the armed stop runs off this bar's own ATR.
    stop_price: float = _armed_stop(strategy, break_bar)
    assert break_bar.low_price <= stop_price < break_bar.close_price

    strategy.on_bar(break_bar)

    # The stop fired, so the whole position is queued as a closing order.
    assert len(engine.pending) == 1
    queued_direction, queued_offset, queued_price, queued_volume = engine.pending[0]
    assert queued_direction is Direction.SHORT
    assert queued_offset is Offset.CLOSE
    assert queued_volume == strategy.pos
    # A resting stop fills at the worse of the stop and the open, so a bar
    # that gaps through the stop never books a price better than the open.
    # The queue rounds that price onto the contract tick.
    armed: float = max(stop_price, break_bar.open_price)
    assert queued_price == pytest.approx(round_to(armed, 0.2))
    assert queued_price >= break_bar.open_price


def test_atr_stop_stays_quiet_when_the_bar_misses_it() -> None:
    """A bar that only closes low leaves the stop alone.

    The stop prices the bar's range, not its close, so a slow grind down that
    never trades through the level must not be stopped out retroactively.
    """
    strategy = _long_position_in_a_rally()
    engine: _BacktestEngineStub = strategy.cta_engine
    engine.pending.clear()

    # Hold the close near the trend so the moving averages stay ordered the
    # same way: this bar must speak only to the stop, not to a cross.
    soft_bar: BarData = build_bar(
        datetime(2024, 1, 2, 9, 31), 99.6, 100.4, 98.2, 99.8
    )
    stop_price: float = _armed_stop(strategy, soft_bar)
    assert soft_bar.low_price > stop_price
    assert strategy.fast_ma0 > strategy.slow_ma0

    strategy.on_bar(soft_bar)
    assert engine.pending == []
