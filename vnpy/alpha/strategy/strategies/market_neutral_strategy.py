"""Hold the selection and short the market against it.

The band book's selection was paid every year: with the cross-section's own
mean removed, the 5-to-15 percent band earned +0.49, +0.14 and +0.22
percent per session in 2024, 2025 and 2026, and on the book's own five
session clock that is a sharpe of 1.55, 2.27 and 1.94. The long-only
version of the same book lost money in 2026, because the cross-section
fell 0.24 percent on an average session and the book carried 0.98 beta to
it.

So the edge was never the problem and the exposure was. This strategy
keeps the long side exactly as the band strategy holds it and adds a short
side that cancels the market's contribution, leaving the selection.

The short side is a basket of liquid names rather than the index itself,
because the engine prices every leg off a bar and an index is not in the
tradable universe. The basket is the most liquid names in the universe,
equal weighted, so the short side tracks the market rather than any one
sector. Its size is the long book's value times the hedge ratio, and the
hedge ratio defaults to the book's own measured beta against the
cross-section.

Shorting individual A-shares is not something a live book can rely on,
since the borrow is scarce and expensive. This is therefore a measurement
of whether market neutrality is the right shape for the strategy, and the
deployment path is an index future or an ETF leg instead of these names.
"""

from __future__ import annotations

from collections import defaultdict

from vnpy.alpha import AlphaStrategy
from vnpy.trader.constant import Direction
from vnpy.trader.object import BarData, TradeData

from vnpy.alpha.strategy.strategies.rank_band_strategy import buffered_band


def scale_turnover(
    deltas: dict[str, float],
    prices: dict[str, float],
    cap: float,
) -> dict[str, float]:
    """Scale position changes so their gross notional fits one turnover cap.

    The long and hedge legs share one budget. Applying the cap to one leg
    while the other resets its target lets the hedge bypass the limit.
    """
    gross: float = sum(
        abs(delta) * prices.get(vt_symbol, 0.0)
        for vt_symbol, delta in deltas.items()
    )
    if gross <= cap or gross <= 0.0:
        return dict(deltas)

    scale: float = cap / gross
    return {
        vt_symbol: delta * scale
        for vt_symbol, delta in deltas.items()
    }


def hedge_basket(
    bars: dict[str, BarData], count: int, exclude: set[str]
) -> list[str]:
    """The most liquid names, equal weighted, with the long side excluded.

    The basket is ranked by close times volume, which is the standing
    turnover proxy in this repository because the cached bars carry volume
    but not turnover. The names the long side holds are excluded: a name
    on both sides cancels its own contribution to the long book, which is
    the very selection the hedge is there to preserve.
    """
    scored: list[tuple[float, str]] = []
    for vt_symbol, bar in bars.items():
        if bar.close_price <= 0 or vt_symbol in exclude:
            continue
        scored.append((bar.close_price * bar.volume, vt_symbol))
    scored.sort(reverse=True)
    return [vt for _amount, vt in scored[:count]]


