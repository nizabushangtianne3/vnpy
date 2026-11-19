"""Decide whether the spread is a book's return or the same-day return.

Every daily-spread number in the note selects at session d off a signal fitted
on bars through the close of d, and then earns ``close(d)/close(d-1) - 1``,
which is the return realised during that same session. A signal computed at
the close of d cannot earn session d's return: it can only act from d onward,
so the first return it is entitled to is session d+1's.

Daily rebalancing is the test that exposes it, because it removes the buffering
that otherwise dilutes the contamination. It reads -0.64 percent a day at t of
-9.96, while the same signal at a 30 session clock reads +0.085 percent at t of
1.42, so the two clocks disagree in sign and the disagreeing part is the part
that happens inside the decision session.

This module re-runs the same selection with an explicit lag between the signal
and the return it is credited with, so lag=0 is the contaminated reading and
lag=1 is the first honest one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

from alpha_ls_window import find_cache, load_returns
from alpha_ls_newey import newey_west_t, autocorr


def lagged_spread(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
    buffer_ratio: float,
    rebalance: int,
    lag: int,
) -> np.ndarray:
    """Daily long-minus-short spread where the return follows the signal by ``lag``."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    dates = sorted(window["datetime"].unique().to_list())
    by_date = {
        d: window.filter(pl.col("datetime") == d).sort("signal", descending=True)
        for d in dates
    }
    index = {d: i for i, d in enumerate(dates)}

    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    out: list[float] = []
    for date in dates:
        group = by_date[date]
        n = group.height
        if n < 300:
            continue
        since += 1
        if since >= rebalance:
            since = 0
            universe = group["vt_symbol"].to_list()
            long_end = max(int(n * long_rate), 1)
            short_start = min(int(n * (1 - short_rate)), n - 1)
            long_band = range(
                max(0, long_end - int(n * buffer_ratio)),
                min(n, long_end + int(n * buffer_ratio)),
            )
            short_band = range(
                max(0, short_start - int(n * buffer_ratio)),
                min(n, short_start + int(n * buffer_ratio)),
            )
            held_long = (held_long & set(universe[i] for i in long_band)) | set(
                universe[:long_end]
            )
            held_short = (held_short & set(universe[i] for i in short_band)) | set(
                universe[short_start:]
            )
        target = index[date] + lag
        if target >= len(dates):
            continue
        future = by_date[dates[target]]
        if not held_long or not held_short:
            continue
        longs = future.filter(pl.col("vt_symbol").is_in(list(held_long)))
        shorts = future.filter(pl.col("vt_symbol").is_in(list(held_short)))
        long_rets = [x for x in longs["ret"].to_list() if x is not None]
        short_rets = [x for x in shorts["ret"].to_list() if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        out.append(float(np.mean(long_rets)) - float(np.mean(short_rets)))
    return np.array(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Signal/return lag test")
    parser.add_argument("--signal", default="")
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(load_returns(daily), on=["datetime", "vt_symbol"], how="inner")

    windows = [
        ("2022-2024", "2022-01-20", "2024-12-31"),
        ("full", "2022-01-20", "2026-11-13"),
    ]
    for rebalance, label in [
        (30, "30 日 clock（note 里的口径）"),
        (1, "每日重选（机器）"),
    ]:
        print(f"=== {label} ===")
        print(
            f"{'window':12} {'lag':>4} {'n':>5} {'均值%':>9} {'t':>7} {'p':>7} {'rho1':>7}"
        )
        for name, start, end in windows:
            for lag in (0, 1, 2):
                sample = lagged_spread(
                    joined, start, end, 0.05, 0.05, 0.10, rebalance, lag
                )
                if len(sample) < 40:
                    continue
                mean, t, p = newey_west_t(sample, 10)
                print(
                    f"{name} top/bottom 5% {lag:4d} {len(sample):5d} {mean * 100:9.4f} "
                    f"{t:7.2f} {p:7.3f} {autocorr(sample, 1):7.3f}",
                    flush=True,
                )
        print(flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
