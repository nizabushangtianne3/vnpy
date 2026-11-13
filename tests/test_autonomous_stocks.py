
from __future__ import annotations

import pytest

from vnpy.trader.constant import Exchange, Interval

from vnpy.autonomous.stocks import (
    MAX_BAR_JUMP,
    _kline_urls,
    _parse_sina_rows,
    _sina_symbol,
    exchange_for_secid,
    parse_klines,
)


def _bar(day: int, close: float) -> str:
    """Render one kline row the way Eastmoney prints it."""
    return f"2026-01-{day:02d},{close},{close},{close},{close},1000,{close * 1000}"


def test_parse_klines_reads_prices_in_yuan() -> None:
    """A-share klines arrive already in yuan, so no dividing happens."""
    bars = parse_klines([_bar(2, 1410.27), _bar(5, 1388.11)])
    assert [bar.close_price for bar in bars] == [1410.27, 1388.11]
    assert all(bar.interval == Interval.DAILY for bar in bars)
    assert all(bar.exchange == Exchange.SSE for bar in bars)


def test_a_zero_prefixed_secid_is_shenzhen() -> None:
    """The market prefix belongs to the exchange, not to the digits."""
    assert exchange_for_secid("0.000001") == Exchange.SZSE
    assert exchange_for_secid("1.600519") == Exchange.SSE


def test_a_unit_break_is_dropped_not_replayed() -> None:
    """A close an order of magnitude from the anchor is a data break."""
    raw = [_bar(2, 1410.27), "2026-01-05,141027,141027,141027,141027,1000,1"]
    bars = parse_klines(raw)
    assert len(bars) == 1
    assert bars[0].close_price == 1410.27


def test_a_real_daily_limit_move_survives() -> None:
    """A 20% STAR-Market band is a market move, not a unit change."""
    bars = parse_klines([_bar(2, 100.0), _bar(5, 82.0)])
    assert len(bars) == 2
    assert bars[1].close_price == 82.0


def test_a_bar_without_a_price_is_dropped() -> None:
    """A bar whose prices cannot be read yields the rest, not nothing."""
    bars = parse_klines([_bar(2, 10.0), "2026-01-05,,,,,1000,0"])
    assert len(bars) == 1


def test_an_unparseable_payload_raises_rather_than_returning_empty() -> None:
    """An empty result set is ambiguous; refusing to guess is not."""
    with pytest.raises(ValueError):
        parse_klines(["garbage", "also-garbage"])


def test_the_direct_host_is_first_and_gateways_follow() -> None:
    """The direct host is tried first; the gateways are the fallback."""
    urls = _kline_urls("/api/qt/stock/kline/get")
    assert urls[0] == "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    assert len(urls) == 3
    assert all("push2his.eastmoney.com" in url for url in urls[1:])


def test_max_bar_jump_keeps_real_moves_and_rejects_breaks() -> None:
    """The threshold sits between a STAR-Market band and a unit change."""
    assert MAX_BAR_JUMP > 1.25   # a 20% band plus its reverse
    assert MAX_BAR_JUMP < 10.0   # a one-decade mispricing

def test_sina_symbol_maps_market_the_same_way_as_secid() -> None:
    """A bare code has to reach the same exchange on both hosts.

    Sina has no exchange object to return, so the market is inferred from the
    code. Inferring it differently than the Eastmoney path would quietly ask
    the wrong exchange and get an empty answer that reads as a delisting.
    """
    assert _sina_symbol("600519") == "sh600519"
    assert _sina_symbol("000001") == "sz000001"
    assert _sina_symbol("000001.SH") == "sh000001"


def test_parse_sina_rows_reads_yuan_and_forward_adjusted_prices() -> None:
    """Sina rows arrive in yuan, and no divisor may be applied."""
    rows = [
        {"day": "2026-01-05", "open": "1410.0", "high": "1415.0",
         "low": "1405.0", "close": "1412.0", "volume": "123456"},
        {"day": "2026-01-06", "open": "1412.0", "high": "1420.0",
         "low": "1411.0", "close": "1418.0", "volume": "234567"},
    ]
    bars = _parse_sina_rows(rows)
    assert [bar.close_price for bar in bars] == [1412.0, 1418.0]
    assert all(bar.interval == Interval.DAILY for bar in bars)
    assert [bar.datetime.day for bar in bars] == [5, 6]


def test_a_malformed_sina_row_is_dropped_not_fatal() -> None:
    """One bad row in a 3000-row series is noise, not a missing instrument."""
    rows = [
        {"day": "2026-01-05", "open": "1410.0", "high": "1415.0",
         "low": "1405.0", "close": "1412.0", "volume": "123456"},
        {"day": "2026-01-06", "open": "bad", "high": "x", "low": "y",
         "close": "z", "volume": "1"},
    ]
    bars = _parse_sina_rows(rows)
    assert len(bars) == 1

def test_sina_unreachable_does_not_stop_the_campaign() -> None:
    """The second host failing must leave the first host as the path.

    The reason a second source exists is that the primary kline host drops
    connections for minutes at a time. Routing the second source first and
    swallowing its failure is what turns an outage into a slow path rather
    than a failed campaign, so the test pins that the fallback is still
    reached and still answers.
    """
    from vnpy.autonomous import stocks
    from vnpy.trader.constant import Exchange as Ex

    original = stocks.fetch_daily_bars_from_sina
    original_window = stocks._fetch_window

    def broken_sina(symbol: str):
        raise ValueError("sina down")

    def fake_window(secid, interval, begin, end, fqt="1"):
        return stocks.parse_klines([_fallback_row(begin)])

    stocks.fetch_daily_bars_from_sina = broken_sina
    stocks._fetch_window = fake_window
    try:
        bars = stocks.fetch_daily_bars("600519", years=1, use_cache=False)
    finally:
        stocks.fetch_daily_bars_from_sina = original
        stocks._fetch_window = original_window

    assert bars, "the Eastmoney path was never reached after sina failed"
    assert bars[0].symbol == "600519"
    assert bars[0].exchange == Ex.SSE


def _fallback_row(date_text: str) -> str:
    """One Eastmoney-shaped row for the fallback test."""
    return f"{date_text},1410.27,1412.0,1415.0,1405.0,123456,1412000.0"
