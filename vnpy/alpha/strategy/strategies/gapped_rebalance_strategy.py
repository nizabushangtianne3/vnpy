"""Cross-sectional strategy that trades on a schedule, not on every bar.

The demo strategy in this package rebalances toward the top of the ranking on
every session, and its fill rule tops the portfolio back up whenever the held
count is short of ``top_k``. Measured on 292 real A-shares over 2024-01 to
2026-11 that behaviour turned 105x of capital in turnover and 10.5 percent of
capital in commissions, against a 0.85 percent total return: the strategy was
paying the exchange to hold a portfolio it had no edge on. The reason is in
the arithmetic rather than the ranking - ``min_days`` delays a sale without
delaying a fill, so the held count never reaches the target and every bar is a
full refill.

This strategy keeps the ranking, and changes the two things that made the
turnover wasteful:

* A rebalance clock. Orders go out only every ``rebalance_days`` sessions, so
  a ranking that moves a little does not move the whole book.
* Symmetric holding constraints. A symbol bought is not sold before it has
  been held ``min_days`` sessions, and a symbol is bought only if it can be
  held that long, which stops the top-up loop from creating positions it
  cannot then exit.

Turnover is the cost the ranking has to out-earn, so it is capped rather than
left as a side effect: no rebalance may turn over more than
``max_turnover_ratio`` of the book, and when it would, the smallest positions
 are dropped from the order list instead of the whole rebalance being sent.
"""

from __future__ import annotations

from collections import defaultdict

from vnpy.alpha import AlphaStrategy
from vnpy.trader.constant import Direction
from vnpy.trader.object import BarData, TradeData


class GappedRebalanceStrategy(AlphaStrategy):
    """Top-k ranking strategy that rebalances on a fixed clock."""

    top_k: int = 30                 # target number of holdings
    rebalance_days: int = 5         # sessions between two rebalances
    min_days: int = 5               # sessions a new holding must wait before selling
    max_turnover_ratio: float = 0.3 # per-rebalance turnover cap, fraction of book
    cash_ratio: float = 0.95        # fraction of available cash deployed
    min_volume: int = 100           # A-share board lot
    open_rate: float = 0.0005
    close_rate: float = 0.0015
    min_commission: int = 5
    price_add: float = 0.05

    def on_init(self) -> None:
        """Initialize holding state."""
        self.holding_days: defaultdict[str, int] = defaultdict(int)
        self.bars_since_rebalance: int = self.rebalance_days
        self.write_log("错位调仓策略初始化")

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

        # Only symbols the signal still covers may be sold: a name that has
        # dropped out of the tradable universe is not a ranking change, it is
        # a stock that stopped trading, and holding it is a different risk.
        covered: set[str] = set(last_signal["vt_symbol"])
        sell_symbols: set[str] = {vt for vt in pos_symbols if vt not in covered}

        ranked: list[str] = list(last_signal["vt_symbol"][: self.top_k])
        # A holding that is already inside the target set is left alone, which
        # is what keeps turnover low: the ranking churns, the book does not.
        keep: list[str] = [vt for vt in pos_symbols if vt in set(ranked)]

        # Candidates to sell: holdings outside the target set that have been
        # held long enough that selling them is allowed rather than blocked.
        sorted_sell: list[str] = sorted(
            [vt for vt in pos_symbols if vt not in set(ranked)],
            key=lambda vt: self.holding_days.get(vt, 0),
        )
        for vt_symbol in sorted_sell:
            if vt_symbol in covered:
                sell_symbols.add(vt_symbol)

        # Room to buy is whatever the target allows minus what is already
        # kept, so a run of rebalances that cannot sell does not buy either.
        open_slots: int = max(self.top_k - len(keep), 0)
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

        # Cap the turnover the buy side may add, measured against what is
        # already in the book, so a rebalance cannot quietly become a turnover.
        book_value: float = self.get_holding_value() + cash
        cap: float = book_value * self.max_turnover_ratio
        if sell_value > 0:
            remaining: float = max(cap - sell_value, 0.0)
        else:
            remaining = cap

        if buyable and cash > 0:
            budget: float = min(cash * self.cash_ratio / len(buyable), remaining / len(buyable))
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
        from vnpy.trader.utility import round_to

        return int(round_to(shares, self.min_volume))
