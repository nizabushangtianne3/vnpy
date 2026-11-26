"""Grade the book the median ranking supports, without the fat tail in it.

The decile medians are monotone across all ten groups while the decile means
are not: on the full window the medians run -0.353, -0.276, -0.229, -0.202,
-0.177, -0.163, -0.144, -0.128, -0.092, +0.187 percent, every step improving,
while the last decile's mean of +0.705 percent falls to -0.206 percent once its
own top ten percent is trimmed away. The signal orders the typical name
reliably and it does not order the extremes, which is the opposite of what a
book that chases the top slice assumes.

This grades two books side by side on the same sessions:

1. ``mean`` -- the equal-weight basket of each slice, the ruler the previous
   notes used, which lets the fattest tail inside the slice set its reading.
2. ``median`` -- the median member of each slice, which cannot be moved by any
   single name and is therefore the reading the ranking itself supports.

The gap between them is the size of the tail that has been charged for a
signal that never claimed to predict it, and it is the reason the extreme
slices graded worse than the band rather than better.

Usage::

    python scripts/alpha_median_book.py [--json out.json]
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

BUY_RATE: float = 0.0003
SELL_RATE: float = 0.0013
SESSIONS_PER_YEAR: float = 244.0

WINDOWS = [
    ("2022-2024", "2022-01-20", "2024-12-31"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
    ("full", "2022-01-20", "2026-11-13"),
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
]

YEARS = [
    ("2022", "2022-01-20", "2022-12-30"),
    ("2023", "2023-01-01", "2023-12-29"),
    ("2024", "2024-01-02", "2024-12-31"),
    ("2025", "2025-01-02", "2025-12-31"),
    ("2026", "2026-01-05", "2026-11-13"),
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


def run(
    joined: pl.DataFrame,
    start: str,
    end: str,
    top: float,
    bottom: float,
    buffer_ratio: float,
    rebalance: int,
) -> dict:
    """Mean-basket and median-member spreads on the same banded selection."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {d: g.sort("signal", descending=True) for d, g in window.group_by("datetime")}
    dates = sorted(groups)
    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    mean_spread: list[float] = []
    median_spread: list[float] = []
    net: list[float] = []
    costs: list[float] = []
    turnover: list[float] = []
    for date in dates:
        group = groups[date].filter(pl.col("ret").is_not_null())
        n = group.height
        if n < 300:
            continue
        since += 1
        cost = 0.0
        if since >= rebalance:
            since = 0
            universe = group["vt_symbol"].to_list()
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
            turnover.append((entered + left) / 2.0 / book)
            cost = (entered * BUY_RATE + left * SELL_RATE) / book
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
        mean_spread.append(float(np.mean(long_rets)) - float(np.mean(short_rets)))
        median_spread.append(
            float(np.median(long_rets)) - float(np.median(short_rets))
        )
        costs.append(cost)
        net.append(median_spread[-1] - cost)
    mean_sample = np.array(mean_spread)
    median_sample = np.array(median_spread)
    net_sample = np.array(net)
    m_mean, m_t, m_p = grade(mean_sample)
    _, m_t_nw, m_p_nw = newey_west_t(mean_sample, 10)
    d_mean, d_t, d_p = grade(median_sample)
    _, d_t_nw, d_p_nw = newey_west_t(median_sample, 10)
    n_mean, n_t, n_p = grade(net_sample)
    _, n_t_nw, n_p_nw = newey_west_t(net_sample, 10)
    sd = float(median_sample.std(ddof=1)) if len(median_sample) > 1 else 0.0
    return {
        "n": int(len(mean_sample)),
        "mean_mean_bp": m_mean * 10000,
        "mean_t": m_t,
        "mean_t_nw": m_t_nw,
        "mean_p_nw": m_p_nw,
        "median_mean_bp": d_mean * 10000,
        "median_t": d_t,
        "median_t_nw": d_t_nw,
        "median_p_nw": d_p_nw,
        "net_mean_bp": n_mean * 10000,
        "net_t": n_t,
        "net_t_nw": n_t_nw,
        "net_p_nw": n_p_nw,
        "total_bp": float(net_sample.sum()) * 10000,
        "cost_bp": float(np.sum(costs)) * 10000,
        "annual_vol_pct": sd * sqrt(SESSIONS_PER_YEAR) * 100,
        "net_ir": n_mean / (float(net_sample.std(ddof=1)) * sqrt(SESSIONS_PER_YEAR))
        if len(net_sample) > 1 and net_sample.std(ddof=1)
        else 0.0,
        "annual_turnover_x": float(np.mean(turnover) * SESSIONS_PER_YEAR / rebalance)
        if turnover
        else 0.0,
        "tail_gap_bp": (m_mean - d_mean) * 10000,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Median-book grading")
    parser.add_argument("--signal", default="")
    parser.add_argument("--buffer-ratio", type=float, default=0.10)
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
    for label, top, bottom in [
        ("L5/S5", 0.05, 0.05),
        ("L10/S10", 0.10, 0.10),
        ("L20/S20", 0.20, 0.20),
    ]:
        for name, start, end in WINDOWS:
            key = f"{label} {name}"
            report[key] = run(
                joined, start, end, top, bottom, args.buffer_ratio, args.rebalance
            )
    print()
    print("等权均值簿 vs 中位数成员簿（T 决策 / T+1 生效，非重叠 1 日收盘收益）")
    print(
        f"{'config':20} {'n':>5} {'mean bp':>9} {'m_tNW':>7} {'med bp':>8} {'d_tNW':>7} "
        f"{'d_pNW':>7} {'net bp':>8} {'n_tNW':>7} {'n_pNW':>7} {'tailgap':>8} {'netIR':>6}"
    )
    for key, value in report.items():
        print(
            f"{key:20} {value['n']:5d} {value['mean_mean_bp']:9.2f} "
            f"{value['mean_t_nw']:7.2f} {value['median_mean_bp']:8.2f} "
            f"{value['median_t_nw']:7.2f} {value['median_p_nw']:7.3f} "
            f"{value['net_mean_bp']:8.2f} {value['net_t_nw']:7.2f} "
            f"{value['net_p_nw']:7.3f} {value['tail_gap_bp']:8.2f} "
            f"{value['net_ir']:6.2f}",
            flush=True,
        )
    print()
    print("逐年（L10/S10 中位数成员簿）")
    yearly: dict[str, dict] = {}
    for name, start, end in YEARS:
        key = f"L10/S10 {name}"
        yearly[key] = run(
            joined, start, end, 0.10, 0.10, args.buffer_ratio, args.rebalance
        )
        value = yearly[key]
        print(
            f"{name} n={value['n']:4d} mean {value['mean_mean_bp']:7.2f}bp "
            f"median {value['median_mean_bp']:7.2f}bp (tNW {value['median_t_nw']:5.2f}) "
            f"net {value['net_mean_bp']:7.2f}bp (tNW {value['net_t_nw']:5.2f})",
            flush=True,
        )
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"report": report, "yearly": yearly}, ensure_ascii=False, indent=1)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
