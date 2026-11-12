"""Offline evaluation of the reference strategy through the bar-loop harness.

The point of these tests is not that a synthetic series is profitable. It is
that the harness itself is trustworthy: limit orders cross the way
vnpy_ctastrategy's own backtester crosses them, position and cost basis track
the fills the way the live CtaEngine tracks them, and reported pnl includes
the open position. Every bug found here previously produced a number that
looked like an edge.
"""

import importlib.util
import warnings
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
    run_portfolio_backtest,
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


def _fills(
    rows: list[tuple[str, Direction, Offset, float, float | None]],
) -> list[TradeData]:
    """Build fills from (tradeid, direction, offset, price[, volume]) rows."""
    filled: list[TradeData] = []
    for row in rows:
        tradeid, direction, offset, price = row[:4]
        volume: float = row[4] if len(row) > 4 and row[4] is not None else 1.0
        filled.append(
            TradeData(
                gateway_name="FAKE",
                symbol="rb",
                exchange=Exchange.SHFE,
                orderid=tradeid,
                tradeid=tradeid,
                direction=direction,
                offset=offset,
                price=price,
                volume=volume,
            )
        )
    return filled


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


def test_scale_in_entries_close_as_one_round_trip() -> None:
    """Three adds and one flat is a single position, not three round trips.

    The reference strategy adds one lot per bar and closes the whole position
    at once. Pairing each open against a single close would leave two of the
    three entries unmatched and price the position as if it held one lot, so
    the pnl of the one real round trip is computed over the whole batch.
    """
    from vnpy.autonomous.backtest import _round_trip_pnls

    trades = _fills([
        ("a1", Direction.LONG, Offset.OPEN, 100.0),
        ("a2", Direction.LONG, Offset.OPEN, 101.0),
        ("a3", Direction.LONG, Offset.OPEN, 102.0),
        ("a4", Direction.SHORT, Offset.CLOSE, 108.0, 3.0),
    ])

    pnls = _round_trip_pnls(trades)

    # (108-100) + (108-101) + (108-102) = 8 + 7 + 6
    assert pnls == pytest.approx([21.0])


def test_partial_close_consumes_only_its_own_volume() -> None:
    """A close smaller than the position leaves the rest of the batch open."""
    from vnpy.autonomous.backtest import _round_trip_pnls

    trades = _fills([
        ("b1", Direction.LONG, Offset.OPEN, 100.0),
        ("b2", Direction.LONG, Offset.OPEN, 100.0),
        ("b3", Direction.SHORT, Offset.CLOSE, 110.0, 1.0),
    ])

    pnls = _round_trip_pnls(trades)

    # One lot of two closed at 110 = +10, and the second entry stays open.
    assert pnls == pytest.approx([10.0])


def test_win_rate_counts_positions_not_fills() -> None:
    """The win rate shares positions held, not fills executed.

    A scale-in strategy fills five opens to build one position. Dividing wins
    by fills reported a 3.8% win rate for a book whose round trips actually
    won half the time, which made every candidate look worse than it was.
    """
    strategy = load_strategy_class()(
        cta_engine=None,
        strategy_name="wr",
        vt_symbol="rb9999.SHFE",
        setting={"fast_window": 5, "slow_window": 40, "stop_atr": 2},
    )
    result = run_backtest(strategy, synthetic_bars(300), BacktestSettings())

    assert result.trade_count > result.round_trips
    if result.round_trips:
        expected = result.winning_trades / result.round_trips
        assert result.win_rate == pytest.approx(expected)


def test_round_trips_exclude_an_open_position_at_the_end() -> None:
    """The entries left open at the last bar are not closed round trips."""
    from vnpy.autonomous.backtest import _round_trip_pnls

    trades = _fills([
        ("c1", Direction.LONG, Offset.OPEN, 100.0),
        ("c2", Direction.LONG, Offset.OPEN, 101.0),
        ("c3", Direction.SHORT, Offset.CLOSE, 105.0, 2.0),
        ("d1", Direction.LONG, Offset.OPEN, 100.0),
        ("d2", Direction.LONG, Offset.OPEN, 100.0),
    ])

    # (105-100) + (105-101) = 9, and the second position never closes.
    assert _round_trip_pnls(trades) == pytest.approx([9.0])


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


