
from __future__ import annotations

import pytest

from vnpy.trader.constant import Exchange, Interval

from vnpy.autonomous.stocks import (
    MAX_BAR_JUMP,
    _kline_urls,
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
