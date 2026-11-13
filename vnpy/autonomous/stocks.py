"""Daily bars for A-shares, pulled from the public Eastmoney kline API.

The promotion path in ``docs/community/autonomous/promotion.md`` asks a
strategy to prove itself on real data before the runner arms it. Until now
that proof stopped at a CSV someone exported by hand, which makes the verdict
a transcription of a file rather than a measurement of the market. This
module removes the manual step: give it symbols and it returns bars.

Two things about this endpoint matter for correctness and both are learned
from its behaviour rather than assumed. It answers with whatever window it is
given and ignores a row count, so pulling a bounded history needs the ``end``
parameter to walk backwards rather than a limit. And it scales every
instrument by its own factor: soybean meal prints 2829 meaning 282.9 while
Shanghai gold prints 845.66 meaning 845.66, so a single divisor applied to
every contract would misprice a position by a factor of ten or a hundred.
For A-shares that hazard is absent - prices arrive already in yuan - but the
guard stays, because a field that means something else one day would
silently fabricate returns.

FQT=1 (forward-adjusted) is the only honest default for a backtest. A
stock's raw close carries every dividend and split since listing, and an
unadjusted series paints a long history of drops that never happened to a
holder, which is exactly the series a strategy would be asked to survive.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

from .market import resolve_secid

HIST_BASE_URL: str = "https://push2his.eastmoney.com"

# The kline host drops the connection outright for a while after a burst, and
# the block outlasts a five minute pause. These gateways are the user's own
# acceleration entries, and they return byte-identical payloads, so they are
# the fallback rather than a hack: the last probe saw direct fail three times
# in a row while both gateways answered with the same 45200 bytes.
PROXY_PREFIXES: tuple[str, ...] = (
    "https://seep.eu.org/",
    "https://jsnzkpg4.pages.dev/",
)

# secid prefixes come from the market module rather than being guessed here:
# 6-prefixed codes are Shanghai, 0 and 3 Shenzhen, and the same rule resolves
# the 8-prefixed codes both markets host.
KLINE_FIELDS: str = "f51,f52,f53,f54,f55,f56,f57"

# Eastmoney kline codes by interval. Daily is the only one the autonomous loop
# grades on; the rest exist so a caller can widen without editing code.
INTERVAL_CODES: dict[Interval, str] = {
    Interval.DAILY: "101",
    Interval.WEEKLY: "102",
    Interval.HOUR: "60",
    Interval.MINUTE: "1",
}

REQUEST_TIMEOUT_SECS: float = 15.0
MAX_RETRIES: int = 3
RETRY_BACKOFF_SECS: float = 0.4

# The kline host drops the connection outright when hit in bursts, unlike the
# quote host which merely slows down. Measured here: a probe every one or two
# seconds stayed healthy, six requests in a row earned a connection reset that
# outlasted a five minute pause. So requests are spaced out, not just retried.
MIN_REQUEST_GAP_SECS: float = 3.0

# A fetched series is cached on disk. The point of pulling real bars is to
# refresh a campaign, not to re-download the same history on every run, and a
# repeated backtest would otherwise re-hit a host that punishes bursts.
CACHE_DIRNAME: str = ".vntrader/stock_bars"
CACHE_TTL_SECS: float = 24 * 60 * 60

# The endpoint ignores the row limit and returns the whole window it is given,
# so a request is shaped by ``end`` alone. Asking for more than this many
# years back would pull a decade of bars in one answer for no reason.
MAX_YEARS_PER_REQUEST: int = 5

# An A-share bar whose close departs from the previous close by more than this
# ratio is a data break, not a market move. The STAR Market's 20% daily band
# and an ordinary stock's 10% band both sit well inside it, while a unit or
# divisor change blows straight through it.
MAX_BAR_JUMP: float = 3.0


_last_request_at: float = 0.0


def _throttle() -> None:
    """Keep a minimum gap between requests to the kline host."""
    global _last_request_at
    now: float = time.monotonic()
    remaining: float = MIN_REQUEST_GAP_SECS - (now - _last_request_at)
    if remaining > 0:
        time.sleep(remaining)
    _last_request_at = time.monotonic()


def _kline_urls(path: str) -> list[str]:
    """Build the request URL list, direct host first then the gateways."""
    urls: list[str] = [f"{HIST_BASE_URL}{path}"]
    prefix: str
    for prefix in PROXY_PREFIXES:
        urls.append(f"{prefix}{HIST_BASE_URL.replace('https://', '')}{path}")
    return urls


def _kline_url(path: str, attempt: int) -> str:
    """Pick the route for one retry: direct host first, then the gateways."""
    urls: list[str] = _kline_urls(path)
    return urls[attempt % len(urls)]


def _headers() -> dict[str, str]:
    """Eastmoney rejects bare hits without a referer."""
    return {
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }


def _to_float(value: object) -> float:
    """Best-effort float conversion, treating a blank field as unknown."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def parse_klines(klines: object) -> list[BarData]:
    """Parse a raw klines list into bars in yuan.

    Returns the bars it could read, so a list where one row is malformed
    yields the rest rather than discarding the whole series. A row is dropped
    when its price cannot be read at all: pricing a bar wrong is worse than
    omitting it, because a wrong price buys a position that no real trade
    would have justified.
    """
    bars: list[BarData] = []
    anchor: float = 0.0
    dropped: int = 0
    if not isinstance(klines, list):
        return bars

    line: object
    for line in klines:
        if not isinstance(line, str) or "," not in line:
            dropped += 1
            continue
        parts: list[str] = line.split(",")
        if len(parts) < 6:
            dropped += 1
            continue

        try:
            date_text: str = parts[0].strip()
            open_price: float = _to_float(parts[1])
            close_price: float = _to_float(parts[2])
            high_price: float = _to_float(parts[3])
            low_price: float = _to_float(parts[4])
            volume: float = _to_float(parts[5])
            when: datetime = datetime.fromisoformat(date_text)
        except (TypeError, ValueError):
            dropped += 1
            continue

        if open_price <= 0 or close_price <= 0 or high_price <= 0 or low_price <= 0:
            dropped += 1
            continue

        # Compare each bar against the previous accepted bar rather than a
        # fixed anchor. A fixed anchor breaks the moment the payload holds
        # two instruments: a Maotai bar followed by a Ping An bar is a real
        # hundred-to-one span, and anchoring on the first would drop the whole
        # second instrument as a "unit change". A running anchor still catches
        # the shape this guard exists for, a jump of an order of magnitude
        # inside one instrument's own series.
        if anchor > 0:
            ratio: float = close_price / anchor
            if ratio > MAX_BAR_JUMP or ratio < 1.0 / MAX_BAR_JUMP:
                dropped += 1
                continue
        anchor = close_price

        bars.append(
            BarData(
                symbol="",
                exchange=Exchange.SSE,
                datetime=when,
                interval=Interval.DAILY,
                volume=volume,
                turnover=volume * close_price,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                gateway_name="EMSTOCK",
            )
        )
    if not bars and dropped:
        raise ValueError("no parseable bars in the klines payload")
    return bars


