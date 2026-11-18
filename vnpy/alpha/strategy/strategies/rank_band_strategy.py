"""Hold a band of the ranking instead of its extreme, to carry less beta.

The top-30 book's beta against the cross-section is 1.58, while a random
30-name book is 1.00. That extra 0.58 is not the market: it is the
selection's own leverage, because the extreme names of a 3,000-name
ranking move further from the cross-section's average than the average
does. On a session where the cross-section fell, that leverage turned a
selection that was still paying +0.28% into a book that earned +0.05%.

This strategy keeps the ranking and moves where it cuts it. A band is a
share of the cross-section rather than a count of names, so the same band
means the same thing on every session and on every universe size. The
band's beta falls as the band widens and moves down the ranking, and the
table in ``docs/alpha_pilot_findings.md`` is what says which band still
earns its keep.

Two things are kept from ``GappedRebalanceStrategy`` because they are what
make a book payable: the rebalance clock, so a ranking that moves a little
does not move the whole book, and the turnover cap, so a rebalance cannot
quietly become a turnover.
"""

from __future__ import annotations

from collections import defaultdict

from vnpy.alpha import AlphaStrategy
from vnpy.trader.constant import Direction
from vnpy.trader.object import BarData, TradeData


def band_slice(total: int, low: float, high: float) -> tuple[int, int]:
    """The slice of a cross-section of ``total`` names that a band covers.

    The band is a share of the cross-section rather than a count of names,
    so the same setting means the same thing on a 300-name universe and on
    a 3,000-name one. A count would quietly widen the band as the universe
    grew, and the setting that was tuned on one would mean something else
    on the other.

    The slice is always at least one name wide, because a band that
    selects nothing is not a band, it is a mistake.
    """
    start: int = int(total * low)
    end: int = max(int(total * high), start + 1)
    return start, min(end, total)


def buffered_band(
    ranked: list[str],
    held: set[str],
    total: int,
    low: float,
    high: float,
    buffer_ratio: float,
) -> list[str]:
    """The band, with held names kept while they stay near its edges.

    The ranking is noisy relative to the edge: the band changed 73 percent
    of its membership every five sessions even though the signal's daily
    IC is stable, so most of that churn was the ranking reshuffling rather
    than the edge moving. At 48 rebalances a year that is 35 times capital
    of turnover, and every cost is charged on it.

    A held name keeps its place while it is inside the band or within
    ``buffer_ratio`` of either edge. A name that merely moved across an
    edge is neither bought nor sold; one that has genuinely left the
    buffered region is sold. The book keeps the band's own number of names,
    so hysteresis does not turn the book into a larger, more expensive one.

    The buffer is a share of the cross-section rather than a rank count, so
    it means the same thing on every universe size.
    """
    start, end = band_slice(total, low, high)
    if not buffer_ratio or not held:
        return list(ranked[start:end])

    target: int = end - start
    lower: int = max(start - int(total * buffer_ratio), 0)
    upper: int = min(end + int(total * buffer_ratio), total)

    # Core holdings come first. Buffer holdings follow, nearest to the core
    # on either side, so a name that moved a little is preferred to one that
    # has already travelled far from the selection.
    above = list(reversed(ranked[lower:start]))
    below = ranked[end:upper]
    keep: list[str] = [vt for vt in ranked[start:end] if vt in held]
    for vt_symbol in above + below:
        if len(keep) >= target:
            break
        if vt_symbol in held and vt_symbol not in keep:
            keep.append(vt_symbol)

    # Vacancies go to the best names available, with the core preferred.
    for vt_symbol in ranked[start:end] + above + below:
        if len(keep) >= target:
            break
        if vt_symbol not in keep:
            keep.append(vt_symbol)

    return sorted(keep[:target], key=ranked.index)


class RankBandStrategy(AlphaStrategy):
    """Hold a share of the ranking's head, rebalanced on a fixed clock."""

    band_low: float = 0.0         # rank share at the top of the band
    band_high: float = 0.10       # rank share at the bottom of the band
    buffer_ratio: float = 0.0     # how far a held name may fall and keep its place
    rebalance_days: int = 5       # sessions between two rebalances
    min_days: int = 5             # sessions a new holding must wait before selling
    max_turnover_ratio: float = 0.3
    cash_ratio: float = 0.95
    min_volume: int = 100         # A-share board lot
    open_rate: float = 0.0005
    close_rate: float = 0.0015
    min_commission: int = 5
    price_add: float = 0.05

    def on_init(self) -> None:
        """Initialize holding state."""
        self.holding_days: defaultdict[str, int] = defaultdict(int)
        self.bars_since_rebalance: int = self.rebalance_days
        self.write_log("排名区间策略初始化")

    def on_trade(self, trade: TradeData) -> None:
        """Forget the holding clock once a position closes."""
        if trade.direction == Direction.SHORT:
            self.holding_days.pop(trade.vt_symbol, None)

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """Rebalance when the clock fires, and only then."""
        pos_symbols: list[str] = [vt for vt, pos in self.pos_data.items() if pos]
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

        # The band is a share of the cross-section, so the same band means
        # the same thing whether the universe is 300 names or 3,000.
        total: int = last_signal.height
        start, end = band_slice(total, self.band_low, self.band_high)
        universe: list[str] = list(last_signal["vt_symbol"])

        # With a buffer the target is the band plus the held names that have
        # fallen just past its lower edge, which is what keeps a book that is
        # already right from being churned by the ranking's noise.
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
            ranked = universe[start:end]
        if not ranked:
            return

        # A holding that is already inside the target set is left alone.
        keep: list[str] = [vt for vt in pos_symbols if vt in set(ranked)]

        # Only symbols the signal still covers may be sold.
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

        sell_value: float = 0.0
        for vt_symbol in sell_symbols:
            if self.holding_days.get(vt_symbol, 0) < self.min_days:
                continue
            bar: BarData | None = bars.get(vt_symbol)
            if not bar:
                continue
            volume: float = self.get_pos(vt_symbol)
            if volume <= 0:
                continue
            self.set_target(vt_symbol, target=0)
            turnover: float = bar.close_price * volume
            cost: float = max(turnover * self.close_rate, self.min_commission)
            cash += turnover - cost
            sell_value += turnover

        book_value: float = self.get_holding_value() + cash
        cap: float = book_value * self.max_turnover_ratio
        if sell_value > 0:
            remaining: float = max(cap - sell_value, 0.0)
        else:
            remaining = cap

        if buyable and cash > 0:
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
                    self.set_target(vt_symbol, volume)

        self.execute_trading(bars, price_add=self.price_add)

    def _lots(self, shares: float) -> int:
        """Round a raw share count down to whole board lots."""
        from vnpy.trader.utility import floor_to

        return int(floor_to(shares, self.min_volume))
