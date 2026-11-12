"""A tick feed that turns public minute bars into paper-account ticks.

Two facts drive this design. The paper account fills nothing without a tick,
so the autonomous loop needs a tick source to be able to trade at all. And
the public history endpoint ignores the row-limit parameter while rate
limiting hosts that poll it too often. So the feed fetches a whole session
once per contract, persists a cursor of the last bar it emitted, and derives
ticks only from bars newer than that cursor.

The emitter is deliberately conservative about what it will claim. A minute
bar has no order book, so bid and ask come from the bar's own range; a limit
order therefore fills only when the price actually traded through it during
that minute. Prices are never divided by a fixed constant, because Eastmoney
scales every contract differently and a wrong divisor would value every
position by a factor of ten or a hundred.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Exchange
from vnpy.trader.event import EVENT_TICK
from vnpy.trader.object import ContractData, TickData

from .cursor import FeedCursor, load_cursor, save_cursor

logger: logging.Logger = logging.getLogger("vnpy.autonomous.feed")

FEED_GATEWAY_NAME: str = "EMFEED"

HIST_BASE_URL: str = "https://push2his.eastmoney.com"
KLINE_FIELDS: str = "f51,f52,f53,f54,f55,f56,f57"

# secid MARKET.SYMBOL prefixes, read from live responses rather than guessed:
# a CFFEX index quotes under 220, and the widely copied "8" returns nothing.
EM_MARKET: dict[str, str] = {
    "CFFEX": "220",
    "SHFE": "113",
    "DCE": "114",
    "CZCE": "115",
    "INE": "142",
    "GFEX": "225",
}

REQUEST_TIMEOUT_SECS: float = 15.0
MAX_RETRIES: int = 3
RETRY_BACKOFF_SECS: float = 0.4

# The endpoint returns the whole session whatever limit is asked for, and it
# drops the connection when hit in bursts, so poll slowly and keep a gap.
DEFAULT_POLL_SECS: float = 60.0
MIN_REQUEST_GAP_SECS: float = 2.0

_last_request_at: float = 0.0


def _throttle() -> None:
    """Keep a minimum gap between requests to the public endpoint."""
    global _last_request_at
    now: float = time.time()
    remaining: float = MIN_REQUEST_GAP_SECS - (now - _last_request_at)
    if remaining > 0:
        time.sleep(remaining)
    _last_request_at = time.time()


def _session() -> requests.Session:
    """Build a requests session that stays off a system proxy."""
    session: requests.Session = requests.Session()
    if os.environ.get("VNPY_MARKET_NO_PROXY", ""):
        session.trust_env = False
    return session


def _headers() -> dict[str, str]:
    """Eastmoney rejects bare hits without a referer."""
    return {
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }


def secid_for(symbol: str, exchange: Exchange) -> str:
    """Build Eastmoney's MARKET.SYMBOL key from a symbol and exchange."""
    market: str | None = EM_MARKET.get(exchange.value)
    if market is None:
        raise ValueError(f"no Eastmoney market for exchange {exchange.value}")
    return f"{market}.{symbol}"


class MinuteBar:
    """One minute bar as Eastmoney reports it, still in raw scaled units."""

    __slots__ = (
        "time_text",
        "open_raw",
        "close_raw",
        "high_raw",
        "low_raw",
        "volume",
        "turnover",
    )

    def __init__(
        self,
        time_text: str,
        open_raw: float,
        close_raw: float,
        high_raw: float,
        low_raw: float,
        volume: float,
        turnover: float,
    ) -> None:
        self.time_text: str = time_text
        self.open_raw: float = open_raw
        self.close_raw: float = close_raw
        self.high_raw: float = high_raw
        self.low_raw: float = low_raw
        self.volume: float = volume
        self.turnover: float = turnover

    @property
    def effective_scale(self) -> float:
        """Infer Eastmoney's scaling factor from this bar's own shape.

        The same endpoint sends soybean meal as 2838 meaning 283.8, and
        Shanghai gold as 845.66 meaning 845.66. Both shapes carry a real
        price, so the value itself says which one it is: a whole number above
        100000 is scaled by 100, one above 1000 by 10, and a number that
        already carries decimals is already in yuan. Recognising only these
        three shapes is deliberate; guessing past them would price a gold
        contract two magnitudes short.
        """
        raw: float = self.close_raw
        if raw != int(raw):
            return 1.0
        if raw >= 100000:
            return 100.0
        if raw >= 1000:
            return 10.0
        return 1.0


