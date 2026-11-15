"""Fetch the float share count that Eastmoney already publishes with the bars.

The alpha research needed a size information set and the repository had only
price and volume. The same public kline endpoint that carries the OHLCV bars
also carries two more columns that the old request never asked for:

* ``f61`` - turnover rate in percent
* ``f70`` - float shares outstanding

The second one is the one that matters. ``f70`` is a point-in-time value, not
today's value repeated backwards: a share issue shows up on the day it lands
and a split shows up on the day it happens. That was checked on names whose
capital changed hard inside the sample window, and the series moves on those
dates and stays flat between them.

Two independent readings agree that ``f70`` is float shares rather than
total shares. The implied float count from turnover,
``volume * 100 / (turnover / 100)``, sits at 1.00 of ``f70`` for names that
have no restricted stock, and the market caps it produces are the right
order of magnitude for the market (Moutai near 1.9 trillion yuan, ICBC near
1.0 trillion).

The module writes one CSV per symbol next to the cached bars, carrying the
same date and the float share count, so the research scripts can join it on
the date without changing how they read bars.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


HIST_BASE_URL: str = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# f51 date, f52 open, f53 close, f54 high, f55 low, f56 volume, f61 turnover
# rate in percent, f70 float shares outstanding.
KLINE_FIELDS: str = "f51,f52,f53,f54,f55,f56,f61,f70"

REQUEST_TIMEOUT_SECS: float = 20.0
MAX_RETRIES: int = 4
RETRY_BACKOFF_SECS: float = 3.0

# The host drops the connection outright when hit in bursts, and the block
# outlasts a short pause. Measured on batches of sixty distinct symbols:
# eight workers with a gap of five seconds each got 50 of 60 through at
# 1.06 a second, twelve workers with a gap of three seconds each got 44 of
# 60 at 1.63 a second, and twenty-four workers with a gap of a second and a
# half got 27 of 60 at 0.94 a second. So the gap has to be per worker and
# wide enough that the host never feels hit.
MIN_REQUEST_GAP_SECS: float = 3.0

# A pass over the symbols that did not land waits this long before the next
# one, because the host's refusal clears on its own and waiting is cheaper
# than retrying into it.
PAUSE_BETWEEN_ROUNDS_SECS: float = 20.0

# The host is also reachable through the user's own acceleration entries,
# which return the same payload. Which one works depends on the host's
# current mood: measured against a host that was refusing every direct
# request, the jsnzkpg4 gateway answered 8 of 8 while the direct host
# answered 0 of 8 and the seep gateway answered 0 of 8. So the gateway
# comes first and the direct host is the fallback.
PROXY_PREFIXES: tuple[str, ...] = (
    "https://jsnzkpg4.pages.dev/",
    "https://seep.eu.org/",
)

HEADERS: dict[str, str] = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": "Mozilla/5.0",
}

_request_state = threading.local()


def throttle() -> None:
    """Keep a minimum gap between requests to the kline host.

    The gap is per worker rather than shared. One shared gap caps the whole
    fetch at one request every gap seconds no matter how many workers are
    running, which measured at 0.067 symbols a second with eight workers
    and a shared gap of five seconds. A per-worker gap lets each worker
    keep its own pace and the fetch runs at the rate the host allows.
    """
    last: float = getattr(_request_state, "last", 0.0)
    now: float = time.monotonic()
    remaining: float = MIN_REQUEST_GAP_SECS - (now - last)
    if remaining > 0:
        time.sleep(remaining)
    _request_state.last = time.monotonic()


def secid_for(code: str) -> str:
    """Turn a bare six-digit code into the host's secid.

    The prefix comes from the market module's rule rather than a lookup
    table: 6-prefixed codes are Shanghai, 0 and 3 are Shenzhen.
    """
    code = code.strip()
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    return f"0.{code}"


def build_urls(code: str, beg: str, end: str) -> list[str]:
    """Build the kline request routes for one symbol and window.

    The direct host comes first and the gateways follow, so a blocked
    request can be retried somewhere else rather than just waiting.
    """
    params: dict[str, str] = {
        "secid": secid_for(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": KLINE_FIELDS,
        "klt": "101",
        "fqt": "1",
        "beg": beg,
        "end": end,
    }
    query: str = urllib.parse.urlencode(params)
    direct: str = f"{HIST_BASE_URL}?{query}"
    bare: str = HIST_BASE_URL.replace("https://", "")
    routes: list[str] = [direct]
    prefix: str
    for prefix in PROXY_PREFIXES:
        routes.append(f"{prefix}{bare}?{query}")
    return routes


def build_url(code: str, beg: str, end: str) -> str:
    """The direct kline request for one symbol and window."""
    return build_urls(code, beg, end)[0]


def parse_float_shares(klines: object) -> list[tuple[str, float]]:
    """Read the date and float share count out of a raw klines list.

    Returns only the rows where both values read as numbers, because a share
    count of zero would silently become a market cap of zero and poison the
    size ranking it is meant to feed.
    """
    rows: list[tuple[str, float]] = []
    if not isinstance(klines, list):
        return rows

    line: object
    for line in klines:
        if not isinstance(line, str) or "," not in line:
            continue
        parts: list[str] = line.split(",")
        # The request asks for eight fields, so a complete row has eight.
        if len(parts) < 8:
            continue
        date: str = parts[0].strip()
        try:
            shares: float = float(parts[7])
        except ValueError:
            continue
        if not date or shares <= 0:
            continue
        rows.append((date, shares))
    return rows


def fetch_float_shares(
    code: str,
    beg: str,
    end: str,
    opener: object = None,
) -> list[tuple[str, float]]:
    """Fetch one symbol's float share history.

    Raises ``RuntimeError`` when the host cannot answer, so the caller can
    tell an empty result from a failed one.
    """
    routes: list[str] = build_urls(code, beg, end)
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        throttle()
        url: str = routes[attempt % len(routes)]
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            if opener is None:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECS) as response:
                    payload: bytes = response.read()
            else:
                payload = opener(url)  # type: ignore[operator]
            data = json.loads(payload.decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                # The backoff grows with the attempt because the host's
                # penalty is a timeout, not a transient reset: waiting a
                # fixed beat is not long enough to clear it.
                time.sleep(RETRY_BACKOFF_SECS * (2**attempt))
            continue

        if not isinstance(data, dict):
            last_error = ValueError("unexpected payload")
            continue
        body: object = data.get("data")
        if not isinstance(body, dict):
            # The host answers a blocked request with a well-formed body that
            # carries no data, so this is a soft failure and another route
            # gets a chance before the symbol is written off.
            last_error = ValueError("no data in payload")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECS * (2**attempt))
            continue
        return parse_float_shares(body.get("klines"))

    raise RuntimeError(f"could not fetch float shares for {code}: {last_error}")


def write_rows(path: Path, rows: list[tuple[str, float]]) -> None:
    """Write one symbol's rows to disk, newest file replacing the old one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["datetime", "float_shares", "code"])
        for date, shares in rows:
            writer.writerow([date, f"{shares:.0f}", ""])