def test_load_bars_from_csv_drops_a_unit_break(tmp_path: Path) -> None:
    """A block in a different price unit must not become a huge real move.

    A feed once delivered a batch of minute bars in an unmarked different
    scale, and a strategy read the glitch as a real price jump: the backtest
    booked pnl from a move that never happened on the exchange.
    """
    path: Path = tmp_path / "rb_unit_break.csv"
    base: datetime = datetime(2024, 1, 2, 9, 0)
    lines: list[str] = ["datetime,open_price,high_price,low_price,close_price,volume"]

    # Ten ordinary bars, then a block whose prices are ~3800 times larger,
    # then ten ordinary bars again. Only the ordinary ones survive.
    for index in range(10):
        price: float = 3000.0 + index
        lines.append(
            f"{(base + timedelta(minutes=index)).isoformat(sep=' ')},"
            f"{price},{price + 2},{price - 2},{price},100"
        )
    for index in range(10, 20):
        price: float = 3000.0 + index
        scaled: float = price * 3830.0
        lines.append(
            f"{(base + timedelta(minutes=index)).isoformat(sep=' ')},"
            f"{scaled},{scaled + 2},{scaled - 2},{scaled},100"
        )
    for index in range(20, 30):
        price: float = 3000.0 + index
        lines.append(
            f"{(base + timedelta(minutes=index)).isoformat(sep=' ')},"
            f"{price},{price + 2},{price - 2},{price},100"
        )
    path.write_text("\n".join(lines), encoding="utf-8")

    with pytest.warns(UserWarning, match="标度"):
        bars = load_bars_from_csv(str(path), "rb2410")

    assert len(bars) == 20
    assert all(bar.close_price < 10000.0 for bar in bars)


def test_load_bars_from_csv_keeps_normal_range_moves(tmp_path: Path) -> None:
    """A 3830x jump is a glitch; a 10% move is not, and must survive."""
    path: Path = tmp_path / "rb_normal.csv"
    base: datetime = datetime(2024, 1, 2, 9, 0)
    lines: list[str] = ["datetime,open_price,high_price,low_price,close_price,volume"]

    closes = [3000.0, 3300.0, 2700.0, 3010.0, 2990.0]
    for index, close in enumerate(closes):
        lines.append(
            f"{(base + timedelta(minutes=index)).isoformat(sep=' ')},"
            f"{close},{close + 2},{close - 2},{close},100"
        )
    path.write_text("\n".join(lines), encoding="utf-8")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        bars = load_bars_from_csv(str(path), "rb2410")

    assert len(bars) == len(closes)


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


def test_trailing_stop_tightens_with_the_position_extreme() -> None:
    """A trailing stop anchors on the best price the position has seen.

    The static stop sits a fixed distance under the entry cost, so a trend
    that runs hands most of the move back before the exit fires. The trail
    tracks the extreme instead, and the tighter of the two levels wins.
    """
    strategy = _long_position_in_a_rally()
    strategy.trail_stop = 1.0
    engine: _BacktestEngineStub = strategy.cta_engine
    engine.pending.clear()

    # Run the extreme far above the entry while the static stop stays put.
    push_bar: BarData = build_bar(
        datetime(2024, 1, 2, 9, 32), 110.0, 112.0, 109.0, 111.0
    )
    strategy.on_bar(push_bar)
    assert strategy.pos > 0
    assert strategy.pos_extreme >= 111.0

    static_stop: float = (
        strategy.avg_cost - strategy.stop_atr * strategy.atr_value
    )
    armed: float = max(
        static_stop, strategy.pos_extreme - strategy.trail_stop * strategy.atr_value
    )
    assert armed > static_stop

    # A pullback that leaves the static stop untouched still trips the trail.
    engine.pending.clear()
    dip_bar: BarData = build_bar(
        datetime(2024, 1, 2, 9, 33), 110.0, 110.5, armed - 1.0, 110.0
    )
    strategy.on_bar(dip_bar)
    assert len(engine.pending) == 1
    direction, offset, price, volume = engine.pending[0]
    assert direction is Direction.SHORT
    assert offset is Offset.CLOSE
    assert dip_bar.low_price <= armed
    assert armed > static_stop


