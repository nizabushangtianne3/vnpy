"""Grade the daily long-short spread with autocorrelation accounted for.

The t statistic in the note treats the 714 (or 1164) daily spreads as 714
independent draws. They are not: the selection rebalances on a 30 session clock,
so a name stays in the book for up to five sessions and its return reappears in
five consecutive spreads. A standard error that ignores that is too narrow, and
a t statistic built on it is too generous.

Newey-West widens the variance estimate by the sample autocovariances up to a
chosen lag, which is the standard correction for a time series whose
observations overlap. This module reports both, because the honest statement of
"is this distinguishable from zero" needs the conservative one, and the gap
between them is the size of the effect the clock creates.
"""

from __future__ import annotations

import argparse
import sys
from math import erf, sqrt
from pathlib import Path

import numpy as np
import polars as pl

from alpha_ls_window import find_cache, load_returns, spread_days


def newey_west_t(sample: np.ndarray, lag: int) -> tuple[float, float, float]:
    """Mean, t and two-tailed p under a Newey-West standard error."""
    n = len(sample)
    if n < lag + 2:
        lag = max(n - 2, 1)
    centered = sample - sample.mean()
    gamma = np.array(
        [float(np.dot(centered[i:], centered[: n - i])) / n for i in range(lag + 1)]
    )
    weights = 1.0 - np.arange(lag + 1) / (lag + 1.0)
    long_run_var = float(np.sum(weights * gamma))
    mean = float(sample.mean())
    if long_run_var <= 0:
        return mean, 0.0, 1.0
    se = sqrt(long_run_var / n)
    t = mean / se
    return mean, t, 1.0 - erf(abs(t) / sqrt(2))


def autocorr(sample: np.ndarray, lag: int) -> float:
    n = len(sample)
    if n < lag + 2:
        return float("nan")
    centered = sample - sample.mean()
    denom = float(np.dot(centered, centered)) / n
    if denom == 0:
        return float("nan")
    return float(np.dot(centered[lag:], centered[: n - lag]) / n / denom)


def main() -> int:
    parser = argparse.ArgumentParser(description="Newey-West grade of the spread")
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
        ("full", "2022-01-20", "2026-11-13"),
    ]
    print("逐日多空价差：朴素 t vs Newey-West 修正 t")
    print(
        f"{'window':12} {'band':10} {'n':>5} {'均值%':>8} {'t_naive':>8} {'t_NW':>7} {'p_NW':>7} {'rho1':>7} {'rho5':>7} {'rho10':>7}"
    )
    for name, start, end in windows:
        for long_rate, short_rate in [(0.05, 0.05), (0.05, 0.10), (0.10, 0.10)]:
            sample = spread_days(joined, start, end, long_rate, short_rate, 0.10, 30)
            if len(sample) < 40:
                continue
            naive = float(sample.mean() / sample.std(ddof=1) * sqrt(len(sample)))
            _, t_nw, p_nw = newey_west_t(sample, 10)
            r1 = autocorr(sample, 1)
            r5 = autocorr(sample, 5)
            r10 = autocorr(sample, 10)
            print(
                f"{name} L{int(long_rate * 100)}/S{int(short_rate * 100)}    "
                f"{len(sample):5d} {sample.mean() * 100:8.4f} {naive:8.2f} {t_nw:7.2f} "
                f"{p_nw:7.3f} {r1:7.3f} {r5:7.3f} {r10:7.3f}",
                flush=True,
            )
    print(flush=True)
    print("把 rebalance clock 降到 1 日（每日重选，自相关应趋零）作对照：")
    print(
        f"{'window':12} {'band':10} {'n':>5} {'均值%':>8} {'t_naive':>8} {'t_NW':>7} {'rho1':>7} {'rho5':>7} {'rho10':>7}"
    )
    for name, start, end in windows:
        for long_rate, short_rate in [(0.05, 0.05), (0.10, 0.10)]:
            sample = spread_days(joined, start, end, long_rate, short_rate, 0.10, 1)
            if len(sample) < 40:
                continue
            naive = float(sample.mean() / sample.std(ddof=1) * sqrt(len(sample)))
            _, t_nw, p_nw = newey_west_t(sample, 10)
            r1 = autocorr(sample, 1)
            r5 = autocorr(sample, 5)
            r10 = autocorr(sample, 10)
            print(
                f"{name} L{int(long_rate * 100)}/S{int(short_rate * 100)}    "
                f"{len(sample):5d} {sample.mean() * 100:8.4f} {naive:8.2f} {t_nw:7.2f} "
                f"{p_nw:7.3f} {r1:7.3f} {r5:7.3f} {r10:7.3f}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