def exchange_for_secid(secid: str) -> Exchange:
    """Return the exchange an Eastmoney secid belongs to."""
    market: str = secid.partition(".")[0]
    if market == "0":
        return Exchange.SZSE
    return Exchange.SSE


def _exchange_for_symbol(symbol: str) -> Exchange:
    """Map a bare symbol to its exchange through the same rule as quotes."""
    return exchange_for_secid(resolve_secid(symbol))


def _fetch_window(
    secid: str,
    interval: str,
    begin: str,
    end: str,
    fqt: str = "1",
) -> list[BarData]:
    """Fetch one kline window, retrying on transient network resets."""
    params: dict[str, str] = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": KLINE_FIELDS,
        "klt": interval,
        "fqt": fqt,
        "beg": begin,
        "end": end,
        "lmt": "1000000",
    }

    payload: dict[str, Any] = {}
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            session: requests.Session = requests.Session()
            try:
                # A system proxy drops these connections on Windows; the
                # quotes module learned the same lesson and opted out here too.
                session.trust_env = False
                response: requests.Response = session.get(
                    _kline_url("/api/qt/stock/kline/get", attempt),
                    params=params,
                    headers=_headers(),
                    timeout=REQUEST_TIMEOUT_SECS,
                )
            finally:
                session.close()
            response.raise_for_status()
            body: object = response.json()
            # A gateway answers 200 with an HTML error page when it cannot
            # reach the origin, so a non-object body is a signal to try the
            # next route rather than a parse failure to swallow.
            if isinstance(body, dict):
                payload = body
                break
            last_error = ValueError("non-object payload")
        except Exception as exc:   # noqa: BLE001 - retry every failure mode
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECS * (attempt + 1))
    else:
        raise ValueError(f"kline request failed: {last_error}")

    data: object = payload.get("data")
    if not isinstance(data, dict) or not data:
        # A symbol the exchange does not host answers with an empty payload
        # rather than an error code, so this is the only signal available.
        raise ValueError(f"no bars for {secid}")
    klines: object = data.get("klines")
    if not isinstance(klines, list) or not klines:
        raise ValueError(f"no bars for {secid}")
    return parse_klines(klines)


