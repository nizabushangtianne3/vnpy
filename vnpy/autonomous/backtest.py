"""Offline strategy evaluation for the autonomous trading loop.

A strategy that only ever ran inside the live engine has never been shown to
make money. This module drives a real
:class:`~vnpy_ctastrategy.template.CtaTemplate` strategy through a bar series,
fills its orders against the next bar, and reports the trade ledger and
performance statistics. It is the first gate of the promotion path documented
in ``docs/community/autonomous/promotion.md``: no strategy should be armed for
paper trading until it shows a positive edge here.

The evaluator deliberately avoids ``vnpy_ctabacktester``. That package is
optional, GUI oriented, and needs a populated bar database, while this one
takes any bar iterable, so a campaign can run from a CSV export, a synthetic
generator, or a database query with no extra dependencies.

Execution model mirrors vnpy_ctastrategy.backtesting: a limit order placed on
bar N is crossed against bar N+1 only when that bar traded through the limit,
and the fill price is the better of the limit price and the bar open. Slippage
is charged per contract as a separate cost, exactly like the official daily
pnl accounting. Without the one-bar lag a trend strategy looks profitable
purely because it saw prices that were not yet knowable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from vnpy.trader.constant import Direction, Exchange, Interval, Offset
from vnpy.trader.object import BarData, TradeData
from vnpy.trader.utility import round_to


def _import_cta_types() -> tuple[Any, Any]:
    """Import the CTA template lazily, the way runner.py resolves app classes.

    vnpy_ctastrategy is an optional dependency, so a hard top-level import would
    make the whole autonomous package unusable on an install without it, and
    vnpy_ctastrategy ships no py.typed marker.
    """
    try:
        from vnpy_ctastrategy import CtaTemplate  # type: ignore[import-untyped]
        from vnpy_ctastrategy.base import (  # type: ignore[import-untyped]
            EngineType,
        )
    except ImportError as exc:
        raise ImportError(
            "vnpy.autonomous.backtest requires vnpy_ctastrategy; "
            "install it with: pip install vnpy_ctastrategy"
        ) from exc
    return CtaTemplate, EngineType


@dataclass
class BacktestSettings:
    """Cost and execution assumptions for one evaluation run."""

    slippage: float = 0.0
    rate: float = 0.0
    size: float = 10
    capital: float = 1_000_000.0
    pricetick: float = 0.2


@dataclass
class BacktestResult:
    """Trade ledger and performance statistics of one run."""

    trades: list[TradeData] = field(default_factory=list)
    bars: int = 0
    trade_count: int = 0
    winning_trades: int = 0
    closed_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    open_position: float = 0.0
    total_pnl: float = 0.0
    equity: float = 0.0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0

    def summary(self) -> dict[str, float | int]:
        """Flat mapping for state files, notifications and assertions."""
        return {
            "bars": self.bars,
            "trade_count": self.trade_count,
            "winning_trades": self.winning_trades,
            "open_position": self.open_position,
            "closed_pnl": self.closed_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl,
            "equity": self.equity,
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
        }


class _BacktestEngineStub:
    """Minimal engine surface so a strategy runs outside the CtaEngine.

    The template reaches the engine through a handful of calls only. Backing
    them with plain lists keeps the strategy under test the production code
    path rather than a reimplementation.
    """

    def __init__(self, settings: BacktestSettings, vt_symbol: str) -> None:
        self.settings: BacktestSettings = settings
        self.vt_symbol: str = vt_symbol
        self.symbol: str = vt_symbol.split(".")[0]
        self.exchange: Exchange = Exchange.SHFE
        self.trades: list[TradeData] = []
        self.logs: list[str] = []
        self.pending: list[tuple[Direction, Offset, float, float]] = []
        self._order_seq: int = 0
        self._trade_seq: int = 0
        self.next_bar: BarData | None = None

    # --- strategy side -------------------------------------------------
    def get_engine_type(self) -> Any:
        """Report the backtesting engine type."""
        from vnpy_ctastrategy.base import EngineType

        return EngineType.BACKTESTING

    def get_pricetick(self, strategy: Any) -> float:
        """Return the contract price tick."""
        return self.settings.pricetick

    def get_size(self, strategy: Any) -> float:
        """Return the contract multiplier."""
        return self.settings.size

    def write_log(self, msg: str, strategy: Any) -> None:
        """Capture log output instead of writing to a logger."""
        self.logs.append(msg)

    def put_strategy_event(self, strategy: Any) -> None:
        """No UI to refresh during a backtest."""
        return None

    def send_email(self, msg: str, strategy: Any) -> None:
        """Notification is out of scope for offline evaluation."""
        return None

    def load_bar(
        self,
        vt_symbol: str,
        days: int,
        interval: Interval,
        callback: object,
        use_database: bool = False,
    ) -> list[BarData]:
        """Warm-up history comes from the replayed bars, not a database."""
        return []

    # --- order flow ----------------------------------------------------
    def send_order(
        self,
        strategy: Any,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        stop: bool = False,
        lock: bool = False,
        net: bool = False,
    ) -> list[str]:
        """Queue a limit order for the next bar, as the live engine does.

        The strategy author picks the price, typically ``bar.close_price``.
        That price is a limit, not a guarantee: the next bar only fills it when
        the market actually traded through it. Prices are rounded to the
        contract tick before queuing, as the official backtester does.
        """
        if volume <= 0:
            return []

        price = round_to(price, self.settings.pricetick)
        self._order_seq += 1
        vt_orderid: str = f"bt_{self._order_seq}"
        self.pending.append((direction, offset, price, volume))
        return [vt_orderid]

    def cancel_all(self, strategy: Any) -> None:
        """Drop unfilled orders, mirroring the live engine call."""
        self.pending.clear()

    # --- execution -----------------------------------------------------
    def fill_pending(self, strategy: Any) -> list[TradeData]:
        """Cross live limit orders against the current bar.

        Mirrors vnpy_ctastrategy.backtesting: a long limit fills when its price
        reaches the bar low, a short when it reaches the bar high. The fill
        price is ``min(limit, open)`` for buys and ``max(limit, open)`` for
        sells, so a gap always fills at the better price and an order never
        fills beyond its limit. Slippage is not baked into the fill price;
        it is charged per contract with the commission.
        """
        if self.next_bar is None or not self.pending:
            return []

        bar: BarData = self.next_bar
        queued: list[tuple[Direction, Offset, float, float]] = list(self.pending)
        self.pending.clear()

        filled: list[TradeData] = []
        for direction, offset, price, volume in queued:
            if direction is Direction.LONG:
                crossed: bool = price >= bar.low_price > 0
                fill_price: float = min(price, bar.open_price)
            else:
                crossed = price <= bar.high_price and bar.high_price > 0
                fill_price = max(price, bar.open_price)

            if not crossed:
                continue

            self._trade_seq += 1
            trade: TradeData = TradeData(
                gateway_name="BACKTEST",
                symbol=self.symbol,
                exchange=self.exchange,
                orderid=f"bt_{self._trade_seq}",
                tradeid=f"bt_t{self._trade_seq}",
                direction=direction,
                offset=offset,
                price=max(fill_price, 0.0),
                volume=volume,
                datetime=bar.datetime,
            )
            self.trades.append(trade)
            filled.append(trade)

        return filled


def run_backtest(
    strategy: Any,
    bars: Iterable[BarData],
    settings: BacktestSettings | None = None,
) -> BacktestResult:
    """Replay bars through a strategy and report its performance.

    Per bar the loop: fills orders the strategy queued during the previous
    bar, hands them to ``on_trade`` so cost basis and pnl update, then lets
    ``on_bar`` decide what to queue next. Cost is charged once per fill.
    """
    # Fail fast with a clear message when the optional app is missing.
    _import_cta_types()

    if settings is None:
        settings = BacktestSettings()

    bar_list: list[BarData] = list(bars)
    if not bar_list:
        raise ValueError("backtest needs at least one bar")

    engine: _BacktestEngineStub = _BacktestEngineStub(settings, strategy.vt_symbol)
    strategy.cta_engine = engine

    strategy.on_init()
    strategy.inited = True
    strategy.on_start()
    strategy.trading = True

    seen_trade_ids: set[str] = set()
    equity_curve: list[float] = [settings.capital]
    peak: float = settings.capital
    max_drawdown: float = 0.0

    for bar in bar_list:
        engine.next_bar = bar

        # Order of operations matches the official Bar loop: cross live limit
        # orders first, then let the strategy act on the bar and re-queue.
        for trade in engine.fill_pending(strategy):
            if trade.tradeid in seen_trade_ids:
                continue
            seen_trade_ids.add(trade.tradeid)
            _apply_position_and_cost(strategy, trade, settings)
            strategy.on_trade(trade)

        strategy.on_bar(bar)

        equity: float = settings.capital + float(getattr(strategy, "equity", 0.0))
        equity_curve.append(equity)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    return _build_result(strategy, engine, settings, bar_list, max_drawdown, equity_curve)


def _apply_position_and_cost(
    strategy: Any,
    trade: TradeData,
    settings: BacktestSettings,
) -> None:
    """Update net position from a fill and charge its commission."""
    signed: float = trade.volume * (1 if trade.direction is Direction.LONG else -1)
    # CtaEngine.process_trade_event updates position by direction alone,
    # ignoring offset, because a closing trade already carries the opposite
    # direction. Mirroring that here keeps cost basis consistent with live.
    strategy.pos += signed

    # CtaTemplate does not model commissions, so deduction happens here and is
    # folded into the strategy's realized pnl field when it publishes one.
    # Mirrors the official DailyResult accounting: commission on turnover
    # (price x volume x size x rate) plus per-contract slippage cost.
    cost: float = (
        trade.price * trade.volume * settings.rate
        + trade.volume * settings.slippage
    ) * settings.size
    if cost and hasattr(strategy, "realized_pnl"):
        strategy.realized_pnl -= cost


def _build_result(
    strategy: Any,
    engine: _BacktestEngineStub,
    settings: BacktestSettings,
    bar_list: list[BarData],
    max_drawdown: float,
    equity_curve: list[float],
) -> BacktestResult:
    """Assemble the result object from the replayed run."""
    # ``equity`` on a reporting strategy is total pnl (realized plus
    # unrealized), so subtracting it from the realized figure recovers the open
    # position pnl. Strategies that publish only realized pnl report zero.
    closed: float = float(getattr(strategy, "realized_pnl", 0.0))
    reported_equity: float = float(getattr(strategy, "equity", 0.0))
    unrealized: float = reported_equity - closed
    total: float = closed + unrealized
    trade_count: int = len(engine.trades)
    open_position: float = float(strategy.pos)

    winning: int = _count_winning_round_trips(engine.trades)

    return BacktestResult(
        trades=list(engine.trades),
        bars=len(bar_list),
        trade_count=trade_count,
        winning_trades=winning,
        closed_pnl=closed,
        open_position=open_position,
        unrealized_pnl=unrealized,
        total_pnl=total,
        equity=settings.capital + total,
        win_rate=(winning / trade_count) if trade_count else 0.0,
        max_drawdown=max_drawdown,
        sharpe=_sharpe(equity_curve),
    )

def _count_winning_round_trips(trades: list[TradeData]) -> int:
    """Match an opening fill with its closing fill, count profitable pairs."""
    open_trades: dict[Direction, list[TradeData]] = {
        Direction.LONG: [],
        Direction.SHORT: [],
    }

    winning: int = 0

    for trade in trades:
        if trade.offset is Offset.OPEN:
            direction: Direction | None = trade.direction
            if direction is None:
                continue
            open_trades[direction].append(trade)
            continue

        closing_direction: Direction | None = trade.direction
        if closing_direction is None:
            continue
        # A close carries the exit direction, so a SHORT exit belongs to a
        # LONG entry and a LONG exit (cover) belongs to a SHORT entry.
        # Matching by the closing direction itself pairs the opposite round
        # trip and silently corrupts the win rate.
        entry_direction: Direction = (
            Direction.SHORT if closing_direction is Direction.LONG else Direction.LONG
        )
        matches: list[TradeData] = open_trades.get(entry_direction, [])
        if not matches:
            continue

        entry: TradeData = matches.pop(0)
        if entry_direction is Direction.LONG:
            pnl: float = (trade.price - entry.price) * trade.volume
        else:
            pnl = (entry.price - trade.price) * trade.volume
        if pnl > 0:
            winning += 1

    return winning


def _sharpe(equity_curve: list[float]) -> float:
    """Sharpe on bar-to-bar returns, zero when the curve is flat."""
    if len(equity_curve) < 3:
        return 0.0

    returns: list[float] = []
    for previous, current in zip(equity_curve, equity_curve[1:], strict=False):
        if previous == 0:
            continue
        returns.append((current - previous) / abs(previous))

    if len(returns) < 2:
        return 0.0

    mean: float = sum(returns) / len(returns)
    variance: float = sum((r - mean) ** 2 for r in returns) / len(returns)
    deviation: float = variance**0.5
    if deviation == 0:
        return 0.0
    return mean / deviation


def build_bar(
    dt: datetime,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    volume: float = 100.0,
    symbol: str = "rb9999",
    exchange: Exchange = Exchange.SHFE,
) -> BarData:
    """Build a bar for synthetic campaigns and tests."""
    return BarData(
        symbol=symbol,
        exchange=exchange,
        datetime=dt,
        interval=Interval.MINUTE,
        volume=volume,
        turnover=volume * close_price,
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        close_price=close_price,
        gateway_name="BACKTEST",
    )


# Bar sources. A campaign is only as good as its data, so the two realistic
# paths are supported instead of only the synthetic generator.


def load_bars_from_csv(
    path: str,
    symbol: str,
    exchange: Exchange = Exchange.SHFE,
    interval: Interval = Interval.MINUTE,
) -> list[BarData]:
    """Load bars from a CSV export.

    Expected header (extra columns are ignored, missing optional columns fall
    back to a reasonable value):

        datetime,open_price,high_price,low_price,close_price[,volume]

    ``datetime`` must be parseable by :func:`datetime.fromisoformat`, for
    example ``2024-01-02 09:00:00``. Rows are returned in file order: the
    caller is responsible for handing the strategy a sorted series, because an
    ArrayManager silently drops out-of-order bars and the result would look
    like a calm market.
    """

    file_path: Path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"K线文件不存在：{path}")

    bars: list[BarData] = []
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        # Real exports mix separators: a comma header over space data is
        # common. Parse each line by its own delimiter rather than trusting
        # the sniffer on one sample, and keep datetime with its time part.
        raw: str = handle.read()
        rows: list[dict[str, str]] = _parse_table(raw, ["datetime", "open_price", "high_price", "low_price", "close_price", "volume"])

        required = {"datetime", "open_price", "high_price", "low_price", "close_price"}
        available: set[str] = set(rows[0].keys()) if rows else set()
        missing: set[str] = required - available
        if missing:
            raise ValueError(
                f"CSV 缺少必需列 {sorted(missing)}，需要 datetime,"
                "open_price,high_price,low_price,close_price"
            )


        for row in rows:
            dt = datetime.fromisoformat(str(row["datetime"]).strip())
            open_price: float = float(row["open_price"])
            high_price: float = float(row["high_price"])
            low_price: float = float(row["low_price"])
            close_price: float = float(row["close_price"])
            volume: float = float(row.get("volume") or 0.0)

            bars.append(
                BarData(
                    symbol=symbol,
                    exchange=exchange,
                    datetime=dt,
                    interval=interval,
                    volume=volume,
                    turnover=volume * close_price,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    gateway_name="CSV",
                )
            )

    if not bars:
        raise ValueError(f"CSV 中没有可用的K线：{path}")
    return bars


def _parse_table(raw: str, expected_columns: list[str]) -> list[dict[str, str]]:
    """Parse a delimited export whose datetime contains a space.

    The header may use commas, semicolons, tabs or spaces, and the data rows
    may use a different one than the header. Each line is split by whichever
    delimiter it actually contains, then folded back onto the header names.
    """
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return []

    header_names = _split_line(lines[0])
    if not header_names or "open_price" not in header_names:
        header_names = expected_columns[: len(_split_line(lines[0]))]

    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        pieces = _split_line(line)
        if len(pieces) < len(header_names):
            continue
        rows.append(dict(zip(header_names, pieces[: len(header_names)], strict=True)))
    return rows


def _split_line(line: str) -> list[str]:
    """Split one CSV line by its dominant delimiter, keeping datetime whole."""
    for delimiter in (",", ";", "\t"):
        if delimiter in line:
            return [part.strip() for part in line.split(delimiter)]

    parts = line.split()
    if len(parts) > 6:
        # datetime with a space: date, time, open, high, low, close[, volume]
        return [" ".join(parts[0:2])] + parts[2:]
    return parts


def load_bars_from_database(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: datetime,
    end: datetime,
) -> list[BarData]:
    """Load bars through the configured VeighNa database.

    Uses whatever ``vnpy.trader.database.get_database()`` resolves to, so a
    campaign runs against the same bars the live engine would load.
    """
    from vnpy.trader.database import get_database

    database = get_database()
    bars: list[BarData] = database.load_bar_data(symbol, exchange, interval, start, end)
    if not bars:
        raise ValueError(
            f"数据库中没有 {symbol}.{exchange.value} 的 {interval.value} 数据"
            f"（{start} 到 {end}），请先导入历史K线"
        )
    return bars


def parse_exchange(name: str) -> Exchange:
    """Resolve an exchange by member name, case-insensitively."""
    try:
        return Exchange[name.strip().upper()]
    except KeyError as exc:
        valid = ", ".join(member.name for member in Exchange)
        raise ValueError(f"未知交易所 {name}，可选：{valid}") from exc


def parse_interval(name: str) -> Interval:
    """Resolve an interval by member name, case-insensitively."""
    try:
        return Interval[name.strip().upper()]
    except KeyError as exc:
        valid = ", ".join(member.name for member in Interval)
        raise ValueError(f"未知周期 {name}，可选：{valid}") from exc