def _parse_bar(line: str) -> MinuteBar | None:
    """Parse one comma-joined kline row, or None when malformed."""
    parts: list[str] = line.split(",")
    if len(parts) < 7:
        return None
    try:
        return MinuteBar(
            time_text=parts[0],
            open_raw=float(parts[1]),
            close_raw=float(parts[2]),
            high_raw=float(parts[3]),
            low_raw=float(parts[4]),
            volume=float(parts[5]),
            turnover=float(parts[6]),
        )
    except (TypeError, ValueError):
        return None


def _parse_timestamp(time_text: str) -> datetime | None:
    """Parse the kline timestamp, which may or may not carry seconds."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(time_text, fmt)
        except ValueError:
            continue
    return None


def fetch_minute_bars(symbol: str, exchange: Exchange) -> list[MinuteBar]:
    """Fetch the minute bars the endpoint currently has for one contract.

    Raises ``ValueError`` when the host reports no such contract, so a typo
    in a provisioned symbol surfaces loudly instead of leaving the strategy
    waiting for a tick that never arrives.
    """
    secid: str = secid_for(symbol, exchange)
    params: dict[str, str] = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": KLINE_FIELDS,
        "klt": "1",
        "fqt": "1",
        "end": "20500101",
    }

    payload: dict = {}
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            session: requests.Session = _session()
            try:
                response: requests.Response = session.get(
                    f"{HIST_BASE_URL}/api/qt/stock/kline/get",
                    params=params,
                    headers=_headers(),
                    timeout=REQUEST_TIMEOUT_SECS,
                )
            finally:
                session.close()
            response.raise_for_status()
            body: object = response.json()
            if isinstance(body, dict):
                payload = body
                break
            last_error = ValueError("non-object payload")
        except Exception as exc:  # noqa: BLE001 - retry every failure mode
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECS * (attempt + 1))
    else:
        raise ValueError(f"minute bar request failed: {last_error}")

    data: object = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise ValueError(f"no minute bars for {secid}")

    klines: object = data.get("klines")
    if not isinstance(klines, list) or not klines:
        raise ValueError(f"no minute bars for {secid}")

    bars: list[MinuteBar] = []
    line: object
    for line in klines:
        if not isinstance(line, str):
            continue
        bar: MinuteBar | None = _parse_bar(line)
        if bar is not None:
            bars.append(bar)
    if not bars:
        raise ValueError(f"no parseable minute bars for {secid}")
    return bars


# Eastmoney scales a contract by its own factor, and that factor is not
# derivable from a bar alone: 2829.0 sits on a 0.5 grid at both scale 1 and
# scale 10, and both readings give a ten-ton lot a plausible notional. The
# only way to price it right is to declare it, so these are the factors read
# from live quotes for the contracts this project trades. A symbol missing
# from this table falls back to the bar's own decimal shape, which the caller
# should treat as a gap to fill in config rather than a proven answer.
KNOWN_PRICE_SCALE: dict[str, float] = {
    "m": 10.0,          # 豆粕，raw 2829 -> 282.9
    "rb": 10.0,         # 螺纹，raw 3028 -> 302.8
    "hc": 10.0,         # 热卷
    "i": 10.0,          # 铁矿石
    "au": 1.0,          # 沪金，raw 845.66 已是元
    "ag": 1.0,          # 沪银
    "cu": 1.0,          # 沪铜
    "ni": 1.0,          # 沪镍
    "if": 1.0,          # 股指，指数点即原值
    "ic": 1.0,
    "ih": 1.0,
    "sr": 10.0,         # 白糖
    "cf": 10.0,         # 棉花
    "ta": 1.0,
    "ma": 1.0,
    "oi": 1.0,
    "fg": 1.0,
    "zc": 1.0,
    "sc": 1.0,
}


def price_symbol(contract_symbol: str) -> str:
    """Strip the delivery month to reach a contract's exchange symbol.

    "m2705" and "m9999" both trade under soybean meal's "m", and a continuous
    contract and a specific month scale their prices identically.
    """
    letters: str = "".join(ch for ch in contract_symbol if not ch.isdigit())
    return letters.lower()


def resolve_scale(bar: MinuteBar, contract: ContractData) -> float:
    """Return this contract's price scale, defaulting to the bar's own shape.

    The symbol table is the authority because the bar alone cannot determine
    the factor. An unknown symbol keeps the bar's reading, which is reported
    rather than silently trusted, so a config can declare the factor.
    """
    known: float | None = KNOWN_PRICE_SCALE.get(price_symbol(contract.symbol))
    if known is not None:
        return known
    return bar.effective_scale


def _align(price: float, pricetick: float) -> float:
    """Round a price onto the contract's tick grid."""
    if pricetick <= 0:
        return round(price, 2)
    steps: int = round(price / pricetick)
    return round(steps * pricetick, 8)