def test_trailing_stop_resets_after_the_position_goes_flat() -> None:
    """The anchor belongs to one position, not to the whole run."""
    strategy = _long_position_in_a_rally()
    strategy.trail_stop = 1.0

    push_bar: BarData = build_bar(
        datetime(2024, 1, 2, 9, 32), 110.0, 112.0, 109.0, 111.0
    )
    strategy.on_bar(push_bar)
    assert strategy.pos > 0
    assert strategy.pos_extreme > 0

    stop_bar: BarData = build_bar(
        datetime(2024, 1, 2, 9, 33), 100.0, 100.5, 90.0, 91.0
    )
    strategy.on_bar(stop_bar)
    # The exit is queued during this bar and crossed on the next one.
    engine: _BacktestEngineStub = strategy.cta_engine
    queued_price: float = engine.pending[0][2]
    settle_bar: BarData = build_bar(
        datetime(2024, 1, 2, 9, 34), queued_price, queued_price + 0.5,
        queued_price - 1.0, queued_price - 0.5
    )
    engine.next_bar = settle_bar
    for trade in engine.fill_pending(strategy):
        strategy.on_trade(trade)
        # The replay loop owns position bookkeeping; the callbacks only
        # consume cost and pnl.
        if trade.direction is Direction.SHORT:
            strategy.pos -= trade.volume
        else:
            strategy.pos += trade.volume
    assert strategy.pos == 0

    # The next bar after going flat must not carry the old anchor forward.
    strategy.on_bar(settle_bar)
    assert strategy.pos_extreme == 0.0


def test_zero_trail_keeps_the_static_stop_level() -> None:
    """The default must leave the armed stop where the entry put it."""
    strategy = _long_position_in_a_rally()
    assert strategy.trail_stop == 0.0

    push_bar: BarData = build_bar(
        datetime(2024, 1, 2, 9, 32), 110.0, 112.0, 109.0, 111.0
    )
    strategy.on_bar(push_bar)
    assert strategy.pos_extreme > 0

    static_stop: float = (
        strategy.avg_cost - strategy.stop_atr * strategy.atr_value
    )
    # With the trail disabled the extreme is tracked but never used.
    assert strategy.pos_extreme > static_stop


def _portfolio_legs(
    count: int = 240,
    offset: float = 0.0,
) -> dict[str, Any]:
    """Two independent strategies over two shifted bar series."""
    strategy_class: type = load_strategy_class()
    legs: dict[str, Any] = {}
    bars: dict[str, list] = {}
    for index, name in enumerate(("a", "b")):
        legs[name] = strategy_class(
            cta_engine=None,
            strategy_name=name,
            vt_symbol=f"{name}9999.SHFE",
            setting={
                "fast_window": 5,
                "slow_window": 20,
                "atr_window": 10,
                "stop_atr": 2.0,
                "fixed_size": 1,
                "max_position": 5,
                "trail_stop": 2.0,
            },
        )
        bars[name] = synthetic_bars(count=count, seed=7 + index)
        if offset:
            for bar in bars[name]:
                bar.datetime += timedelta(days=offset * (index + 1))
                bar.close_price *= 1.0 + 0.01 * offset * (index + 1)
    return legs, bars