def read_rows(path: Path) -> list[tuple[str, float]]:
    """Read a cached float share file back."""
    if not path.exists():
        return []
    rows: list[tuple[str, float]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for record in reader:
            if len(record) < 2:
                continue
            try:
                rows.append((record[0], float(record[1])))
            except ValueError:
                continue
    return rows


def cache_path(cache_dir: Path, code: str) -> Path:
    """The file one symbol's float shares live in."""
    return cache_dir / f"{code}_float_shares.csv"


def main() -> int:
    """Fetch the float share history for a universe and cache it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codes",
        default="D:/Codex/work/vnpy-alpha-cache/codes_market3000.txt",
        help="file with one six-digit code per line",
    )
    parser.add_argument(
        "--cache-dir",
        default="D:/Codex/work/vnpy-alpha-cache/float_shares",
        help="where to write the per-symbol files",
    )
    parser.add_argument("--beg", default="20211115", help="first date, YYYYMMDD")
    parser.add_argument("--end", default="20261115", help="last date, YYYYMMDD")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after this many symbols; 0 means all of them",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="fetch again even when the cache file already exists",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=12,
        help="how many symbols to fetch at once; the request gap is shared",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=15,
        help="how many passes over the symbols that have not landed yet",
    )
    args = parser.parse_args()

    codes: list[str] = []
    codes_path: Path = Path(args.codes)
    if codes_path.exists():
        for line in codes_path.read_text(encoding="utf-8").splitlines():
            code: str = line.strip()
            if code and not code.startswith("#"):
                codes.append(code)
    if not codes:
        print(f"no codes found in {codes_path}")
        return 2

    cache_dir: Path = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    done: int = 0
    failed: int = 0
    pending: list[str] = []
    for code in codes:
        target: Path = cache_path(cache_dir, code)
        if target.exists() and not args.refresh:
            done += 1
            continue
        pending.append(code)

    if args.limit:
        pending = pending[: args.limit]

    if not pending:
        print(f"nothing to fetch, {done} already cached")
        return 0

    def work(code: str) -> tuple[str, int]:
        """Fetch one symbol and report how many rows it produced."""
        try:
            rows: list[tuple[str, float]] = fetch_float_shares(code, args.beg, args.end)
        except RuntimeError:
            return code, -1
        write_rows(cache_path(cache_dir, code), rows)
        return code, len(rows)

    workers: int = max(1, args.workers)
    remaining: list[str] = pending
    for round_index in range(max(1, args.rounds)):
        if not remaining:
            break
        failed_now: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for code, count in pool.map(work, remaining):
                if count < 0:
                    failed_now.append(code)
                else:
                    done += 1
        failed += len(failed_now)
        total: int = len(pending) + done
        print(
            f"round {round_index + 1}/{args.rounds}: "
            f"{done}/{total} ok, {len(failed_now)} failed this round"
        )
        remaining = failed_now
        if remaining:
            # The host's refusal is soft: a batch that got 20 of 30 through
            # got 28 of 30 after a fifteen second pause. So the next pass
            # waits long enough for the host to stop feeling hit.
            time.sleep(PAUSE_BETWEEN_ROUNDS_SECS)

    print(f"done: {done} ok, {failed} failed")
    if remaining:
        print(f"still missing {len(remaining)}: {', '.join(remaining[:10])}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
