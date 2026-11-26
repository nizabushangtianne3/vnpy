
"""Grade the buffer that keeps stale names, as a rank tolerance and not a slice.

The banded selection every earlier script inherited keeps a name that has
fallen outside the band as long as it is still inside ``buffer_ratio`` of the
whole cross-section. A five percent band with a ten percent buffer therefore
retains everything ranked inside the top fifteen percent, which is a turnover
dial that silently widens the book to three times the band it was asked to
hold.

The decile medians say that widening is expensive. On the full window the
medians of the ten signal-ranked deciles are -0.353, -0.276, -0.229, -0.202,
-0.177, -0.163, -0.144, -0.128, -0.092, +0.187 percent a day, so a name that
drifts from the top five percent to the tenth percentile stops contributing
and starts costing. The five-percent book that reads 2.33 basis points a
session should read far closer to the 54 basis points its two end deciles
separate, and the gap is the retention zone.

This scans the buffer as a tolerance in percentage points of the
cross-section, reports the rank the retained book actually occupies, and grades
the median-member spread of each setting. A tight buffer raises turnover, so
the net reading is the one that has to decide it, and the fit and hold halves
are reported separately because a buffer is exactly the kind of dial that fits
one half and inverts on the other.

Usage::

    python scripts/alpha_buffer_diag.py [--json out.json]
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
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
    ("2022-2024", "2022-01-20", "2024-12-31"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
    ("full", "2022-01-20", "2026-11-13"),
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
    long_rate: float,
    short_rate: float,
    buffer_pp: float,
    rebalance: int,
) -> dict:
    """Median-member spread under a retention zone measured in rank points."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {
        d[0] if isinstance(d, tuple) else d: g.sort("signal", descending=True)
        for d, g in window.group_by("datetime")
    }
    dates = sorted(groups)
    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    spreads: list[float] = []
    costs: list[float] = []
    turnover: list[float] = []
    long_ranks: list[float] = []
    short_ranks: list[float] = []
    sizes: list[int] = []
    for date in dates:
        group = groups[date].filter(pl.col("ret").is_not_null())
        n = group.height
        if n < 300:
            continue
        universe = group["vt_symbol"].to_list()
        rank = {sym: i for i, sym in enumerate(universe)}
        # ``n`` is the filtered height used for selection, so it matches the
        # universe the ranks are built from; keeping them from drifting is
        # what makes the reported book rank mean what it says.
        long_end = max(int(n * long_rate), 1)
        short_end = max(int(n * short_rate), 1)
        short_start = n - short_end
        # The retention zone now extends ``buffer_pp`` percentage points past
        # the band edge rather than ``buffer_ratio`` times the whole universe,
        # so a five percent band with a one point buffer holds the top six.
        long_keep = min(n, long_end + int(n * buffer_pp / 100.0))
        short_keep_start = max(0, short_start - int(n * buffer_pp / 100.0))
        since += 1
        cost = 0.0
        if since >= rebalance:
            since = 0
            zone_long = set(universe[:long_keep])
            zone_short = set(universe[short_keep_start:])
            new_long = (held_long & zone_long) | set(universe[:long_end])
            new_short = (held_short & zone_short) | set(universe[short_start:])
            entered = len(new_long - held_long) + len(new_short - held_short)
            left = len((held_long - new_long) | (held_short - new_short))
            book = (len(new_long) + len(new_short)) or 1
            turnover.append((entered + left) / 2.0 / book)
            cost = (entered * BUY_RATE + left * SELL_RATE) / book
            held_long, held_short = new_long, new_short
            sizes.append(book)
        if not held_long or not held_short:
            continue
        # Rank in percent of the cross-section, so the composition of the
        # retained book is visible rather than inferred.
        if held_long:
            present = [rank[sym] for sym in held_long if sym in rank]
            if present:
                long_ranks.append(float(np.mean(present) / n * 100))
        if held_short:
            present = [rank[sym] for sym in held_short if sym in rank]
            if present:
                short_ranks.append(float(np.mean(present) / n * 100))
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
        spreads.append(float(np.median(long_rets)) - float(np.median(short_rets)))
        costs.append(cost)
    stocks = np.array(spreads)
    net = stocks - np.array(costs)
    mean, t, p = grade(stocks)
    _, t_nw, p_nw = newey_west_t(stocks, 10)
    n_mean, n_t, n_p = grade(net)
    _, n_t_nw, n_p_nw = newey_west_t(net, 10)
    cumulative = np.cumprod(1.0 + net) - 1.0
    peak = np.maximum.accumulate(cumulative)
    sd = float(net.std(ddof=1))
    return {
        "n": int(len(stocks)),
        "gross_mean_bp": mean * 10000,
        "gross_t": t,
        "gross_t_nw": t_nw,
        "gross_p_nw": p_nw,
        "net_mean_bp": n_mean * 10000,
        "net_t_nw": n_t_nw,
        "net_p_nw": n_p_nw,
        "net_total_pct": float(cumulative[-1] * 100),
        "max_dd_pct": float((cumulative - peak).min() * 100),
        "annual_vol_pct": float(stocks.std(ddof=1) * sqrt(SESSIONS_PER_YEAR) * 100),
        "net_ir": n_mean / (sd * sqrt(SESSIONS_PER_YEAR)) if sd else 0.0,
        "cost_bp": float(np.sum(costs)) * 10000,
        "annual_turnover_x": float(np.mean(turnover) * SESSIONS_PER_YEAR / rebalance)
        if turnover
        else 0.0,
        "mean_book_names": float(np.mean(sizes)) if sizes else 0.0,
        "held_long_rank_pct": float(np.mean(long_ranks)) if long_ranks else 0.0,
        "held_short_rank_pct": float(np.mean(short_ranks)) if short_ranks else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Buffer tolerance scan")
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
    for buffer_pp in [0.0, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]:
        for name, start, end in WINDOWS:
            key = f"buf {buffer_pp}pp {name}"
            report[key] = run(
                joined, start, end, 0.05, 0.05, buffer_pp, args.rebalance
            )
    print()
    print(f"中位数 L5/S5 簿，buffer 以截面百分点计的保留带（clock {args.rebalance}）")
    print(
        f"{'config':20} {'n':>5} {'gross bp':>9} {'g_tNW':>7} {'pNW':>7} {'net bp':>8} "
        f"{'n_tNW':>7} {'n_pNW':>7} {'net%':>7} {'maxDD%':>8} {'vol%':>6} {'netIR':>6} "
        f"{'turn/x':>7} {'names':>6} {'Lrank%':>7} {'Srank%':>7}"
    )
    for key, value in report.items():
        print(
            f"{key:20} {value['n']:5d} {value['gross_mean_bp']:9.2f} "
            f"{value['gross_t_nw']:7.2f} {value['gross_p_nw']:7.3f} "
            f"{value['net_mean_bp']:8.2f} {value['net_t_nw']:7.2f} "
            f"{value['net_p_nw']:7.3f} {value['net_total_pct']:7.2f} "
            f"{value['max_dd_pct']:8.2f} {value['annual_vol_pct']:6.2f} "
            f"{value['net_ir']:6.2f} {value['annual_turnover_x']:7.1f} "
            f"{value['mean_book_names']:6.0f} {value['held_long_rank_pct']:7.2f} "
            f"{value['held_short_rank_pct']:7.2f}",
            flush=True,
        )
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
