"""Turn cached A-share daily bars into the AlphaLab research store.

The alpha pipeline in :mod:`vnpy.alpha` is the part of this repository that
already does what an A-share stock quant stack does: cross-sectional factors,
 a fitted model, a signal, and a portfolio backtest. It expects its input as
 per-symbol parquet under ``<lab>/daily``, a contract table naming commission
 and lot size per symbol, and an index-component table giving which symbols
 traded on which day. This module produces exactly those three from the bars
 the public sources already cached, so no manual export step stands between a
 campaign and a verdict.

The component table is derived rather than fetched, and that choice is worth
stating plainly: the cached bars are what the fetch actually found, so a
symbol is listed on the days it has a bar for. That is a "was it trading"
 filter, not a CSI 300 membership list, and it is honest about the difference
 - a screened universe is what this backs, not an index replication.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


from vnpy.alpha import AlphaLab
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData


DEFAULT_BARS_DIR: str = ".vntrader/stock_bars"

# Real A-share costs, in fractions of turnover. The buy side carries the
# stamp-free commission plus a small slippage allowance; the sell side adds the
# 0.05 percent stamp duty, which is why the two rates are not symmetric. These
# are the figures the demo strategy in ``vnpy.alpha`` uses as well, restated
# here so the loader can build contract settings without a second source.
LONG_RATE: float = 0.0005
SHORT_RATE: float = 0.0015

# An A-share board lot is 100 shares. Anything below one lot cannot be traded
# at all, and the backtester rounds positions to multiples of this, so a value
# under 100 would silently book fractional lots the exchange would reject.
MIN_LOT: int = 100

# Shanghai's tick grid is 0.01 yuan. Shenzhen uses the same for the price band
# this pipeline screens, so one value serves both rather than a per-exchange map.
PRICE_TICK: float = 0.01


def _exchange_for(code: str) -> Exchange:
    """Map a bare symbol to its exchange through the range its code lives in."""
    if code.startswith("6") or code.startswith("900") or code.startswith("688"):
        return Exchange.SSE
    if code.startswith("920") or code.startswith("4") or code.startswith("8"):
        return Exchange.BSE
    return Exchange.SZSE


def read_cached_bars(code: str, bars_dir: str | Path = DEFAULT_BARS_DIR) -> list[BarData]:
    """Read one symbol's cached daily bars.

    The cache is the CSV the fetch writes: ``datetime,open,close,high,low,
    volume,symbol``. Column order matters because that is the writer's order,
    not the header's alphabetical one, so the reader is explicit rather than
    sniffing and guessing positionally.
    """
    # The secid encodes the market, so the file is named by it rather than by
    # the bare code; a caller that only has the code still finds it.
    candidates: list[Path] = sorted(Path(bars_dir).glob(f"*_{code}_101.csv"))
    if not candidates:
        raise FileNotFoundError(f"no cached bars for {code}")
    path: Path = candidates[0]
    exchange: Exchange = _exchange_for(code)
    bars: list[BarData] = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts: list[str] = line.split(",")
        if len(parts) < 6:
            continue
        try:
            bars.append(
                BarData(
                    symbol=code,
                    exchange=exchange,
                    datetime=datetime.fromisoformat(parts[0]),
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
        except (IndexError, TypeError, ValueError):
            continue
    return bars


def load_universe_codes(bars_dir: str | Path = DEFAULT_BARS_DIR) -> list[str]:
    """List every symbol the cache holds a series for."""
    codes: list[str] = []
    seen: set[str] = set()
    for path in Path(bars_dir).glob("*.csv"):
        parts: list[str] = path.stem.split("_")
        if len(parts) < 3:
            continue
        code: str = parts[1]
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return sorted(codes)


def build_lab(
    lab_path: str | Path,
    bars_dir: str | Path = DEFAULT_BARS_DIR,
    min_bars: int = 120,
    index_symbol: str = "000300.SSE",
) -> tuple[AlphaLab, list[str]]:
    """Convert the cached bars into the AlphaLab store, and list what it holds.

    A symbol with fewer than ``min_bars`` daily bars cannot warm up a
    lookback, so it is skipped rather than loaded and then silently dropped by
    the dataset's own filters. The contract table and the component table are
    written for the same set that makes it in, which keeps the three stores
    consistent with each other instead of trusting the caller to reconcile
    them.
    """
    lab: AlphaLab = AlphaLab(str(lab_path))

    # Each series is read once and reused for the parquet store and for the
    # component table, because reading a thousand files twice to build two
    # views of the same data is the kind of cost that shows up in a campaign
    # nobody is watching.
    series: dict[str, list[BarData]] = {}
    for code in load_universe_codes(bars_dir):
        bars: list[BarData] = read_cached_bars(code, bars_dir)
        if len(bars) < min_bars:
            continue
        series[code] = bars
        lab.save_bar_data(bars)

    codes: list[str] = sorted(series)
    for code in codes:
        vt_symbol: str = f"{code}.{_exchange_for(code).value}"
        lab.add_contract_setting(
            vt_symbol=vt_symbol,
            long_rate=LONG_RATE,
            short_rate=SHORT_RATE,
            size=1.0,
            pricetick=PRICE_TICK,
        )

    components: dict[str, list[str]] = {}
    for code in codes:
        vt_symbol = f"{code}.{_exchange_for(code).value}"
        for bar in series[code]:
            day: str = bar.datetime.date().isoformat()
            components.setdefault(day, []).append(vt_symbol)

    lab.save_component_data(index_symbol, components)
    return lab, codes
