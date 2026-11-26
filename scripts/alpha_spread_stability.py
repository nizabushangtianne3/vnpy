"""Grade the signal session by session instead of window by window.

Every reading so far reduces the spread to two numbers, one for the fit window
and one for the hold window, and both of those averages hide the way the edge
actually behaves. A signal that loses 1 basis point a week and a signal that is
flat for eight months and then loses 2 percent in one month average out to the
same pair of numbers.

This script keeps the per-session series and reports three things an average
cannot:

1. ``monthly`` -- the compounding of each calendar month, so a window that is
   only carried by two months is visible as two months.
2. ``drawdown`` -- the worst peak-to-trough of the cumulative series, which is
   what the book has to survive to reach the average.
3. ``decay`` -- the mean of the first half of the hold window against the mean
   of the second half, which is the direct measurement of whether the fitted
   model goes stale and, if it does, how fast.

Usage::

    python scripts/alpha_spread_stability.py [--rebalance 30] [--json out.json]
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
from alpha_ls_window import find_cache, load_returns

SESSIONS_PER_YEAR: float = 244.0
WINDOW_START = "2022-01-20"
WINDOW_END = "2026-11-13"


def grade(sample: np.ndarray) -> tuple[float, float, float]:
    if len(sample) < 3:
        return float(sample.mean()) if len(sample) else 0.0, float("nan"), float("nan")
    mean = float(sample.mean())
    sd = float(sample.std(ddof=1))
    if sd == 0:
        return mean, 0.0, 1.0
    t = mean / sd * sqrt(len(sample))
    return mean, t, 1.0 - erf(abs(t) / sqrt(2))


def build(
    joined: pl.DataFrame,
    top: float,
    bottom: float,
    buffer_ratio: float,
    rebalance: int,
    median: bool,
) -> pl.DataFrame:
    """One row per session: date, spread, and the cost charged that session."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(WINDOW_START).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(WINDOW_END).str.to_datetime())
    ).sort("datetime")
    # ``group_by`` yields the key as a tuple, so the date has to be taken out
    # of it or every later comparison sees a sequence rather than a value.
    groups = {
        d[0] if isinstance(d, tuple) else d: g.sort("signal", descending=True)
        for d, g in window.group_by("datetime")
    }
    dates = sorted(groups)
    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    rows: list[dict] = []
    for date in dates:
        group = groups[date].filter(pl.col("ret").is_not_null())
        if group.height < 300:
            continue
        since += 1
        cost = 0.0
        entered = left = 0
        if since >= rebalance:
            since = 0
            universe = group["vt_symbol"].to_list()
            n = group.height
            long_end = max(int(n * top), 1)
            short_start = min(int(n * (1 - bottom)), n - 1)
            long_band = range(
                max(0, long_end - int(n * buffer_ratio)),
                min(n, long_end + int(n * buffer_ratio)),
            )
            short_band = range(
                max(0, short_start - int(n * buffer_ratio)),
                min(n, short_start + int(n * buffer_ratio)),
            )
            new_long = (held_long & set(universe[i] for i in long_band)) | set(
                universe[:long_end]
            )
            new_short = (held_short & set(universe[i] for i in short_band)) | set(
                universe[short_start:]
            )
            entered = len(new_long - held_long) + len(new_short - held_short)
            left = len((held_long - new_long) | (held_short - new_short))
            book = (len(new_long) + len(new_short)) or 1
            cost = (entered * 0.0003 + left * 0.0013) / book
            held_long, held_short = new_long, new_short
        if not held_long or not held_short:
            continue
        long_rets = group.filter(
            pl.col("vt_symbol").is_in(list(held_long))
        )["ret"].to_numpy()
        short_rets = group.filter(
            pl.col("vt_symbol").is_in(list(held_short))
        )["ret"].to_numpy()
        long_rets = long_rets[~np.isnan(long_rets)]
        short_rets = short_rets[~np.isnan(short_rets)]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        stat = np.median if median else np.mean
        spread = float(stat(long_rets)) - float(stat(short_rets))
        rows.append(
            {
                "datetime": date,
                "spread": spread,
                "cost": cost,
                "entered": entered,
                "left": left,
            }
        )
    frame = pl.DataFrame(
        rows,
        schema={
            "datetime": pl.Datetime,
            "spread": pl.Float64,
            "cost": pl.Float64,
            "entered": pl.Int64,
            "left": pl.Int64,
        },
    )
    return frame.sort("datetime")


