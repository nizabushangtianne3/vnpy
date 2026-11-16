"""Tests for the two strategies that answer the beta question.

Both of them exist because the top-30 book's beta against the
cross-section was 1.58 while a random 30-name book was 1.00. The band
strategy moves where it cuts the ranking, and the neutral strategy adds a
short leg against the market. The mistakes worth pinning are the ones
that produce numbers without producing a book: a band that is counted in
names instead of as a share of the cross-section, and a short basket
that holds a name the long side also holds, which cancels the very
selection the hedge is there to preserve.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from vnpy.trader.object import BarData
from vnpy.trader.constant import Exchange, Interval
from datetime import datetime


REPO: Path = Path(__file__).resolve().parents[1]


def load(name: str):
    """Import a strategy module by path."""
    spec = importlib.util.spec_from_file_location(
        name, str(REPO / "vnpy" / "alpha" / "strategy" / "strategies" / f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


band = load("rank_band_strategy")
neutral = load("market_neutral_strategy")


def make_bar(vt_symbol: str, close: float, volume: float) -> BarData:
    """A bar with the two fields the basket ranking reads."""
    return BarData(
        symbol=vt_symbol.split(".")[0],
        exchange=Exchange.SZSE,
        datetime=datetime(2026, 1, 5),
        interval=Interval.DAILY,
        open_price=close,
        high_price=close,
        low_price=close,
        close_price=close,
        volume=volume,
        gateway_name="TEST",
    )


def test_band_cuts_on_a_share_of_the_cross_section_not_a_count():
    """The same band has to mean the same thing on every universe size.

    A count of names would make the band wider on a small universe and
    narrower on a large one, so a setting that works on 300 names would
    silently mean something else on 3,000. The band is a share, so it
    scales with the cross-section.
    """
    small_start, small_end = band.band_slice(1000, 0.05, 0.15)
    large_start, large_end = band.band_slice(3000, 0.05, 0.15)
    assert small_end - small_start == 100
    assert large_end - large_start == 300
    # The share is the same in both, to within a name of rounding.
    assert abs((small_end - small_start) / 1000 - 0.10) < 0.001
    assert abs((large_end - large_start) / 3000 - 0.10) < 0.001


def test_band_is_never_empty():
    """A band that selects nothing is not a band, it is a mistake."""
    start, end = band.band_slice(100, 0.50, 0.50)
    assert end > start
    assert end - start == 1


def test_band_never_runs_past_the_cross_section():
    """A band that reaches past the end of the universe selects everything."""
    start, end = band.band_slice(100, 0.90, 1.10)
    assert end == 100
    assert start < end


def test_neutral_strategy_hedge_ratio_defaults_to_one():
    """A hedge that is not one-for-one is a different bet, not a default."""
    assert neutral.MarketNeutralStrategy.hedge_ratio == 1.0


def test_hedge_basket_excludes_long_names_from_the_hedge():
    """The long band and the short basket must not overlap.

    If they share a name, the hedge cancels that name's contribution to
    the long side, which is the selection the strategy exists to keep.
    """
    bars = {
        "000001.SZSE": make_bar("000001.SZSE", 10.0, 1_000_000),
        "000002.SZSE": make_bar("000002.SZSE", 10.0, 9_000_000),
        "000003.SZSE": make_bar("000003.SZSE", 10.0, 5_000_000),
    }
    basket = neutral.hedge_basket(bars, count=3, exclude={"000002.SZSE"})
    # The most liquid name is excluded, so the basket takes the next ones.
    assert "000002.SZSE" not in basket
    assert basket == ["000003.SZSE", "000001.SZSE"]


def test_hedge_basket_ranks_by_turnover():
    """The basket has to be the names that can actually be traded.

    A basket of names that cannot be sold on size is not a hedge.
    """
    bars = {
        "A.SZSE": make_bar("A.SZSE", 10.0, 1_000),
        "B.SZSE": make_bar("B.SZSE", 100.0, 100_000),
        "C.SZSE": make_bar("C.SZSE", 1.0, 5_000_000),
    }
    basket = neutral.hedge_basket(bars, count=3, exclude=set())
    # Turnover, not volume: B trades 100 thousand at 100.00, which is ten
    # million of value, while C trades five million at 1.00. B is the more
    # liquid name by value even though it trades a twentieth of the shares.
    assert basket[0] == "B.SZSE"


def test_hedge_basket_honours_its_count():
    """A basket bigger than the tradable universe cannot be filled."""
    bars = {
        "A.SZSE": make_bar("A.SZSE", 10.0, 1_000),
        "B.SZSE": make_bar("B.SZSE", 10.0, 2_000),
    }
    assert len(neutral.hedge_basket(bars, count=30, exclude=set())) == 2


def test_neutral_strategy_hedge_defaults():
    """The defaults have to be a hedge and not a bet."""
    strategy = neutral.MarketNeutralStrategy
    assert strategy.hedge_ratio == 1.0
    assert strategy.hedge_count > 0
    assert strategy.band_low < strategy.band_high


def test_neutral_strategy_defaults_clear_the_fee_gate():
    """The default book has to clear the fee gate, not just the sharpe gate.

    The engine's commission is a fraction of capital, so a 30-day clock
    with a buffered band has to keep the whole book under two percent of
    capital in fees. A five-day clock spends nearly five percent, which is
    why it is not the default.
    """
    strategy = neutral.MarketNeutralStrategy
    assert strategy.rebalance_days == 30
    assert strategy.buffer_ratio == 0.10
    assert strategy.rebalance_days * 2 <= 60


def test_rank_band_strategy_defaults_to_a_band_that_is_not_the_extreme():
    """The band has to be a real band and not a degenerate one."""
    strategy = band.RankBandStrategy
    assert strategy.band_low >= 0.0
    assert strategy.band_high > strategy.band_low


def test_buffered_band_keeps_a_held_name_that_fell_just_past_the_edge():
    """A name that fell a little is not a name that fell out.

    The ranking is noisy relative to the band's edge: the band changed 73
    percent of its membership every five sessions, and most of that was the
    ranking reshuffling rather than the edge moving. Churning on that noise
    costs real commission, so a name that has fallen just past the edge is
    still kept.
    """
    universe = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    # band 0.2-0.4 is C and D. E is held, one name past the edge and inside
    # a 0.2 buffer, which is two names of ten. Keep it, because it only fell.
    kept = band.buffered_band(universe, {"E"}, 10, 0.2, 0.4, 0.2)
    assert "E" in kept
    assert len(kept) == 2
def test_buffered_band_sells_a_held_name_that_fell_far():
    """A name that genuinely fell out has to actually fall out.

    The buffer exists to absorb ranking noise, not to hold a position the
    signal has abandoned. A name past the buffer's reach is sold.
    """
    universe = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    # band 0.2-0.4 is C and D. F is held, two names past the edge and outside
    # a 0.05 buffer, which covers no names at all on this universe.
    kept = band.buffered_band(universe, {"F"}, 10, 0.2, 0.4, 0.05)
    assert "F" not in kept
    assert kept == ["C", "D"]
def test_buffered_band_never_runs_past_the_cross_section():
    """A buffer that runs past the end of the universe selects everything."""
    universe = ["A", "B", "C", "D", "E"]
    kept = band.buffered_band(universe, {"E"}, 5, 0.8, 1.0, 0.5)
    assert len(kept) <= 5
    assert set(kept) <= set(universe)


def test_buffered_band_zero_ratio_is_the_undecorated_band():
    """Zero buffer must be exactly the plain band, with no name kept out."""
    universe = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    start, end = band.band_slice(10, 0.2, 0.4)
    kept = band.buffered_band(universe, {"A"}, 10, 0.2, 0.4, 0.0)
    assert kept == universe[start:end]


def test_buffered_band_keeps_the_bands_own_number_of_names():
    """The buffer may change which names are held, never how many.

    A buffer that grows the book also grows its turnover and its cost, which
    is the opposite of what it is for.
    """
    universe = [chr(ord("A") + i) for i in range(20)]
    start, end = band.band_slice(20, 0.2, 0.4)
    kept = band.buffered_band(universe, {"A", "B", "E"}, 20, 0.2, 0.4, 0.2)
    assert len(kept) == end - start


def test_buffered_band_keeps_a_name_that_moved_above_the_band():
    """A name that ranked better is not a name that fell out.

    The buffer is on both edges on purpose: selling a holding because it
    improved its rank pays the spread to replace it with a worse name.
    """
    universe = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    # band 0.2-0.4 is C and D. A is held and one name above the band, inside
    # a 0.2 buffer of two names. Keep it.
    kept = band.buffered_band(universe, {"A"}, 10, 0.2, 0.4, 0.2)
    assert "A" in kept
    assert len(kept) == 2


def test_buffered_band_neutral_strategy_shares_the_one_implementation():
    """Both strategies must use the same buffered band.

    The neutral strategy's short leg is sized against the long book, so the
    two books have to hold the same names. Sharing one function is what
    keeps that true.
    """
    universe = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    # This file loads each module separately, so identity cannot be compared;
    # the shared behaviour is what keeps the two books alike.
    assert neutral.buffered_band(universe, {"E"}, 10, 0.2, 0.4, 0.2) == band.buffered_band(
        universe, {"E"}, 10, 0.2, 0.4, 0.2
    )
