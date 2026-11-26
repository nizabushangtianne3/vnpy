
"""Hold the top and the bottom of the ranking for exactly one session.

The offline ruler found the edge in a book rebuilt every session on the lag-one
return, and the engine's stock strategies cannot express that book. Two of their
mechanisms block it. ``RankBandStrategy`` clears a name's holding age only when
the name is sold, so a name that has been bought once is never buyable again,
which for a daily book turns every session's buys into names it has never owned.
And ``min_days`` defaults to five sessions, so a name cannot be sold the session
after it was bought and the book cannot hold for the one session the edge lives
in.

Neither is a defect in the engine. Both are right for the slow book the other
strategies trade. What the repository needs to measure is a book that holds each
name one session, and this is the strategy for it.

The T+1 rule is respected explicitly. A-share settlement does not allow a name
bought on session T to be sold on session T, so only names that entered on or
before T-1 may be sold. A name therefore spends exactly one session in the book:
bought on T from the T-1 ranking, held through T, sold on T+1 once settlement
allows it. The short leg is the mirror: its names are sold on T-1's ranking and
bought back on T.

Usage is the same as the other strategies::

    python scripts/alpha_beta_backtest.py --strategy daily-spread ...
"""

from __future__ import annotations

from datetime import datetime

from vnpy.alpha.strategy import AlphaStrategy
from vnpy.trader.object import BarData, TradeData

from vnpy.alpha.strategy.strategies.rank_band_strategy import band_slice


