"""The derived tick feed must never emit a price it cannot justify.

Three invariants carry the whole execution path. A limit order must not fill
without a tick that traded through its price, otherwise the loop books fills
that never happened. A bar's price scale must be resolved from the contract
rather than assumed, because assuming 100 misprices gold by two magnitudes
and soybean meal by one. And a restart must not replay a session, because
replaying would double book every fill.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from vnpy.trader.constant import Exchange
from vnpy.trader.object import ContractData, Product

from vnpy.autonomous.cursor import FeedCursor, load_cursor, save_cursor
from vnpy.autonomous.feed import (
    MinuteBar,
    DerivedFeed,
    _align,
    build_tick,
    price_symbol,
    resolve_scale,
    secid_for,
)


def _contract(pricetick: float, size: int = 10) -> ContractData:
    return ContractData(
        gateway_name="TEST",
        symbol="m2705",
        exchange=Exchange.DCE,
        name="m2705",
        product=Product.FUTURES,
        size=size,
        pricetick=pricetick,
    )


def _au_contract() -> ContractData:
    """Shanghai gold, which already quotes in yuan."""
    return ContractData(
        gateway_name="TEST",
        symbol="au2612",
        exchange=Exchange.SHFE,
        name="au2612",
        product=Product.FUTURES,
        size=1000,
        pricetick=0.02,
    )


def _if_contract() -> ContractData:
    """A stock index, which quotes in index points."""
    return ContractData(
        gateway_name="TEST",
        symbol="IF2612",
        exchange=Exchange.CFFEX,
        name="IF2612",
        product=Product.FUTURES,
        size=300,
        pricetick=0.2,
    )


def _shfe_contract(pricetick: float, size: int = 10) -> ContractData:
    """A second contract on another exchange, so a watchlist spans two."""
    return ContractData(
        gateway_name="TEST",
        symbol="hc2705",
        exchange=Exchange.SHFE,
        name="hc2705",
        product=Product.FUTURES,
        size=size,
        pricetick=pricetick,
    )


def _bar(close: float, low: float, high: float) -> MinuteBar:
    return MinuteBar(
        time_text="2026-11-13 09:30",
        open_raw=close,
        close_raw=close,
        high_raw=high,
        low_raw=low,
        volume=100.0,
        turnover=1000.0,
    )


def test_secid_uses_the_resolved_exchange_market() -> None:
    """A wrong market prefix silently returns nothing; the map is explicit."""
    assert secid_for("m2705", Exchange.DCE) == "114.m2705"
    assert secid_for("IF2612", Exchange.CFFEX) == "220.IF2612"
    with pytest.raises(ValueError):
        secid_for("600519", Exchange.SSE)


def test_scale_is_declared_per_symbol() -> None:
    """Soybean meal arrives as tenths and Shanghai gold already in yuan.

    Both shapes were read from live responses. A fixed divisor would price
    gold 100x short and soybean meal 10x short, which silently corrupts every
    position value and every risk limit derived from it.
    """
    assert resolve_scale(_bar(2829.0, 2828.0, 2830.0), _contract(1.0)) == 10.0
    assert resolve_scale(_bar(845.66, 845.60, 845.70), _au_contract()) == 1.0
    assert resolve_scale(_bar(4040.0, 4038.0, 4042.0), _if_contract()) == 1.0


def test_price_symbol_strips_the_delivery_month() -> None:
    """m2705 and m9999 scale identically; both must reach soybean meal's "m"."""
    assert price_symbol("m2705") == "m"
    assert price_symbol("m9999") == "m"
    assert price_symbol("IF2612") == "if"
    assert price_symbol("au2612") == "au"


def test_unknown_symbol_falls_back_to_the_bar_shape() -> None:
    """A contract outside the table keeps the bar's reading, not a guess."""
    contract: ContractData = ContractData(
        gateway_name="TEST",
        symbol="zz9999",
        exchange=Exchange.DCE,
        name="zz9999",
        product=Product.FUTURES,
        size=10,
        pricetick=1.0,
    )
    assert resolve_scale(_bar(845.66, 845.0, 846.0), contract) == 1.0
    assert resolve_scale(_bar(141027.0, 141000.0, 141100.0), contract) == 100.0


def test_bar_shape_reports_its_own_scale() -> None:
    """A bar carrying decimals is already priced; a big integer is not."""
    assert _bar(845.66, 845.0, 846.0).effective_scale == 1.0
    assert _bar(2829.0, 2828.0, 2830.0).effective_scale == 10.0
    assert _bar(141027.0, 141000.0, 141100.0).effective_scale == 100.0


