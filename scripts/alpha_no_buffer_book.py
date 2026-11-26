
"""Grade the book that holds exactly the band, with no retention at all.

The buffer scan reported the retained long book sitting at the fortieth
percentile of the cross-section when it was asked to hold the top five
percent. That reading reproduces exactly under an independent implementation of
the same selection, so it is the behaviour of the retention rule rather than an
error in the diagnostic: a name that leaves the top five percent is kept as long
as it is still inside the buffer, and over a five-year window the names it keeps
drift far enough down the ranking that the book it is holding is a middle-percentile
book.

That is the whole explanation of the gap between the decile medians and the book.
The ten decile medians separate the top five percent from the bottom five by 54
basis points a session on this signal, while the banded book earns 2.33 basis
points. A book that actually holds the top five percent has to earn close to the
first figure.

This grades the book that holds exactly the band and nothing else: every
rebalance rebuilds both legs from the current ranking, so a name that falls out
of the band is sold the same session. The turnover is therefore the maximum the
band can generate, and the net reading is charged for all of it.

Usage::

    python scripts/alpha_no_buffer_book.py [--json out.json]
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
    rebalance: int,
) -> dict:
    """Median-member spread of a book rebuilt from the band at each rebalance."""
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
    ranks_long: list[float] = []
    ranks_short: list[float] = []
    for date in dates:
        group = groups[date].filter(pl.col("ret").is_not_null())
        n = group.height
        if n < 300:
            continue
        universe = group["vt_symbol"].to_list()
        rank = {sym: i for i, sym in enumerate(universe)}
        long_end = max(int(n * long_rate), 1)
        short_start = n - max(int(n * short_rate), 1)
        since += 1
        cost = 0.0
        if since >= rebalance:
            since = 0
            new_long = set(universe[:long_end])
            new_short = set(universe[short_start:])
            entered = len(new_long - held_long) + len(new_short - held_short)
            left = len((held_long - new_long) | (held_short - new_short))
            book = (len(new_long) + len(new_short)) or 1
            turnover.append((entered + left) / 2.0 / book)
            cost = (entered * BUY_RATE + left * SELL_RATE) / book
            held_long, held_short = new_long, new_short
        if not held_long or not held_short:
            continue
        present_long = [rank[sym] for sym in held_long if sym in rank]
        present_short = [rank[sym] for sym in held_short if sym in rank]
        if present_long:
            ranks_long.append(float(np.mean(present_long)) / n * 100)
        if present_short:
            ranks_short.append(float(np.mean(present_short)) / n * 100)
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
    sd_n = float(net.std(ddof=1))
    sd_g = float(stocks.std(ddof=1))
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
        "annual_vol_pct": sd_g * sqrt(SESSIONS_PER_YEAR) * 100,
        "net_ir": n_mean / (sd_n * sqrt(SESSIONS_PER_YEAR)) if sd_n else 0.0,
        "cost_bp": float(np.sum(costs)) * 10000,
        "annual_turnover_x": float(np.mean(turnover) * SESSIONS_PER_YEAR / rebalance)
        if turnover
        else 0.0,
        "held_long_rank_pct": float(np.mean(ranks_long)) if ranks_long else 0.0,
        "held_short_rank_pct": float(np.mean(ranks_short)) if ranks_short else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="No-buffer band book")
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
    for label, long_rate, short_rate in [
        ("L5/S5", 0.05, 0.05),
        ("L10/S10", 0.10, 0.10),
        ("L10/S5", 0.10, 0.05),
        ("L5/S10", 0.05, 0.10),
        ("L20/S20", 0.20, 0.20),
    ]:
        for name, start, end in WINDOWS:
            key = f"{label} {name}"
            report[key] = run(joined, start, end, long_rate, short_rate, args.rebalance)
    print()
    print(f"严格 band 簿（无保留带，clock {args.rebalance}，买3bp/卖13bp）")
    print(
        f"{'config':18} {'n':>5} {'gross bp':>9} {'g_tNW':>7} {'pNW':>7} {'net bp':>8} "
        f"{'n_tNW':>7} {'n_pNW':>7} {'net%':>8} {'maxDD%':>8} {'vol%':>6} {'netIR':>6} "
        f"{'turn/x':>7} {'Lrank%':>7} {'Srank%':>7}"
    )
    for key, value in report.items():
        print(
            f"{key:18} {value['n']:5d} {value['gross_mean_bp']:9.2f} "
            f"{value['gross_t_nw']:7.2f} {value['gross_p_nw']:7.3f} "
            f"{value['net_mean_bp']:8.2f} {value['net_t_nw']:7.2f} "
            f"{value['net_p_nw']:7.3f} {value['net_total_pct']:8.2f} "
            f"{value['max_dd_pct']:8.2f} {value['annual_vol_pct']:6.2f} "
            f"{value['net_ir']:6.2f} {value['annual_turnover_x']:7.1f} "
            f"{value['held_long_rank_pct']:7.2f} {value['held_short_rank_pct']:7.2f}",
            flush=True,
        )
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