class DailySpreadStrategy(AlphaStrategy):
    """Rebuild the head and the tail of the ranking every session."""

    long_rate: float = 0.01       # rank share at the top of the long leg
    short_rate: float = 0.01      # rank share at the bottom of the short leg
    buffer_ratio: float = 0.0     # how far a held name may slide and keep its place
    signal_lag: int = 1           # sessions between the signal used and the bar traded
    capital_ratio: float = 0.95   # share of book value put to work
    min_volume: int = 100         # A-share board lot
    open_rate: float = 0.00025
    close_rate: float = 0.00076
    min_commission: int = 5
    price_add: float = 0.05
    hedge_ratio: float = 0.5      # notional of each leg against book value

    def on_init(self) -> None:
        """Record the session each name entered, so settlement can be enforced."""
        self.entry_session: dict[str, int] = {}
        self.seen_session_dates: list[datetime] = []
        self.holding_days: dict[str, int] = {}
        self.write_log("每日多空策略初始化")

    def on_trade(self, trade: TradeData) -> None:
        """Remember the session a position was opened on, either direction.

        ``on_trade`` fires inside ``execute_trading``, after this session's bar
        has already been appended to the calendar, so ``len`` is the current
        session rather than the one the fill belongs to. Recording the current
        index would make the very next session read the position as un-settled
        forever, which is the state that locked the book into names it could
        never sell. One is subtracted so the fill settles the session after it.
        """
        if trade.volume > 0:
            self.entry_session[trade.vt_symbol] = len(self.seen_session_dates) - 1

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """Rebuild both legs from the ranking, every session."""
        for vt_symbol in list(self.holding_days):
            self.holding_days[vt_symbol] += 1

        bars_datetime = (
            next(iter(bars.values())).datetime.replace(tzinfo=None) if bars else None
        )
        if bars_datetime and (
            not self.seen_session_dates
            or self.seen_session_dates[-1] != bars_datetime
        ):
            self.seen_session_dates.append(bars_datetime)
        session_index: int = len(self.seen_session_dates)

        # A session lag keeps the ranking the session before the bar being
        # traded, which is the only source a close-computed signal can have.
        index = max(session_index - 1 - self.signal_lag, 0)
        target_datetime = self.seen_session_dates[index] if self.signal_lag else None
        last_signal = self.get_signal(target_datetime).sort("signal", descending=True)
        if not last_signal.height:
            return
        total: int = last_signal.height
        universe: list[str] = list(last_signal["vt_symbol"])
        in_universe: set[str] = set(universe)

        long_start, long_end = band_slice(total, 0.0, self.long_rate)
        # The short leg is the tail of the same ranking, so its slice starts
        # where ``short_rate`` leaves off rather than where ``band_slice``
        # starts counting from zero. Calling ``band_slice`` with a low of zero
        # returns the head of the cross-section, which is the long leg, and
        # the two legs would be the same names.
        short_start: int = max(total - int(total * self.short_rate), 0)

        # A buffer lets a held name slide past its edge without being churned.
        # It is measured in rank points rather than as a share of the
        # cross-section, so a one percent band with a one point buffer holds the
        # top two percent rather than the top one point zero five.
        long_slack = int(total * self.buffer_ratio)
        short_slack = int(total * self.buffer_ratio)
        long_hi = min(total, long_end + long_slack)
        short_lo = max(0, short_start - short_slack)

        pos_symbols = [vt for vt, pos in self.pos_data.items() if pos]

        # Settlement: a position opened this session cannot be closed this
        # session, so only names that entered on an earlier session may leave.
        settled = [
            vt for vt in pos_symbols
            if session_index - self.entry_session.get(vt, session_index) >= 1
        ]

        target_long = set(universe[long_start:long_end])
        target_short = set(universe[short_start:])
        long_zone = set(universe[:long_hi]) | target_long
        short_zone = set(universe[short_lo:]) | target_short

        # A held name stays when it is still in the zone its leg covers. The
        # zone is the band plus the buffer, so a name that slid a few rank
        # points out of the band is kept rather than churned, which is what
        # the buffer is for.
        keep = {
            vt for vt in settled
            if (vt in long_zone or vt in short_zone) and vt in in_universe
        }
        held_long = {vt for vt in keep if vt in long_zone}
        held_short = {vt for vt in keep if vt in short_zone}
        leave = {vt for vt in settled if vt not in keep}

        deltas: dict[str, float] = {}
        prices: dict[str, float] = {}

        for vt_symbol in leave:
            volume = self.get_pos(vt_symbol)
            # A short leg is a negative position, so the emptiness test has to
            # accept both signs rather than only a positive holding.
            if volume == 0:
                continue
            bar = bars.get(vt_symbol)
            if not bar:
                continue
            deltas[vt_symbol] = -volume
            prices[vt_symbol] = bar.close_price

        cash: float = self.get_cash_available()
        book_value: float = self.get_holding_value() + cash
        budget: float = book_value * self.capital_ratio

        # New names fill the leg up to its target count, drawn from the whole
        # zone and skipping names already held. Sizing divides the leg's
        # notional across every name the leg will hold, kept ones included,
        # so the leg's total stays at ``hedge_ratio`` whether a name is new or
        # is being carried by the buffer. Dividing by the new names alone
        # makes each order grow as the buffer shrinks the order count: a ten
        # percent buffer left a thirty-name leg holding fifty-five names and
        # paying nearly twice its intended notional.
        long_capacity = max(int(total * self.long_rate), 1)
        short_capacity = max(int(total * self.short_rate), 1)
        long_targets = [
            vt for vt in universe[:long_hi] if vt not in set(pos_symbols)
        ][: max(long_capacity - len(held_long), 0)]
        short_targets = [
            vt for vt in universe[short_lo:] if vt not in set(pos_symbols)
        ][: max(short_capacity - len(held_short), 0)]
        long_book = held_long | set(long_targets)
        short_book = held_short | set(short_targets)
        long_size = budget * self.hedge_ratio / max(len(long_book), 1)
        short_size = budget * self.hedge_ratio / max(len(short_book), 1)
        for vt_symbol in held_long:
            bar = bars.get(vt_symbol)
            if not bar:
                continue
            self.set_target(vt_symbol, target=self._lots(long_size / bar.close_price))
        for vt_symbol in held_short:
            bar = bars.get(vt_symbol)
            if not bar:
                continue
            self.set_target(vt_symbol, target=-self._lots(short_size / bar.close_price))

        # The long leg is bought with cash; the short leg is sold and so does
        # not consume the cash the long leg needs. The engine's cash book
        # releases a short's market value as spendable, which is true for a
        # closed trade and false for a security still owed, and a book whose
        # legs each claim the whole book value runs itself out of cash.
        for vt_symbol in long_targets:
            bar = bars.get(vt_symbol)
            if not bar:
                continue
            volume = self._lots(long_size / bar.close_price)
            if volume > 0:
                deltas[vt_symbol] = volume
                prices[vt_symbol] = bar.close_price
        for vt_symbol in short_targets:
            bar = bars.get(vt_symbol)
            if not bar:
                continue
            volume = self._lots(short_size / bar.close_price)
            if volume > 0:
                deltas[vt_symbol] = -volume
                prices[vt_symbol] = bar.close_price

        for vt_symbol, delta in deltas.items():
            target = self.get_pos(vt_symbol) + delta
            self.set_target(vt_symbol, target=target)

        self.execute_trading(bars, price_add=self.price_add)

    def _lots(self, shares: float) -> int:
        """Round a raw share count down to whole board lots."""
        from vnpy.trader.utility import floor_to

        return int(floor_to(shares, self.min_volume))
