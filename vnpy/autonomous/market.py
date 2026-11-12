"""Live A-share market data from the public Eastmoney quote API.

The autonomous loop backtests on daily bars, but its frontier is a live bar:
before handing a promotion verdict to the broker, every provisioned symbol
should be checked against what the market actually prints right now. This
module fetches that snapshot without a paid data vendor account.

Scope note: quotes are read-only. Order routing still needs a CTP or broker
account; this module never places an order and never pretends a snapshot is a
fill. It exists so that "the signal says buy" can be cross-checked against
"the instrument is actually tradable today".
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import requests


logger: logging.Logger = logging.getLogger("vnpy.autonomous.market")

BASE_URL: str = "https://push2.eastmoney.com"
HIST_URL: str = "https://push2his.eastmoney.com"

# Eastmoney wants a "secid" of MARKET.SYMBOL. The market prefix is fixed by
# the exchange, not by the symbol's own digits: Shanghai is 1, Shenzhen 0.
# Guessing it from the code fails because SZ and SH codes overlap in range
# (000001 is the Ping An Bank stock on SZ and the Shanghai index on SH).
SH_MARKET: str = "1"
SZ_MARKET: str = "0"

# Eastmoney returns prices as integers scaled by 100 (cents), and percentages
# as the same scale. Reading them as plain floats prints prices 100x too big.
PRICE_SCALE: float = 100.0
PCT_SCALE: float = 100.0

DEFAULT_FIELDS: str = "f43,f57,f58,f60,f86,f169,f170"

# When the host has a system proxy (common on Windows), urllib funnels these requests
# through it and Eastmoney drops the connection. Honoring a NO_PROXY opt-out keeps quote
# fetches local and reproducible.
TRUST_ENV_DISABLED: bool = bool(os.environ.get("VNPY_MARKET_NO_PROXY", ""))
REQUEST_TIMEOUT_SECS: float = 10.0
RETRY_BACKOFF_SECS: float = 0.4


@dataclass
class Quote:
    """One live quote snapshot for a single instrument."""

    symbol: str
    name: str
    last_price: float
    prev_close: float
    change_pct: float
    change_amount: float
    updated_at: str
    secid: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def tradable(self) -> bool:
        """A snapshot with a price is a live instrument; zeros are not."""
        return self.last_price > 0.0


@dataclass
class QuoteError:
    """A failed lookup, kept alongside successes so callers can report it."""

    secid: str
    symbol: str
    message: str


def _headers() -> dict[str, str]:
    """Reuse one header set for every request; Eastmoney rejects bare hits."""
    return {
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }


def _make_session() -> requests.Session:
    """Build a requests.Session that stays off a system HTTP proxy.

    A Windows machine with a system proxy makes urllib sometimes route
    localhost-only traffic through it; Eastmoney then closes the connection
    with no response and the retry loop looks like an outage. Building a
    session with trust_env disabled keeps quote fetches reproducible.
    """
    session: requests.Session = requests.Session()
    if TRUST_ENV_DISABLED:
        session.trust_env = False
    return session


def resolve_secid(symbol: str) -> str:
    """Map a bare A-share symbol to Eastmoney's MARKET.SYMBOL form.

    ``6``-prefixed main-board codes are Shanghai; ``0`` and ``3`` are
    Shenzhen. Symbols that already carry an exchange suffix (``000001.SH``)
    keep it, because the same digits name a different instrument on the other
    exchange.
    """
    raw: str = symbol.strip().upper()
    if "." in raw:
        code, _, market = raw.partition(".")
        if market in ("SZ", "SZSE"):
            return f"{SZ_MARKET}.{code}"
        return f"{SH_MARKET}.{code}"
    if raw.startswith("6"):
        return f"{SH_MARKET}.{raw}"
    return f"{SZ_MARKET}.{raw}"


def _request_json(url: str, params: dict[str, str]) -> dict:
    """GET one JSON payload, retrying on transient network resets.

    Eastmoney occasionally cuts the connection with no response body; the
    retry is cheap and turns a rare failure into a slow success.
    """
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            session: requests.Session = _make_session()
            try:
                response: requests.Response = session.get(
                    url,
                    params=params,
                    headers=_headers(),
                    timeout=REQUEST_TIMEOUT_SECS,
                )
            finally:
                session.close()
            response.raise_for_status()
            payload: object = response.json()
            if isinstance(payload, dict):
                return payload
            raise ValueError(f"interface returned {type(payload).__name__}")
        except Exception as exc:  # noqa: BLE001 - retry every failure mode
            last_error = exc
            if attempt < 2:
                time.sleep(RETRY_BACKOFF_SECS * (attempt + 1))
    raise ValueError(f"quote request failed: {last_error}")


def _parse_timestamp(epoch: object) -> str:
    """Render Eastmoney's unix-seconds field as local wall clock text."""
    try:
        seconds: int = int(str(epoch))
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


