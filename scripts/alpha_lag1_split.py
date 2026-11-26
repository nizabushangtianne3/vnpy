
"""Split the executable return into the overnight jump and the intraday move.

The daily book earns its return from the open of the session after the decision
to the open of the session after that. That span contains two different
economics: the gap between the previous close and this open, which is where
overnight information is priced, and the move from this open to the next open,
which contains one full intraday session. A signal computed from a session's
close is only live until that close, so crediting it with overnight moves is
defensible and crediting it with the intraday session is not.

This grades both legs separately on the same ranking and the same book, so the
split is a measurement rather than an assumption. If the edge is in the
overnight gap, the book is executable at the open and the return is earned by
the gap. If the edge is in the intraday move, the book is earning a session's
direction on a signal that stopped at the previous close, which is a different
and weaker claim.

Usage::

    python scripts/alpha_lag1_split.py [--json out.json]
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

COST: float = 0.0016

WINDOWS = [
    ("full", "2022-01-20", "2026-11-13"),
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
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


def leg_returns(daily: Path) -> pl.DataFrame:
    """Per-session overnight gap and the open-to-open span that contains it."""
    rows: list[pl.DataFrame] = []
    for path in sorted(daily.glob("*.parquet")):
        frame = pl.read_parquet(
            path, columns=["datetime", "open", "close"]
        ).sort("datetime")
        if frame.height < 8:
            continue
        frame = frame.with_columns(
            # The jump from the decision session's close into the next open.
            (pl.col("open").shift(-1) / pl.col("close") - 1.0).alias("gap"),
            # The span from that open to the open one session later.
            (pl.col("open").shift(-2) / pl.col("open").shift(-1) - 1.0).alias("span"),
            pl.lit(path.stem).alias("vt_symbol"),
        )
        rows.append(frame.select("datetime", "vt_symbol", "gap", "span"))
    return pl.concat(rows, how="vertical")


def spread_series(
    joined: pl.DataFrame, start: str, end: str, long_rate: float, short_rate: float,
    column: str,
) -> dict:
    """Median spread of a daily book on one named return leg, with turnover."""
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
        group = groups[date].filter(pl.col(column).is_not_null())
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
        values = group[column].to_list()
        long_rets = [x for x in values[:long_end] if x is not None]
        short_rets = [x for x in values[short_start:] if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spreads.append(float(np.median(long_rets)) - float(np.median(short_rets)))
        shares.append((entered + left) / 2.0 / book)
    return {
        "spreads": np.array(spreads),
        "turnover_share": float(np.mean(shares)) if shares else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Lag-one return legs")
    parser.add_argument("--signal", default="")
    parser.add_argument("--long-rate", type=float, default=0.01)
    parser.add_argument("--short-rate", type=float, default=0.01)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    print("signal:", signal_path, flush=True)
    bars = leg_returns(daily)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    print(
        f"band L{int(args.long_rate * 100)}/S{int(args.short_rate * 100)}  "
        f"join rows: {joined.height}",
        flush=True,
    )

    report: dict[str, dict] = {}
    print()
    print("隔夜跳空 vs 开对开全段（band 1%，成本 16bp × 当日变动比例）")
    print(
        f"{'window':10} {'leg':6} {'gross bp':>10} {'t_NW':>7} {'net bp':>9} "
        f"{'net tNW':>8} {'turn/日':>8}"
    )
    for name, start, end in WINDOWS:
        for column, label in [("gap", "隔夜"), ("span", "开对开")]:
            outcome = spread_series(
                joined, start, end, args.long_rate, args.short_rate, column
            )
            sample = outcome["spreads"]
            share = outcome["turnover_share"]
            gross = float(sample.mean()) * 10000
            _, g_t_nw, _ = newey_west_t(sample, 10)
            netted = sample - COST * share
            n_mean = float(netted.mean()) * 10000
            _, n_t_nw, _ = newey_west_t(netted, 10)
            print(
                f"{name:10} {label:6} {gross:10.2f} {g_t_nw:7.2f} {n_mean:9.2f} "
                f"{n_t_nw:8.2f} {share * 100:7.2f}%",
                flush=True,
            )
            report[f"{name} {label}"] = {
                "gross_mean_bp": gross,
                "gross_t_nw": float(g_t_nw),
                "net_mean_bp": n_mean,
                "net_t_nw": float(n_t_nw),
                "turnover_share": float(share),
                "n": int(len(sample)),
            }
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