def test_tick_bid_and_ask_come_from_the_bar_range() -> None:
    """A buy limit fills only when the minute traded through its price."""
    # Soybean meal arrives in tenths, so raw 2833 is 283.3 yuan and a 1.0 tick
    # grid rounds it to 283.0. The live quote endpoint agreed: its raw 2829
    # is the same factor, so the bar and the tick describe one price.
    tick = build_tick(_bar(2833.0, 2828.0, 2836.0), _contract(1.0))

    assert tick is not None
    assert tick.last_price == pytest.approx(283.0)
    # raw 2828 -> 282.8, rounded onto the 1.0 grid to 283.0, and raw 2836 ->
    # 283.6 also snaps to 284.0. The range is preserved as real prices.
    assert tick.bid_price_1 == pytest.approx(283.0)
    assert tick.ask_price_1 == pytest.approx(284.0)
    assert tick.high_price == pytest.approx(284.0)
    assert tick.low_price == pytest.approx(283.0)
    # The tick must belong to the bar's minute, never run ahead of the clock.
    assert tick.datetime == datetime(2026, 11, 13, 9, 30)


def test_tick_is_none_when_the_bar_cannot_be_timed() -> None:
    """A mistimed tick is a fake fill waiting to happen."""
    bar: MinuteBar = _bar(2833.0, 2828.0, 2836.0)
    bar.time_text = "not-a-timestamp"
    assert build_tick(bar, _contract(1.0)) is None


def test_align_snaps_to_the_tick_grid() -> None:
    """A price off the grid is a price the exchange would reject."""
    assert _align(2828.4, 0.5) == 2828.5
    assert _align(2828.2, 0.5) == 2828.0
    assert _align(845.666, 0.02) == pytest.approx(845.66)


def test_cursor_never_replays_a_bar() -> None:
    """A restart must continue forward, not book the same fills twice."""
    cursor: FeedCursor = FeedCursor()
    bar: MinuteBar = _bar(2833.0, 2828.0, 2836.0)

    assert cursor.seen(bar.time_text) is False
    cursor.advance(bar)
    assert cursor.seen(bar.time_text) is True
    assert cursor.seen("2026-11-13 09:29") is True
    assert cursor.seen("2026-11-13 09:31") is False


def test_cursor_compares_on_the_minute_not_the_string() -> None:
    """A trailing :00 denotes the same minute and must not re-emit."""
    cursor: FeedCursor = FeedCursor()
    cursor.advance(_bar(2833.0, 2828.0, 2836.0))
    cursor.last_timestamp = "2026-11-13 09:30"
    assert cursor.seen("2026-11-13 09:30:00") is True


def test_cursor_survives_a_round_trip(tmp_path: Path) -> None:
    """A crash mid-run must not lose the bars already fetched."""
    first: FeedCursor = FeedCursor()
    first.advance(_bar(2833.0, 2828.0, 2836.0))
    save_cursor(tmp_path, first)

    restored: FeedCursor = load_cursor(tmp_path)
    assert restored.last_timestamp == "2026-11-13 09:30"
    assert restored.seen("2026-11-13 09:30")


def test_cursor_falls_back_to_empty_when_unreadable(tmp_path: Path) -> None:
    """A corrupt cursor must not crash the feed at startup."""
    (tmp_path / "emfeed_cursor.json").write_text("{not json", encoding="utf-8")
    assert load_cursor(tmp_path).last_timestamp == ""


def test_feed_skips_contracts_it_cannot_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """One missing contract must not stop the rest of the watchlist."""
    from vnpy.event import EventEngine

    feed = DerivedFeed(
        EventEngine(),
        {"m2705.DCE": _contract(1.0), "hc2705.SHFE": _shfe_contract(1.0)},
        Path("."),
    )

    def flaky(symbol: str, exchange: Exchange) -> list[MinuteBar]:
        if symbol == "m2705":
            raise ValueError("no minute bars for 114.m2705")
        return [_bar(3030.0, 3028.0, 3032.0)]

    monkeypatch.setattr("vnpy.autonomous.feed.fetch_minute_bars", flaky)

    ticks: list[object] = feed.poll_once()

    assert len(ticks) == 1
    assert feed.last_error.startswith("m2705.DCE")


def test_feed_honours_the_catchup_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long outage must not bury the process in stale ticks."""
    from vnpy.event import EventEngine

    feed = DerivedFeed(
        EventEngine(),
        {"m2705.DCE": _contract(1.0)},
        Path("."),
        max_catchup=2,
    )
    bars: list[MinuteBar] = [
        MinuteBar(
            time_text=f"2026-11-13 09:3{i}",
            open_raw=2830.0,
            close_raw=2830.0,
            high_raw=2832.0,
            low_raw=2828.0,
            volume=1.0,
            turnover=1.0,
        )
        for i in range(6)
    ]
    monkeypatch.setattr("vnpy.autonomous.feed.fetch_minute_bars", lambda *a: bars)

    ticks: list[object] = feed.poll_once()

    assert len(ticks) == 2