def _to_float(value: object) -> float:
    """Eastmoney sends numbers as JSON strings; blank means unknown."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _build_quote(data: dict, secid: str) -> Quote:
    """Turn one stock/get payload into a Quote, scaling by 100."""
    return Quote(
        symbol=str(data.get("f57", "")),
        name=str(data.get("f58", "")),
        last_price=_to_float(data.get("f43")) / PRICE_SCALE,
        prev_close=_to_float(data.get("f60")) / PRICE_SCALE,
        change_pct=_to_float(data.get("f170")) / PCT_SCALE,
        change_amount=_to_float(data.get("f169")) / PRICE_SCALE,
        updated_at=_parse_timestamp(data.get("f86")),
        secid=secid,
        raw=dict(data),
    )


def fetch_quote(symbol: str) -> Quote:
    """Fetch the live snapshot for one A-share symbol.

    Raises ``ValueError`` when the exchange hosts no such instrument, so a
    typo in a config entry surfaces as an error instead of a silent empty
    row.
    """
    secid: str = resolve_secid(symbol)
    payload: dict = _request_json(
        f"{BASE_URL}/api/qt/stock/get",
        {"secid": secid, "fields": DEFAULT_FIELDS},
    )
    data: object = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise ValueError(f"no quote for {secid}")
    return _build_quote(data, secid)


def fetch_quotes(symbols: list[str]) -> tuple[list[Quote], list[QuoteError]]:
    """Fetch snapshots for many symbols in one round trip.

    Returns successes and failures separately: a watchlist where one symbol
    is delisted should still show the other rows instead of aborting.
    """
    if not symbols:
        return [], []

    secids: list[str] = []
    original: dict[str, str] = {}
    for symbol in symbols:
        secid: str = resolve_secid(symbol)
        secids.append(secid)
        original[secid] = symbol

    payload: dict = _request_json(
        f"{BASE_URL}/api/qt/ulist.np/get",
        {
            "flid": "rs",
            "secids": ",".join(secids),
            "fields": "f2,f3,f4,f12,f14,f18,f124",
        },
    )

    quotes: list[Quote] = []
    errors: list[QuoteError] = []
    data: object = payload.get("data")
    entries: object = data.get("diff") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        for secid in secids:
            errors.append(QuoteError(secid, original[secid], "no quote data"))
        return quotes, errors

    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code: str = str(entry.get("f12", ""))
        # The entry carries only the bare code, so match it back through the
        # caller's own secid list rather than inferring the market from the
        # digits: 000001 is a SZ stock and a SH index, and guessing wrong
        # silently drops the row.
        secid = ""
        for candidate in secids:
            if candidate.split(".")[-1] == code and candidate not in seen:
                secid = candidate
                break
        if secid not in original:
            continue
        seen.add(secid)
        quotes.append(
            Quote(
                symbol=code,
                name=str(entry.get("f14", "")),
                last_price=_to_float(entry.get("f2")) / PRICE_SCALE,
                prev_close=_to_float(entry.get("f18")) / PRICE_SCALE,
                change_pct=_to_float(entry.get("f3")) / PCT_SCALE,
                change_amount=_to_float(entry.get("f4")) / PRICE_SCALE,
                updated_at=_parse_timestamp(entry.get("f124")),
                secid=secid,
                raw=dict(entry),
            )
        )

    for secid in secids:
        if secid not in seen:
            errors.append(QuoteError(secid, original[secid], "symbol not found"))

    return quotes, errors