def summarise(frame: pl.DataFrame, label: str) -> dict:
    spreads = frame["spread"].to_numpy()
    net = spreads - frame["cost"].to_numpy()
    mean, t, p = grade(spreads)
    _, t_nw, p_nw = newey_west_t(spreads, 10)
    n_mean, n_t, n_p = grade(net)
    _, n_t_nw, n_p_nw = newey_west_t(net, 10)
    cumulative = np.cumprod(1.0 + net) - 1.0
    peak = np.maximum.accumulate(cumulative)
    drawdown = float((cumulative - peak).min() * 100)
    monthly = (
        frame.with_columns(
            # ``dt.strftime`` resolves to ``to_string`` on this polars build and
            # refuses a datetime, so the month key is assembled from the
            # integer parts instead.
            pl.concat_str(
                [
                    pl.col("datetime").dt.year().cast(pl.Utf8),
                    pl.lit("-"),
                    pl.col("datetime").dt.month().cast(pl.Utf8).str.pad_start(2, "0"),
                ]
            ).alias("month")
        )
        .group_by("month")
        .agg(
            pl.col("spread").mean().alias("mean"),
            pl.len().alias("n"),
        )
        .sort("month")
    )
    monthly_rows = monthly.with_columns(
        ((1.0 + pl.col("mean")).cum_prod() - 1.0).alias("compound")
    ).to_dicts()
    half = len(spreads) // 2
    first = spreads[:half]
    second = spreads[half:]
    _, f_t, _ = grade(first)
    _, s_t, _ = grade(second)
    sd = float(spreads.std(ddof=1))
    return {
        "label": label,
        "n": int(len(spreads)),
        "gross_mean_bp": mean * 10000,
        "gross_t": t,
        "gross_t_nw": t_nw,
        "gross_p_nw": p_nw,
        "net_mean_bp": n_mean * 10000,
        "net_t_nw": n_t_nw,
        "net_p_nw": n_p_nw,
        "net_total_pct": float(cumulative[-1] * 100),
        "max_dd_pct": drawdown,
        "annual_vol_pct": sd * sqrt(SESSIONS_PER_YEAR) * 100,
        "net_ir": n_mean / (float(net.std(ddof=1)) * sqrt(SESSIONS_PER_YEAR))
        if len(net) > 1 and net.std(ddof=1)
        else 0.0,
        "positive_sessions": float((spreads > 0).mean()),
        "first_half_bp": float(first.mean()) * 10000,
        "first_half_t": f_t,
        "second_half_bp": float(second.mean()) * 10000,
        "second_half_t": s_t,
        "decay_ratio": float(second.mean() / first.mean())
        if first.mean()
        else float("nan"),
        "monthly": monthly_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-session spread stability")
    parser.add_argument("--signal", default="")
    parser.add_argument("--rebalance", type=int, default=30)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    print("signal:", signal_path, flush=True)
    bars = load_returns(daily)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    print("join rows:", joined.height, flush=True)

    report: dict[str, dict] = {}
    frames: dict[str, pl.DataFrame] = {}
    for label, top, bottom, median in [
        ("mean L5/S5", 0.05, 0.05, False),
        ("median L5/S5", 0.05, 0.05, True),
        ("median L10/S10", 0.10, 0.10, True),
    ]:
        frame = build(joined, top, bottom, 0.10, args.rebalance, median)
        frames[label] = frame
        report[label] = summarise(frame, label)

    print()
    print(f"逐日稳定性（全窗口 {WINDOW_START}..{WINDOW_END}，clock {args.rebalance}）")
    print(
        f"{'book':16} {'n':>5} {'gross bp':>9} {'g_tNW':>7} {'net bp':>8} {'n_tNW':>7} "
        f"{'pNW':>7} {'net tot%':>9} {'maxDD%':>8} {'vol%':>6} {'netIR':>6} {'pos%':>6}"
    )
    for key, value in report.items():
        print(
            f"{key:16} {value['n']:5d} {value['gross_mean_bp']:9.2f} "
            f"{value['gross_t_nw']:7.2f} {value['net_mean_bp']:8.2f} "
            f"{value['net_t_nw']:7.2f} {value['net_p_nw']:7.3f} "
            f"{value['net_total_pct']:9.2f} {value['max_dd_pct']:8.2f} "
            f"{value['annual_vol_pct']:6.2f} {value['net_ir']:6.2f} "
            f"{value['positive_sessions'] * 100:6.1f}",
            flush=True,
        )
    print()
    print("前半窗 vs 后半窗（模型陈旧速度）：")
    for key, value in report.items():
        print(
            f"{key:16} 前半 {value['first_half_bp']:7.2f}bp (t {value['first_half_t']:5.2f})  "
            f"后半 {value['second_half_bp']:7.2f}bp (t {value['second_half_t']:5.2f})  "
            f"比值 {value['decay_ratio']:.2f}"
        )
    print()
    print("monthly compound（median L5/S5）：")
    for row in report["median L5/S5"]["monthly"]:
        print(f"  {row['month']}  n={row['n']:3d}  日均 {row['mean'] * 10000:7.2f}bp  "
              f"累计 {row['compound'] * 100:+8.2f}%")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
