"""Split the new window's long-short spread into its two legs.

A long-short book can earn its spread from both legs agreeing, or from one leg
carrying it while the other cancels. On the 900-window signal the graded half's
profit turned out to be the short leg alone, so the split has to be shown on the
window the model never saw before that leg is read as anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

from alpha_ls_window import find_cache, load_returns


def leg_days(
    joined: pl.DataFrame,
    start: str,
    end: str,
    rate: float,
    buffer_ratio: float,
    rebalance: int,
    side: str,
) -> np.ndarray:
    """Daily return of one side of the banded, rebalanced selection."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    )
    groups = {
        d: g.sort("signal", descending=True) for d, g in window.group_by("datetime")
    }
    dates = sorted(groups)
    held: set[str] = set()
    since = rebalance
    out: list[float] = []
    for date in dates:
        group = groups[date]
        n = group.height
        if n < 300:
            continue
        since += 1
        if since >= rebalance:
            since = 0
            universe = group["vt_symbol"].to_list()
            edge = (
                max(int(n * rate), 1)
                if side == "long"
                else min(int(n * (1 - rate)), n - 1)
            )
            band = range(
                max(0, edge - int(n * buffer_ratio)),
                min(n, edge + int(n * buffer_ratio)),
            )
            names = universe[:edge] if side == "long" else universe[edge:]
            held = (held & set(universe[i] for i in band)) | set(names)
        if not held:
            continue
        legs = group.filter(pl.col("vt_symbol").is_in(list(held)))
        rets = [x for x in legs["ret"].to_list() if x is not None]
        if len(rets) < 5:
            continue
        out.append(float(np.mean(rets)))
    return np.array(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Leg split by session")
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
        ("2025-2026", "2025-01-02", "2026-11-13"),
    ]
    print("逐腿复利（非重叠 1 日收盘收益连乘），band 5% / buffer 0.10 / 30 日 clock")
    for name, start, end in windows:
        for rate in (0.05, 0.10):
            longs = leg_days(joined, start, end, rate, 0.10, 30, "long")
            shorts = leg_days(joined, start, end, rate, 0.10, 30, "short")
            spread = float(np.prod(1 + longs) / np.prod(1 + shorts) - 1) * 100
            l_growth = float(np.prod(1 + longs) - 1) * 100
            s_growth = float(np.prod(1 + shorts) - 1) * 100
            print(
                f"{name} top/bottom {int(rate * 100)}%: "
                f"多 {l_growth:+8.2f}%  空 {s_growth:+8.2f}%  多空 {spread:+8.2f}%  "
                f"n={len(longs)}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
