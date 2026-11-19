"""Grade the same signal on non-overlapping five-session blocks.

Daily spreads of an equal-weight basket are almost serially uncorrelated but
not exactly so, and a rebalance clock of thirty sessions ties neighbouring
sessions together. Summing five sessions into one block breaks that tie while
keeping every block disjoint, which is the more conservative unit of
observation for a t statistic.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

from alpha_ls_window import find_cache, load_returns, grade, spread_days


def block_spreads(
    joined: pl.DataFrame, start: str, end: str, long_rate: float, short_rate: float
) -> np.ndarray:
    """Daily long-minus-short spread, resampled into disjoint 5-day blocks."""
    daily = spread_days(joined, start, end, long_rate, short_rate, 0.10, 30)
    usable = len(daily) - len(daily) % 5
    if usable < 10:
        return np.array([])
    return daily[:usable].reshape(-1, 5).sum(axis=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Non-overlapping block test")
    parser.add_argument("--signal", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    bars = load_returns(daily)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")

    windows = [
        ("2022-2024", "2022-01-20", "2024-12-31"),
        ("2025-2026", "2025-01-02", "2026-11-13"),
        ("全窗口", "2022-01-20", "2026-11-13"),
    ]
    print("非重叠 5 日块之和（每块 5 个 session 的价差合计）")
    print(f"{'window':12} {'config':12} {'块数':>5} {'块均值%':>9} {'t':>7} {'p':>7}")
    report: dict[str, dict] = {}
    for name, start, end in windows:
        for long_rate, short_rate in [(0.05, 0.05), (0.05, 0.10), (0.10, 0.10)]:
            blocks = block_spreads(joined, start, end, long_rate, short_rate)
            mean, t, p = grade(blocks)
            print(
                f"{name} L{int(long_rate * 100)}/S{int(short_rate * 100)}    "
                f"{len(blocks):5d} {mean * 100:9.4f} {t:7.2f} {p:7.3f}",
                flush=True,
            )
            report[f"{name} L{int(long_rate * 100)}/S{int(short_rate * 100)}"] = {
                "blocks": int(len(blocks)),
                "block_mean_bp": mean * 10000,
                "t": t,
                "p": p,
            }
        print(flush=True)
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
