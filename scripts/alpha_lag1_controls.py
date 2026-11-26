
"""Verify the daily reading has no same-session look-ahead left in it.

The daily book on the lag-one return is the first strongly positive reading in
the repository, so the first thing to establish is that it is not the reverse of
the contamination that produced the negative readings before it. A signal
computed from session T's own prices must not be credited with session T+1's
close-to-close return if the ranking used to pick names was formed on data that
includes part of T+1.

Two controls settle it. One: the same book measured on the return of the session
of the decision, which must read strongly negative if there is no look-ahead,
because that is what a reversal looks like when the ranking is good. Two: the
same book measured on a lag of two and of five sessions, which must decay toward
zero if the edge is genuinely a one-session jump and not a persistent
mis-measurement.

This reports all of them on the same bars with the same band and the same cost,
so the shape of the decay is the evidence.

Usage::

    python scripts/alpha_lag1_controls.py [--json out.json]
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
COST: float = 0.0016

WINDOWS = [
    ("full", "2022-01-20", "2026-11-13"),
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
    ("2022-2024", "2022-01-20", "2024-12-31"),
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


def returns_at_lag(daily: Path, lag: int) -> pl.DataFrame:
    """Close-to-close return credited to the decision ``lag`` sessions later.

    ``lag`` of 0 credits the decision session's own return, which is the
    contaminated reading and is kept as the control.
    """
    rows: list[pl.DataFrame] = []
    for path in sorted(daily.glob("*.parquet")):
        frame = pl.read_parquet(path, columns=["datetime", "close"]).sort("datetime")
        if frame.height < 8:
            continue
        frame = frame.with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"),
            pl.lit(path.stem).alias("vt_symbol"),
        )
        frame = frame.with_columns(pl.col("ret").shift(-lag).alias("fwd"))
        rows.append(frame.select("datetime", "vt_symbol", "fwd"))
    return pl.concat(rows, how="vertical")


def spread_series(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
) -> dict:
    """Median spread of a book rebuilt every session, with its turnover share."""
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
    parser = argparse.ArgumentParser(description="Lag-one controls")
    parser.add_argument("--signal", default="")
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
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")

    report: dict[str, dict] = {}
    print()
    print("同一每日重选簿，收益按滞后 session 数归属（band L5/S5，成本 16bp × 当日变动）")
    print(f"{'window':10} {'lag 0':>18} {'lag 1':>18} {'lag 2':>18} {'lag 5':>18}")
    for name, start, end in WINDOWS:
        row = f"{name:10}"
        report[name] = {}
        for lag in [0, 1, 2, 5]:
            bars = returns_at_lag(daily, lag)
            joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
            outcome = spread_series(
                joined, start, end, args.long_rate, args.short_rate
            )
            sample = outcome["spreads"]
            share = outcome["turnover_share"]
            gross = sample.mean() * 10000
            _, g_t_nw, _ = newey_west_t(sample, 10)
            netted = sample - COST * share
            n_mean = float(netted.mean()) * 10000
            _, n_t_nw, _ = newey_west_t(netted, 10)
            row += f" {gross:>7.1f}/{n_mean:>6.1f}/{n_t_nw:<4.1f}"
            report[name][f"lag {lag}"] = {
                "gross_mean_bp": float(gross),
                "gross_t_nw": float(g_t_nw),
                "net_mean_bp": float(n_mean),
                "net_t_nw": float(n_t_nw),
                "turnover_share": float(share),
                "n": int(len(sample)),
            }
        print(row, flush=True)
    print()
    print("每格为 毛bp/净bp/净tNW。lag 0 是决策当日收益，即污染读数")
    print("若 lag 0 显著为负、lag 1 显著为正、lag 2 与 lag 5 衰减，则读数干净")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
