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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from vnpy.trader.constant import Direction, Exchange, Interval, Offset
from vnpy.trader.object import BarData, TradeData
from vnpy.trader.utility import round_to

# A bar-to-bar price jump above this multiple is a unit break in the source
# file, not a market move: no listed instrument reprices that far between
# consecutive bars, and replaying the jump fabricates pnl out of the glitch.
MAX_PRICE_JUMP: float = 50.0


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
    profit_factor: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    curve_dates: list[datetime] = field(default_factory=list)
    round_trips: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    # The cost map travels with the result so a portfolio can reuse the same
    # round trips instead of recomputing them without costs, which would
    # report gross movement and overstate every leg's payoff.
    trade_costs: dict[str, float] = field(default_factory=dict)

    def summary(self) -> dict[str, float | int]:
        """Flat mapping for state files, notifications and assertions."""
        return {
            "bars": self.bars,
            "trade_count": self.trade_count,
            "winning_trades": self.winning_trades,
            "round_trips": self.round_trips,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "open_position": self.open_position,
            "closed_pnl": self.closed_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl,
            "equity": self.equity,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "curve_points": len(self.equity_curve),
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
        self.trade_costs: dict[str, float] = {}
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
    timestamps: list[datetime] = [bar_list[0].datetime]
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
            engine.trade_costs[trade.tradeid] = _apply_position_and_cost(
                strategy, trade, settings
            )
            strategy.on_trade(trade)

        strategy.on_bar(bar)

        equity: float = settings.capital + float(getattr(strategy, "equity", 0.0))
        equity_curve.append(equity)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        timestamps.append(bar.datetime)

    return _build_result(
        strategy,
        engine,
        settings,
        bar_list,
        max_drawdown,
        equity_curve,
        timestamps,
    )


def _apply_position_and_cost(
    strategy: Any,
    trade: TradeData,
    settings: BacktestSettings,
) -> float:
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

    return cost


def _build_result(
    strategy: Any,
    engine: _BacktestEngineStub,
    settings: BacktestSettings,
    bar_list: list[BarData],
    max_drawdown: float,
    equity_curve: list[float],
    timestamps: list[datetime] | None = None,
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

    winning: int = _count_winning_round_trips(engine.trades, engine.trade_costs)
    round_trip_pnls: list[float] = _round_trip_pnls(
        engine.trades, engine.trade_costs
    )
    round_trip_count: int = len(round_trip_pnls)
    gross_profit: float = sum(pnl for pnl in round_trip_pnls if pnl > 0)
    gross_loss: float = -sum(pnl for pnl in round_trip_pnls if pnl < 0)

    return BacktestResult(
        trades=list(engine.trades),
        bars=len(bar_list),
        trade_count=trade_count,
        winning_trades=winning,
        round_trips=round_trip_count,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        closed_pnl=closed,
        open_position=open_position,
        unrealized_pnl=unrealized,
        total_pnl=total,
        equity=settings.capital + total,
        # The win rate is shares won out of positions held, not out of fills.
        # A strategy that scales in with one lot per bar fills five opens to
        # build one position, so dividing wins by fills understates the rate
        # by the number of scale-in legs: real daily data produced a 3.8%
        # win rate for a book whose round trips actually won half the time.
        # Open fills carry no outcome, so they belong in neither numerator
        # nor denominator; the aside on the shared pairing below keeps the
        # round trip count and the pnl list in agreement.
        win_rate=(winning / round_trip_count) if round_trip_count else 0.0,
        max_drawdown=max_drawdown,
        sharpe=annual_sharpe(equity_curve, timestamps),
        profit_factor=profit_factor(engine.trades, engine.trade_costs),
        trade_costs=dict(engine.trade_costs),
        equity_curve=list(equity_curve),
        curve_dates=list(timestamps or []),
    )

def _round_trip_pnls(
    trades: list[TradeData],
    trade_costs: dict[str, float] | None = None,
) -> list[float]:
    """Net pnl of every position batch that was closed, in fill order.

    A round trip is profitable only when the net pnl is positive: gross price
    movement minus the commission and slippage charged on every fill in the
    round trip. Counting gross movement alone reports a win on round trips
    that lose money, which inflates the win rate and hides a strategy that
    pays more in costs than it captures.

    This is the single pairing used by the win rate, the profit factor and the
    payoff ratio, so those figures cannot disagree with each other.

    Scale-in entries belong to one position, not to one round trip each. A
    trend strategy that adds a lot on every new high fills several OPEN
    trades between two exits; pairing each open with a single close splits
    one position into several round trips and leaves most fills unmatched.
    On real daily data that dropped 80% of the fills, so the win rate counted
    a handful of round trips over a denominator of every fill and the profit
    factor described a book the strategy never traded. Entry fills that
    follow one another without the position going flat are therefore
    accumulated into one batch, and the batch is closed as a whole.
    """
    open_trades: dict[Direction, list[TradeData]] = {
        Direction.LONG: [],
        Direction.SHORT: [],
    }
    batch: dict[Direction, list[TradeData]] = {
        Direction.LONG: [],
        Direction.SHORT: [],
    }
    net_pnls: list[float] = []


    for trade in trades:
        if trade.offset is Offset.OPEN:
            direction: Direction | None = trade.direction
            if direction is None:
                continue
            open_trades[direction].append(trade)
            batch[direction].append(trade)
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
        entries: list[TradeData] = batch[entry_direction]
        outstanding: list[TradeData] = open_trades.get(entry_direction, [])
        if not outstanding:
            continue

        if not entries:
            # The entry fills for this batch are gone, so this close belongs
            # to an earlier book: fall back to matching the outstanding opens
            # one by one.
            entry: TradeData = outstanding.pop(0)
            gross: float = _fill_pnl(entry, trade, entry_direction)
            costs: float = _fill_cost(entry, trade, trade_costs)
            net_pnls.append(gross - costs)
            continue

        # Consume the batch's entries in order, keeping the volume that the
        # close actually retires. A close larger than the batch is capped at
        # the batch so the accounting cannot invent exposure.
        remaining: float = float(trade.volume)
        gross = 0.0
        costs = 0.0
        for entry in list(entries):
            if remaining <= 0:
                break
            used: float = min(float(entry.volume), remaining)
            gross += _fill_pnl(entry, trade, entry_direction, used)
            costs += _fill_cost(entry, trade, trade_costs, used)
            remaining -= used
            if used >= float(entry.volume):
                entries.remove(entry)
                if entry in outstanding:
                    outstanding.remove(entry)

        net_pnls.append(gross - costs)

    return net_pnls


def _fill_pnl(
    entry: TradeData,
    exit_trade: TradeData,
    entry_direction: Direction,
    volume: float | None = None,
) -> float:
    """Gross price move of one entry fill against the fill that closes it."""
    size: float = float(entry.volume) if volume is None else volume
    if entry_direction is Direction.LONG:
        return (exit_trade.price - entry.price) * size
    return (entry.price - exit_trade.price) * size


def _fill_cost(
    entry: TradeData,
    exit_trade: TradeData,
    trade_costs: dict[str, float] | None,
    volume: float | None = None,
) -> float:
    """Commission and slippage charged on both legs of one round trip.

    The cost map holds the whole cost of a fill, so a partially closed entry
    is charged only the share of its cost that the closed volume represents.
    """
    if not trade_costs:
        return 0.0
    entry_cost: float = trade_costs.get(entry.tradeid, 0.0)
    exit_cost: float = trade_costs.get(exit_trade.tradeid, 0.0)
    if volume is None or float(entry.volume) <= 0:
        return entry_cost + exit_cost
    share: float = volume / float(entry.volume)
    return entry_cost * share + exit_cost * share


def _count_winning_round_trips(
    trades: list[TradeData],
    trade_costs: dict[str, float] | None = None,
) -> int:
    """Count round trips whose net pnl is positive."""
    pnls: list[float] = _round_trip_pnls(trades, trade_costs)
    return sum(1 for pnl in pnls if pnl > 0)


def gross_split(
    trades: list[TradeData],
    trade_costs: dict[str, float] | None = None,
) -> tuple[float, float]:
    """Total winning pnl and total losing pnl over the round trips.

    A profit factor is a ratio of two sums, so pooling several books requires
    the sums rather than their ratios. Averaging ratios from books of unequal
    size lets a two-round-trip book count as much as a fifty-round-trip one and
    reports a figure that describes neither.
    """
    pnls: list[float] = _round_trip_pnls(trades, trade_costs)
    profit: float = sum(pnl for pnl in pnls if pnl > 0)
    loss: float = -sum(pnl for pnl in pnls if pnl < 0)
    return profit, loss


def profit_factor(
    trades: list[TradeData],
    trade_costs: dict[str, float] | None = None,
) -> float:
    """Gross profit divided by gross loss over the round trips.

    A total pnl of 1000 from one 1000 win and nothing else is a coin flip,
    while 1000 spread over fifty small wins is a repeatable edge, and both
    rank identically when only pnl is ranked. This ratio carries that
    difference, and it is the figure an operator reads to tell the two apart.
    Returns 0.0 when there is nothing to divide.
    """
    pnls: list[float] = _round_trip_pnls(trades, trade_costs)
    gross_profit: float = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss: float = -sum(pnl for pnl in pnls if pnl < 0)
    if gross_loss <= 0:
        return 0.0
    return gross_profit / gross_loss


def annual_sharpe(
    equity_curve: list[float],
    timestamps: list[datetime] | None = None,
) -> float:
    """Sharpe ratio on bar-to-bar returns, annualized by the bar delta.

    Without a time base the ratio is per-sample, so the same strategy scores
    differently on 1-minute and daily bars and the optimizer ends up ranking
    a data frequency instead of a strategy. Timestamps scaled per year turn
    any interval into one comparable scale; without them the ratio stays
    per-sample.
    """
    returns: list[float] = []
    for previous, current in zip(equity_curve, equity_curve[1:], strict=False):
        if previous == 0:
            continue
        returns.append((current - previous) / abs(previous))

    periods_per_year: float | None = _periods_per_year(timestamps, len(returns))
    if periods_per_year is None:
        return _ratio(returns)

    scaled: float = _ratio(returns) * periods_per_year**0.5
    return scaled


def _ratio(returns: list[float]) -> float:
    """Mean over standard deviation of a return series."""
    if len(returns) < 2:
        return 0.0

    mean: float = sum(returns) / len(returns)
    variance: float = sum((r - mean) ** 2 for r in returns) / len(returns)
    deviation: float = variance**0.5
    if deviation == 0:
        return 0.0
    return mean / deviation


def _periods_per_year(timestamps: list[datetime] | None, samples: int) -> float | None:
    """Infer the sampling frequency per year from bar timestamps.

    The equity curve has one point per bar plus its seed value, so the number
    of return intervals is ``len(equity) - 1``. Timestamps are only used when
    they cover that interval count; a shorter list means the curve was built
    without them and no frequency is claimed.
    """
    if not timestamps or samples <= 0 or len(timestamps) <= samples:
        return None

    deltas: list[float] = []
    for previous, current in zip(timestamps, timestamps[1:], strict=False):
        seconds: float = (current - previous).total_seconds()
        if seconds > 0:
            deltas.append(seconds)

    if not deltas:
        return None

    mean_delta: float = sum(deltas) / len(deltas)
    if mean_delta <= 0:
        return None
    return 365.0 * 24.0 * 3600.0 / mean_delta


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


@dataclass
class PortfolioResult:
    """Combined statistics of several strategies run side by side.

    A single instrument's walk-forward result answers whether one edge
    exists. It cannot answer whether the collection survives, because a
    curve made of uncorrelated legs has a lower drawdown than the worst leg
    alone, and that difference is the argument for holding several
    instruments. ``legs`` keeps the individual runs so the total can be
    attributed, which matters because a combined pnl carried entirely by one
    leg is not diversification.
    """

    legs: dict[str, BacktestResult] = field(default_factory=dict)
    dates: list[datetime] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    trade_count: int = 0
    winning_trades: int = 0
    round_trips: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    closed_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    open_position: float = 0.0

    def leg_pnls(self) -> dict[str, float]:
        """Per-instrument total pnl, for attributing the combined figure."""
        return {name: result.total_pnl for name, result in self.legs.items()}

    def summary(self) -> dict[str, float | int | dict[str, float]]:
        """Flat mapping for JSON output and assertions."""
        return {
            "total_pnl": self.total_pnl,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "trade_count": self.trade_count,
            "winning_trades": self.winning_trades,
            "round_trips": self.round_trips,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            "closed_pnl": self.closed_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "open_position": self.open_position,
            "legs": len(self.legs),
            "leg_pnls": self.leg_pnls(),
        }


def run_portfolio_backtest(
    strategies: dict[str, Any],
    bars_by_symbol: Mapping[str, Iterable[BarData]],
    settings: BacktestSettings | None = None,
) -> PortfolioResult:
    """Run one strategy per instrument and combine the equity curves.

    Each leg is independent: it replays its own bar series with its own
    position and cost. Combining them is bookkeeping, so the combined curve
    sums the per-bar pnl of every leg.

    Alignment matters more than it looks. Instruments trade different
    calendars and different daily bar counts, so the combined curve unions
    the dates instead of zipping lists by index: a date one leg did not trade
    is a flat day for that leg, not the end of the series. Walking every date
    any leg saw keeps the combined curve honest about the years it covers.
    """
    if settings is None:
        settings = BacktestSettings()

    per_date_pnl: dict[datetime, float] = {}
    union_dates: set[datetime] = set()
    legs: dict[str, BacktestResult] = {}

    for name, strategy in strategies.items():
        bars: list[BarData] = list(bars_by_symbol[name])
        if not bars:
            # An instrument without bars contributes no pnl. Sending an empty
            # series into the single-strategy path would fail a precondition
            # that belongs to a caller who expects to replay something.
            continue
        result: BacktestResult = run_backtest(strategy, bars, settings)
        legs[name] = result
        union_dates.update(result.curve_dates)
        # The curve starts at capital with one point per bar, so the pnl a bar
        # contributed is its difference against the previous point.
        # Differencing lets two legs sharing a timestamp add their increments
        # rather than adding two running totals.
        running: float = 0.0
        for date, value in zip(
            result.curve_dates, result.equity_curve, strict=False
        ):
            current: float = value - settings.capital
            delta: float = current - running
            if delta:
                per_date_pnl[date] = per_date_pnl.get(date, 0.0) + delta
            running = current

    dates: list[datetime] = sorted(union_dates)
    equity: float = settings.capital
    equity_curve: list[float] = [equity]
    peak: float = equity
    max_drawdown: float = 0.0
    for date in dates:
        equity += per_date_pnl.get(date, 0.0)
        equity_curve.append(equity)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    total_pnl: float = equity - settings.capital
    trade_count: int = sum(result.trade_count for result in legs.values())
    winning_trades: int = sum(result.winning_trades for result in legs.values())
    closed_pnl: float = sum(result.closed_pnl for result in legs.values())
    unrealized_pnl: float = sum(result.unrealized_pnl for result in legs.values())
    open_position: float = sum(result.open_position for result in legs.values())
    pooled_profit: float
    pooled_loss: float
    pooled_profit, pooled_loss = _pooled_gross(legs)
    round_trips: int = sum(result.round_trips for result in legs.values())
    # The combined book holds every leg's round trips, so its win rate and
    # profit factor describe the whole book rather than one leg. A gate that
    # judged the portfolio by a single leg's payoff would grade a book nobody
    # trades, and a single-leg figure would hide a leg losing steadily next
    # to a louder winner.
    win_rate: float = (
        winning_trades / round_trips if round_trips else 0.0
    )
    profit_factor: float = _portfolio_profit_factor(legs)
    # ``annual_sharpe`` reads one timestamp per return interval while the
    # curve carries one extra point for its starting capital, so the series
    # is seeded with the first date.
    sharpe_dates: list[datetime] = [dates[0], *dates] if dates else []
    return PortfolioResult(
        legs=legs,
        dates=dates,
        equity_curve=equity_curve,
        total_pnl=total_pnl,
        max_drawdown=max_drawdown,
        sharpe=annual_sharpe(equity_curve, sharpe_dates),
        trade_count=trade_count,
        winning_trades=winning_trades,
        round_trips=round_trips,
        win_rate=win_rate,
        profit_factor=profit_factor,
        gross_profit=pooled_profit,
        gross_loss=pooled_loss,
        closed_pnl=closed_pnl,
        unrealized_pnl=unrealized_pnl,
        open_position=open_position,
    )


def _pooled_gross(legs: Mapping[str, BacktestResult]) -> tuple[float, float]:
    """Sum the gross profit and gross loss of every leg.

    Summing the per-leg ratios would weight a leg that traded rarely as much
    as one that ran all year, so the round trip sums themselves are pooled
    before dividing. A leg reuses the split computed while it ran, so the
    pooled figures stay consistent with each leg's own profit factor.
    """
    profit: float = sum(result.gross_profit for result in legs.values())
    loss: float = sum(result.gross_loss for result in legs.values())
    return profit, loss


def _portfolio_profit_factor(legs: Mapping[str, BacktestResult]) -> float:
    """Gross profit over gross loss across every leg's round trips.

    Without any loss there is no ratio, matching the single-book convention of
    reporting 0.0 rather than infinity.
    """
    profit, loss = _pooled_gross(legs)
    if loss <= 0:
        return 0.0
    return profit / loss


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
    anchor_price: float = 0.0
    dropped: int = 0
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

            # Anchor on the first price seen, then flag every bar whose price
            # sits a different order of magnitude away from that anchor. An
            # unmarked unit change in the middle of a file is the shape this
            # catches, and the anchor is what keeps the earlier (correct) block
            # from being discarded in favour of a later glitch.
            if anchor_price > 0 and close_price > 0:
                scale: float = close_price / anchor_price
                if scale > MAX_PRICE_JUMP or scale < 1.0 / MAX_PRICE_JUMP:
                    dropped += 1
                    continue
            elif close_price > 0:
                anchor_price = close_price

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
        raise ValueError(
            f"CSV 中没有可用的K线：{path}；已因价格标度突变丢弃 {dropped} 行"
        )

    if dropped:
        # Continuing silently would backtest a series that no longer matches
        # the file the caller pointed at, and the gap would read as data.
        import warnings

        warnings.warn(
            f"{path} 丢弃了 {dropped} 行价格标度突变的K线，"
            "回测行情与文件原始内容已不一致",
            stacklevel=2,
        )
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
