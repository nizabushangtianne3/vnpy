"""Grade the extreme deciles on their own, apart from the diluted band.

The ten-decile reading shows the ranking is not spread evenly across the
cross-section: deciles one through nine move in a shallow monotone line and
the whole of the signal sits in the last one. A band that holds a percentile
slice therefore dilutes the one decile that carries the ranking with the eight
that do not, and the share of dilution changes with the market, which is what
made the band edge read as a sign-flipping knob instead of a dilution dial.

This grades the book the deciles actually support: long the top decile, short
the bottom one, and charge every name that has to be traded to do it. The
rates are the ones ``alpha_ls_cost`` uses -- three basis points to buy and
thirteen to sell -- so the net reading is comparable with that script's.

Usage::

    python scripts/alpha_extreme_decile.py [--top 0.10] [--bottom 0.10]
        [--rebalance 30] [--json out.json]
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
    """Extreme-decile spread, gross and net of entry and exit rates."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {d: g.sort("signal", descending=True) for d, g in window.group_by("datetime")}
    dates = sorted(groups)
    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    gross: list[float] = []
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
            # One rate is paid per side of the trade, scaled by how much of the
            # book that side is, so a book that changes half its names pays half
            # a full turnover in cost.
            cost = (entered * BUY_RATE + left * SELL_RATE) / book
            held_long, held_short = new_long, new_short
        if not held_long or not held_short:
            continue
        longs = group.filter(pl.col("vt_symbol").is_in(list(held_long)))
        shorts = group.filter(pl.col("vt_symbol").is_in(list(held_short)))
        long_rets = [x for x in longs["ret"].to_list() if x is not None]
        short_rets = [x for x in shorts["ret"].to_list() if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spread = float(np.mean(long_rets)) - float(np.mean(short_rets))
        gross.append(spread)
        costs.append(cost)
        net.append(spread - cost)
    sample = np.array(gross)
    netted = np.array(net)
    g_mean, g_t, g_p = grade(sample)
    _, g_t_nw, g_p_nw = newey_west_t(sample, 10)
    n_mean, n_t, n_p = grade(netted)
    _, n_t_nw, n_p_nw = newey_west_t(netted, 10)
    sd = float(sample.std(ddof=1)) if len(sample) > 1 else 0.0
    return {
        "n": int(len(sample)),
        "gross_mean_bp": g_mean * 10000,
        "gross_t": g_t,
        "gross_p": g_p,
        "gross_t_nw": g_t_nw,
        "gross_p_nw": g_p_nw,
        "net_mean_bp": n_mean * 10000,
        "net_t": n_t,
        "net_p": n_p,
        "net_t_nw": n_t_nw,
        "net_p_nw": n_p_nw,
        "total_bp": float(netted.sum()) * 10000,
        "cost_bp": float(np.sum(costs)) * 10000,
        "annual_vol_pct": sd * sqrt(SESSIONS_PER_YEAR) * 100,
        "ir": g_mean / sd * sqrt(SESSIONS_PER_YEAR) if sd else 0.0,
        "net_ir": n_mean
        / (float(netted.std(ddof=1)) * sqrt(SESSIONS_PER_YEAR))
        if len(netted) > 1 and netted.std(ddof=1)
        else 0.0,
        "annual_turnover_x": float(np.mean(turnover) * SESSIONS_PER_YEAR / rebalance)
        if turnover
        else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extreme-decile spread grading")
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
        ("top0.05/bot0.05", 0.05, 0.05),
        ("top0.10/bot0.10", 0.10, 0.10),
        ("top0.20/bot0.20", 0.20, 0.20),
        ("top0.10/bot0.05", 0.10, 0.05),
        ("top0.05/bot0.10", 0.05, 0.10),
    ]:
        for name, start, end in WINDOWS:
            key = f"{label} {name}"
            report[key] = run(
                joined, start, end, top, bottom, args.buffer_ratio, args.rebalance
            )

    print()
    print("极端档多空价差：T 决策 / T+1 生效，非重叠 1 日收盘收益")
    print(
        f"{'config':26} {'n':>5} {'gross bp':>9} {'g_tNW':>7} {'net bp':>8} {'n_tNW':>7} "
        f"{'n_pNW':>7} {'cost bp':>8} {'vol%':>6} {'netIR':>6} {'turn/x':>7}"
    )
    for key, value in report.items():
        print(
            f"{key:26} {value['n']:5d} {value['gross_mean_bp']:9.2f} "
            f"{value['gross_t_nw']:7.2f} {value['net_mean_bp']:8.2f} "
            f"{value['net_t_nw']:7.2f} {value['net_p_nw']:7.3f} "
            f"{value['cost_bp']:8.1f} {value['annual_vol_pct']:6.2f} "
            f"{value['net_ir']:6.2f} {value['annual_turnover_x']:7.1f}",
            flush=True,
        )
    print()
    print("逐年（top0.05/bot0.05，net）")
    yearly: dict[str, dict] = {}
    for name, start, end in YEARS:
        key = f"top0.05/bot0.05 {name}"
        yearly[key] = run(joined, start, end, 0.05, 0.05, args.buffer_ratio, args.rebalance)
        value = yearly[key]
        print(
            f"{name} n={value['n']:4d} gross {value['gross_mean_bp']:7.2f}bp "
            f"(tNW {value['gross_t_nw']:5.2f})  net {value['net_mean_bp']:7.2f}bp "
            f"(tNW {value['net_t_nw']:5.2f}, p {value['net_p_nw']:.3f})",
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
