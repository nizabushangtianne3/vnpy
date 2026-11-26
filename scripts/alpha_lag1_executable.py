
"""Grade the lag-one book on the return it can actually execute on.

The lag-one reading uses the closing price of the session after the decision as
the entry price, which is not available: a signal computed from session T's
closing prices can only be acted on at session T+1's opening price, and the
return it earns is from that open to the close one session later. Using the
close-to-close return flatters the book by one day of drift at exactly the
moment the book has to trade.

This grades the executable version. The entry bar is the next session's open,
the exit bar is the following session's open, and both legs are median members
of the ranking taken on the decision session. Everything else is held constant,
so the difference between the two readings is the cost of not being able to
trade at the close the signal was computed from.

Usage::

    python scripts/alpha_lag1_executable.py [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from math import erf, sqrt
from pathlib import Path

import numpy as np
import polars as pl

from alpha_ls_newey import newey_west_t
from alpha_ls_window import find_cache

SESSIONS_PER_YEAR: float = 244.0
COSTS = [0.0, 0.0016, 0.0030]

WINDOWS = [
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
    ("full", "2022-01-20", "2026-11-13"),
    ("2022-2024", "2022-01-20", "2024-12-31"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
]

YEARS = [
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026", "2026-01-01", "2026-11-13"),
]


def grade(sample: np.ndarray) -> tuple[float, float, float]:
    if len(sample) < 3:
        return float(sample.mean()) if len(sample) else 0.0, float("nan"), float("nan")
    mean = float(sample.mean())
    sd = float(sample.std(ddof=1))
    if sd == 0:
        return mean, 0.0, 1.0
    t = mean / sd * sqrt(len(sample))
    return mean, t, 1.0 - erf(abs(t) / sqrt(2))


def open_returns(daily: Path, hold: int = 1) -> pl.DataFrame:
    """Return from the open of the session after the decision, over ``hold``."""
    rows: list[pl.DataFrame] = []
    for path in sorted(daily.glob("*.parquet")):
        frame = pl.read_parquet(
            path, columns=["datetime", "open"]
        ).sort("datetime")
        if frame.height < hold + 3:
            continue
        frame = frame.with_columns(
            (pl.col("open").shift(-1) * 0.0 + pl.col("open")).alias("_entry_open"),
            pl.lit(path.stem).alias("vt_symbol"),
        )
        # Entry is the open of the session after the decision; the exit is the
        # open ``hold`` sessions after that, so the hold period is measured in
        # opens rather than in closes.
        entry = pl.col("open").shift(-1)
        exit_ = pl.col("open").shift(-1 - hold)
        frame = frame.with_columns((exit_ / entry - 1.0).alias("fwd"))
        rows.append(frame.select("datetime", "vt_symbol", "fwd"))
    return pl.concat(rows, how="vertical")


def spread_series(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
) -> dict:
    """Median spread of the executable book plus the turnover it actually pays."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {
        d[0] if isinstance(d, tuple) else d: g.sort("signal", descending=True)
        for d, g in window.group_by("datetime")
    }
    spreads: list[float] = []
    shares: list[float] = []
    held_long: set[str] = set()
    held_short: set[str] = set()
    for date in sorted(groups):
        group = groups[date].filter(pl.col("fwd").is_not_null())
        n = group.height
        if n < 300:
            continue
        universe = group["vt_symbol"].to_list()
        long_end = max(int(n * long_rate), 1)
        short_start = n - max(int(n * short_rate), 1)
        new_long = set(universe[:long_end])
        new_short = set(universe[short_start:])
        entered = len(new_long - held_long) + len(new_short - held_short)
        left = len((held_long - new_long) | (held_short - new_short))
        book = (len(new_long) + len(new_short)) or 1
        held_long, held_short = new_long, new_short
        long_rets = [x for x in group["fwd"].to_list()[:long_end] if x is not None]
        short_rets = [x for x in group["fwd"].to_list()[short_start:] if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spreads.append(float(np.median(long_rets)) - float(np.median(short_rets)))
        shares.append((entered + left) / 2.0 / book)
    return {
        "spreads": np.array(spreads),
        "turnover_share": float(np.mean(shares)) if shares else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Executable lag-one book")
    parser.add_argument("--signal", default="")
    parser.add_argument("--hold", type=int, default=1)
    parser.add_argument("--long-rate", type=float, default=0.05)
    parser.add_argument("--short-rate", type=float, default=0.05)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    print("signal:", signal_path, flush=True)
    bars = open_returns(daily, args.hold)
    print("open-return rows:", bars.height, flush=True)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    print(
        f"band L{int(args.long_rate * 100)}/S{int(args.short_rate * 100)}  "
        f"hold {args.hold} session(s)  join rows: {joined.height}",
        flush=True,
    )

    names = [w[0] for w in WINDOWS] + [y[0] for y in YEARS]
    series: dict[str, dict] = {}
    for name in names:
        for window_name, start, end in WINDOWS + YEARS:
            if window_name == name:
                series[name] = spread_series(
                    joined, start, end, args.long_rate, args.short_rate
                )

    print()
    print(f"全窗口每日换手比例 {series['full']['turnover_share'] * 100:.2f}%"
          f"（年化 {series['full']['turnover_share'] * 244:.0f}x）")
    print()
    print("净 bp/日 与净 Newey-West t（开价进、开价出，T 决策 / T+1 开盘成交）")
    header = f"{'window':10}" + "".join(f"{c * 10000:>13.1f}bp" for c in COSTS)
    print(header)
    report: dict[str, dict] = {}
    for name in names:
        sample = series[name]["spreads"]
        share = series[name]["turnover_share"]
        row = f"{name:10}"
        report[name] = {"turnover_share": share}
        for cost in COSTS:
            netted = sample - cost * share
            mean, t, p = grade(netted)
            _, t_nw, p_nw = newey_west_t(netted, 10)
            row += f"{mean * 10000:>7.1f}/{t_nw:<5.1f}"
            report[name][f"{cost * 10000:.1f}bp"] = {
                "net_mean_bp": float((sample - cost * share).mean()) * 10000,
                "net_total_pct": float(np.prod(1.0 + netted) - 1.0) * 100,
                "net_t_nw": float(t_nw),
            }
        print(row, flush=True)
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