def fetch_daily_bars(
    symbol: str,
    years: int = 3,
    exchange: Exchange | None = None,
    use_cache: bool = True,
) -> list[BarData]:
    """Fetch forward-adjusted daily bars for one A-share symbol.

    ``years`` walks backwards from today in year-round steps, because the
    endpoint answers with whatever window it is given and ignores a row
    count. Raises ``ValueError`` when the exchange hosts no such symbol, so a
    typo in a watchlist surfaces loudly instead of producing an empty
    campaign.
    """
    resolved: Exchange = exchange or _exchange_for_symbol(symbol)
    secid: str = resolve_secid(symbol)
    interval: str = INTERVAL_CODES[Interval.DAILY]

    if use_cache:
        cached: list[BarData] | None = _read_cache(secid, interval, resolved)
        if cached is not None:
            return cached

    end: str = datetime.now().strftime("%Y%m%d")
    collected: list[BarData] = []
    seen: set[datetime] = set()

    for _ in range(max(1, years // MAX_YEARS_PER_REQUEST)):
        end_year: int = datetime.strptime(end, "%Y%m%d").year
        begin: str = f"{end_year - MAX_YEARS_PER_REQUEST}0101"
        bars: list[BarData] = _fetch_window(secid, interval, begin, end, fqt="1")
        if not bars:
            break
        fresh: list[BarData] = [bar for bar in bars if bar.datetime not in seen]
        if not fresh:
            break
        bar: BarData
        for bar in fresh:
            bar.symbol = symbol
            bar.exchange = resolved
            seen.add(bar.datetime)
            collected.append(bar)
        # Start the next step from the earliest bar already held, since the
        # window is inclusive of ``end`` and repeating it would re-fetch.
        earliest: datetime = min(bar.datetime for bar in fresh)
        end = earliest.strftime("%Y%m%d")

    collected.sort(key=lambda item: item.datetime)
    if not collected:
        raise ValueError(f"no daily bars for {secid}")
    if use_cache:
        _write_cache(secid, interval, resolved, collected)
    return collected


def _cache_path(secid: str, interval: str) -> Path:
    """Build the cache file path for one secid and interval."""
    safe: str = secid.replace("/", "_").replace(".", "_")
    return Path(CACHE_DIRNAME) / f"{safe}_{interval}.csv"


def _read_cache(
    secid: str,
    interval: str,
    exchange: Exchange,
) -> list[BarData] | None:
    """Read a fresh cache entry, or None when absent or stale.

    A stale cache is treated as absent rather than trimmed: a series with a
    gap in the middle would grade a strategy on a market that skipped bars,
    and the whole point of fetching live is that the evidence is current.
    """
    path: Path = _cache_path(secid, interval)
    if not path.is_file():
        return None
    age: float = time.time() - path.stat().st_mtime
    if age > CACHE_TTL_SECS:
        return None

    lines: list[str]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    bars: list[BarData] = []
    for line in lines[1:]:
        parts: list[str] = line.split(",")
        if len(parts) < 6:
            continue
        try:
            when: datetime = datetime.fromisoformat(parts[0])
            bars.append(
                BarData(
                    symbol=parts[6] if len(parts) > 6 else "",
                    exchange=exchange,
                    datetime=when,
                    interval=Interval.DAILY,
                    volume=float(parts[5]),
                    turnover=float(parts[5]) * float(parts[2]),
                    open_price=float(parts[1]),
                    high_price=float(parts[3]),
                    low_price=float(parts[4]),
                    close_price=float(parts[2]),
                    gateway_name="EMSTOCK",
                )
            )
        except (TypeError, ValueError):
            continue
    return bars or None


def _write_cache(
    secid: str,
    interval: str,
    exchange: Exchange,
    bars: list[BarData],
) -> None:
    """Write a fetched series atomically so a crash mid-write loses nothing."""
    path: Path = _cache_path(secid, interval)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path = path.with_suffix(".tmp")
        tmp.write_text(
            "datetime,open,close,high,low,volume,symbol\n"
            + "\n".join(
                ",".join(
                    [
                        bar.datetime.isoformat(),
                        f"{bar.open_price:g}",
                        f"{bar.close_price:g}",
                        f"{bar.high_price:g}",
                        f"{bar.low_price:g}",
                        f"{bar.volume:g}",
                        bar.symbol,
                    ]
                )
                for bar in bars
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        # A cache is an optimisation, so failing to write one must not fail a
        # campaign that already has its bars in hand.
        return