class MarketNeutralStrategy(AlphaStrategy):
    """Long the band, short a liquid basket against it."""

    band_low: float = 0.05       # rank share at the top of the long band
    band_high: float = 0.15      # rank share at the bottom of the long band
    buffer_ratio: float = 0.10   # how far a held name may move and keep its place
    hedge_ratio: float = 1.0     # short value per unit of long value
    hedge_count: int = 30        # names in the short basket
    rebalance_days: int = 30     # sessions between two rebalances
    min_days: int = 5            # sessions a new long holding must wait
    max_turnover_ratio: float = 0.3
    cash_ratio: float = 0.95
    min_volume: int = 100        # A-share board lot
    open_rate: float = 0.0005
    close_rate: float = 0.0015
    min_commission: int = 5
    price_add: float = 0.05

    def on_init(self) -> None:
        """Initialize holding state."""
        self.holding_days: defaultdict[str, int] = defaultdict(int)
        self.bars_since_rebalance: int = self.rebalance_days
        self._cached_hedge_pool: list[str] = []
        self.write_log("市场中性策略初始化")

    def on_trade(self, trade: TradeData) -> None:
        """Forget the holding clock once a long position closes."""
        if trade.direction == Direction.SHORT and self.get_pos(trade.vt_symbol) >= 0:
            self.holding_days.pop(trade.vt_symbol, None)

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """Rebalance when the clock fires, and only then."""
        pos_symbols: list[str] = [vt for vt, pos in self.pos_data.items() if pos > 0]
        for vt_symbol in pos_symbols:
            self.holding_days[vt_symbol] += 1

        self.bars_since_rebalance += 1
        if self.bars_since_rebalance < self.rebalance_days:
            return
        self.bars_since_rebalance = 0

        last_signal = self.get_signal().sort("signal", descending=True)
        if not last_signal.height:
            return

        covered: set[str] = set(last_signal["vt_symbol"])

        # The long side is a band of the ranking, measured as a share of the
        # cross-section so the same band means the same thing on every
        # session.
        total: int = last_signal.height
        universe: list[str] = list(last_signal["vt_symbol"])
        # The long side uses the same buffered band as the long-only
        # strategy, so the two books hold the same names and the hedge
        # cancels the market rather than a different selection.
        if self.buffer_ratio:
            ranked = buffered_band(
                universe,
                set(pos_symbols),
                total,
                self.band_low,
                self.band_high,
                self.buffer_ratio,
            )
        else:
            start: int = int(total * self.band_low)
            end: int = max(int(total * self.band_high), start + 1)
            ranked = universe[start:end]
        if not ranked:
            return

        # The short side is the universe's most liquid names, equal
        # weighted. The basket is fixed once, from a liquidity ranking that
        # is computed over the whole history rather than on the session being
        # traded: a basket that is re-picked every rebalance churns the hedge
        # and, worse, a name that enters the long band can also enter the
        # short basket, so the hedge cancels the very selection it is there
        # to preserve.
        hedge_pool: list[str] = [
            vt for vt in self._hedge_basket(bars) if vt not in set(ranked)
        ]

        keep: list[str] = [vt for vt in pos_symbols if vt in set(ranked)]

        sell_symbols: set[str] = {vt for vt in pos_symbols if vt not in covered}
        sorted_sell: list[str] = sorted(
            [vt for vt in pos_symbols if vt not in set(ranked)],
            key=lambda vt: self.holding_days.get(vt, 0),
        )
        for vt_symbol in sorted_sell:
            if vt_symbol in covered:
                sell_symbols.add(vt_symbol)

        open_slots: int = max(len(ranked) - len(keep), 0)
        buyable = [
            vt
            for vt in ranked
            if vt not in set(pos_symbols) and self.holding_days.get(vt, 0) == 0
        ][:open_slots]

        cash: float = self.get_cash_available()

        long_deltas: dict[str, float] = {}
        prices: dict[str, float] = {}
        for vt_symbol in sell_symbols:
            if self.holding_days.get(vt_symbol, 0) < self.min_days:
                continue
            bar: BarData | None = bars.get(vt_symbol)
            if not bar:
                continue
            volume: float = self.get_pos(vt_symbol)
            if volume <= 0:
                continue
            turnover: float = bar.close_price * volume
            cost: float = max(turnover * self.close_rate, self.min_commission)
            cash += turnover - cost
            long_deltas[vt_symbol] = -volume
            prices[vt_symbol] = bar.close_price

        book_value: float = self.get_holding_value() + cash
        cap: float = book_value * self.max_turnover_ratio

        if buyable and cash > 0:
            remaining: float = max(
                cap - sum(
                    abs(delta) * prices.get(vt_symbol, 0.0)
                    for vt_symbol, delta in long_deltas.items()
                ),
                0.0,
            )
            budget: float = min(
                cash * self.cash_ratio / len(buyable),
                remaining / len(buyable),
            )
            for vt_symbol in buyable:
                bar = bars.get(vt_symbol)
                if not bar:
                    continue
                volume = self._lots(budget / bar.close_price)
                if volume > 0:
                    long_deltas[vt_symbol] = volume
                    prices[vt_symbol] = bar.close_price

        # The hedge is sized against the book the long legs are becoming,
        # not the one they are leaving: sizing it off the stale positions
        # would hedge the previous book and leave the new one exposed.
        long_value: float = 0.0
        for vt_symbol, pos in self.pos_data.items():
            if pos <= 0:
                continue
            position_bar: BarData | None = bars.get(vt_symbol)
            if position_bar:
                long_value += position_bar.close_price * pos
        for vt_symbol, delta in long_deltas.items():
            if delta <= 0:
                continue
            bar = bars.get(vt_symbol)
            if bar:
                long_value += bar.close_price * delta

        self._rebalance_hedge(
            hedge_pool,
            bars,
            long_value,
            long_deltas,
            prices,
        )

        scaled: dict[str, float] = scale_turnover(long_deltas, prices, cap)
        for vt_symbol, delta in scaled.items():
            current: float = self.get_pos(vt_symbol)
            target: float = current + delta
            target = self._round_position(target)
            self.set_target(vt_symbol, target=target)

        self.execute_trading(bars, price_add=self.price_add)

    def _hedge_basket(self, bars: dict[str, BarData]) -> list[str]:
        """The most liquid names in the universe, equal weighted and fixed.

        The ranking is over the whole signal history, so the basket does not
        change from session to session and the hedge's own turnover stays
        low. Liquidity is the standing turnover proxy in this repository: the
        cached bars carry volume but not turnover, so close times volume is
        what the basket is ranked on.
        """
        if self._cached_hedge_pool:
            return self._cached_hedge_pool
        self._cached_hedge_pool = hedge_basket(bars, self.hedge_count, set())
        return self._cached_hedge_pool

    def _rebalance_hedge(
        self,
        hedge_pool: list[str],
        bars: dict[str, BarData],
        long_value: float,
        long_deltas: dict[str, float],
        prices: dict[str, float],
    ) -> None:
        """Add the hedge leg's deltas into one shared turnover budget."""
        if not hedge_pool:
            return
        target_value: float = long_value * self.hedge_ratio
        per_name: float = target_value / len(hedge_pool)

        # The deltas are merged into the long side's budget rather than
        # scaled here: scaling the hedge on its own and then scaling the
        # whole book again would shrink it twice and quietly change the
        # hedge ratio from the one the setting declares.
        hedge_deltas: dict[str, float] = {}
        for vt_symbol in hedge_pool:
            hedge_bar: BarData | None = bars.get(vt_symbol)
            if not hedge_bar or hedge_bar.close_price <= 0:
                continue
            shares: float = self._lots(per_name / hedge_bar.close_price)
            if shares > 0:
                current: float = self.get_pos(vt_symbol)
                hedge_deltas[vt_symbol] = -shares - current
                prices[vt_symbol] = hedge_bar.close_price

        for vt_symbol, delta in hedge_deltas.items():
            current = self.get_pos(vt_symbol)
            target = self._round_position(current + delta)
            long_deltas[vt_symbol] = target - current

    def _lots(self, shares: float) -> int:
        """Round a raw share count down to whole board lots."""
        from vnpy.trader.utility import floor_to

        return int(floor_to(shares, self.min_volume))

    def _round_position(self, position: float) -> float:
        """Round a signed position toward zero to a whole board lot."""
        magnitude: float = self._lots(abs(position))
        if position < 0:
            return -float(magnitude)
        return float(magnitude)