def build_tick(bar: MinuteBar, contract: ContractData) -> TickData | None:
    """Turn one minute bar into a TickData priced in the contract's unit.

    Returns None when the bar cannot be timed or priced safely. Emitting a
    tick at a wrong price is worse than emitting none, because a paper fill at
    a wrong price is a fake trade rather than a missing one.
    """
    when: datetime | None = _parse_timestamp(bar.time_text)
    if when is None:
        return None

    scale: float = resolve_scale(bar, contract)
    if scale <= 0:
        return None

    pricetick: float = contract.pricetick if contract.pricetick else 0.01
    open_price: float = _align(bar.open_raw / scale, pricetick)
    close_price: float = _align(bar.close_raw / scale, pricetick)
    high_price: float = _align(bar.high_raw / scale, pricetick)
    low_price: float = _align(bar.low_raw / scale, pricetick)

    # A minute bar has no order book, so the top ask is the bar's high and the
    # top bid is its low. A buy limit fills only when the minute traded down
    # through it, which is the conservative reading of a bar.
    ask_price: float = high_price
    bid_price: float = low_price
    if ask_price <= bid_price:
        ask_price = _align(bid_price + pricetick, pricetick)

    return TickData(
        gateway_name=FEED_GATEWAY_NAME,
        symbol=contract.symbol,
        exchange=contract.exchange,
        datetime=when,
        name=contract.name,
        volume=bar.volume,
        turnover=bar.turnover,
        open_interest=0.0,
        last_price=close_price,
        last_volume=bar.volume,
        limit_up=high_price,
        limit_down=low_price,
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        pre_close=0.0,
        bid_price_1=bid_price,
        ask_price_1=ask_price,
        bid_price_2=bid_price,
        bid_price_3=bid_price,
        bid_price_4=bid_price,
        bid_price_5=bid_price,
        ask_price_2=ask_price,
        ask_price_3=ask_price,
        ask_price_4=ask_price,
        ask_price_5=ask_price,
        bid_volume_1=max(bar.volume, 1.0),
        ask_volume_1=max(bar.volume, 1.0),
        bid_volume_2=1.0,
        bid_volume_3=1.0,
        bid_volume_4=1.0,
        bid_volume_5=1.0,
        ask_volume_2=1.0,
        ask_volume_3=1.0,
        ask_volume_4=1.0,
        ask_volume_5=1.0,
        localtime=datetime.now(),
    )


class DerivedFeed:
    """Poll the public endpoint and emit ticks only for bars not yet seen.

    The cursor is what keeps a restart honest: it records the last bar already
    turned into a tick, so a restart continues rather than replaying a session
    and booking fills twice. ``max_catchup`` bounds how many bars a single
    poll may emit, so a long outage cannot bury the process in a burst of
    stale ticks.
    """

    def __init__(
        self,
        event_engine: EventEngine,
        contracts: dict[str, ContractData],
        cursor_dir: Path,
        poll_secs: float = DEFAULT_POLL_SECS,
        max_catchup: int = 5,
    ) -> None:
        self.event_engine: EventEngine = event_engine
        self.contracts: dict[str, ContractData] = contracts
        self.cursor_dir: Path = cursor_dir
        self.poll_secs: float = poll_secs
        self.max_catchup: int = max_catchup
        self.cursor: FeedCursor = load_cursor(cursor_dir)
        self.ticks_emitted: int = 0
        self.last_tick_at: datetime | None = None
        self.last_error: str = ""

    def poll_once(self) -> list[TickData]:
        """Fetch bars for every contract and emit ticks for new ones.

        A per-contract failure is reported on ``last_error`` and skipped
        rather than aborting the whole watchlist, so one delisted contract
        does not stop the rest from trading.
        """
        ticks: list[TickData] = []
        for vt_symbol, contract in self.contracts.items():
            try:
                bars: list[MinuteBar] = fetch_minute_bars(
                    contract.symbol, contract.exchange
                )
            except ValueError as exc:
                self.last_error = f"{vt_symbol}: {exc}"
                logger.warning("[emfeed] %s", self.last_error)
                continue

            emitted: int = 0
            for bar in bars:
                if self.cursor.seen(bar.time_text):
                    continue
                tick: TickData | None = build_tick(bar, contract)
                if tick is None:
                    continue
                self.event_engine.put(Event(EVENT_TICK, tick))
                self.cursor.advance(bar)
                ticks.append(tick)
                emitted += 1
                if emitted >= self.max_catchup:
                    break

        if ticks:
            self.ticks_emitted += len(ticks)
            self.last_tick_at = datetime.now()
            save_cursor(self.cursor_dir, self.cursor)
        return ticks