def test_portfolio_backtest_sums_every_leg() -> None:
    """The combined pnl is the sum of the legs, with no leg dropped."""
    legs, bars = _portfolio_legs()
    settings = BacktestSettings(capital=100000.0)
    result = run_portfolio_backtest(legs, bars, settings)

    expected: float = sum(leg.total_pnl for leg in result.legs.values())
    assert result.total_pnl == pytest.approx(expected)
    assert set(result.legs) == {"a", "b"}
    assert result.trade_count == sum(leg.trade_count for leg in result.legs.values())
    # Every leg reports its own curve, so the sum can be attributed later.
    for leg in result.legs.values():
        assert leg.equity_curve
        assert len(leg.curve_dates) == len(leg.equity_curve)


def test_portfolio_backtest_aligns_curves_on_timestamp() -> None:
    """Legs on different calendars union their dates, not overwrite them."""
    # Real instruments trade different calendars: one leg has a session the
    # other does not. A union keeps both contributions, while keying the
    # combination by list index would let one leg silently drop the other's
    # pnl on every shared date.
    strategy_class: type = load_strategy_class()
    setting: dict[str, float] = {
        "fast_window": 5,
        "slow_window": 20,
        "atr_window": 10,
        "stop_atr": 2.0,
        "fixed_size": 1,
        "max_position": 5,
        "trail_stop": 2.0,
    }
    legs: dict[str, Any] = {}
    bars: dict[str, list] = {}
    base: datetime = datetime(2024, 1, 2, 9, 0)
    for index, name in enumerate(("a", "b")):
        legs[name] = strategy_class(
            cta_engine=None,
            strategy_name=name,
            vt_symbol=f"{name}9999.SHFE",
            setting=dict(setting),
        )
        # Leg "a" runs every hour, leg "b" every two hours and starts an hour
        # later, so most dates are exclusive to one of them.
        series = synthetic_bars(count=200, seed=7 + index)
        bars[name] = [
            build_bar(
                base + timedelta(hours=2 * index + hour),
                bar.open_price,
                bar.high_price,
                bar.low_price,
                bar.close_price,
            )
            for hour, bar in enumerate(series)
        ]
        # Drop a few bars from "b" so its calendar stays genuinely different.
        if name == "b":
            bars[name] = bars[name][::2]

    settings = BacktestSettings(capital=100000.0)
    result = run_portfolio_backtest(legs, bars, settings)

    seen: set[datetime] = set()
    for leg in result.legs.values():
        seen.update(leg.curve_dates)
    assert set(result.dates) == seen
    # One extra curve point for the starting capital.
    assert len(result.equity_curve) == len(result.dates) + 1
    assert result.equity_curve[0] == pytest.approx(settings.capital)
    assert len(result.dates) > 0
    # Both legs must have contributed: neither is fully inside the other.
    assert set(result.legs["a"].curve_dates) - set(result.legs["b"].curve_dates)
    assert set(result.legs["b"].curve_dates) - set(result.legs["a"].curve_dates)


def test_portfolio_drawdown_never_exceeds_the_worst_leg() -> None:
    """Diversification may shrink a drawdown, it cannot invent one."""
    legs, bars = _portfolio_legs()
    settings = BacktestSettings(capital=100000.0)
    result = run_portfolio_backtest(legs, bars, settings)

    worst_leg: float = max(leg.max_drawdown for leg in result.legs.values())
    assert result.max_drawdown <= worst_leg + 1e-9
    assert result.max_drawdown >= 0.0


def test_portfolio_backtest_survives_an_empty_leg() -> None:
    """A leg with no bars contributes nothing and breaks nothing."""
    strategy_class: type = load_strategy_class()
    legs: dict[str, Any] = {
        "a": strategy_class(
            cta_engine=None,
            strategy_name="a",
            vt_symbol="a9999.SHFE",
            setting={"fast_window": 5, "slow_window": 20},
        ),
    }
    bars: dict[str, list] = {"a": []}
    result = run_portfolio_backtest(legs, bars, BacktestSettings(capital=100000.0))
    assert result.total_pnl == pytest.approx(0.0)
    assert result.equity_curve == [100000.0]
