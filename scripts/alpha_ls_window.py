"""Test the long-short book on the window the train window never covered.

The rolling900 fit could only score from 2024-09-26 because its window needs
900 sessions of history before the first block. A train window of 250 starts
scoring at 2022-01-20, which puts 2022-2024 inside a signal the model was never
fitted on, so the same long-short test that came out indistinguishable from
zero can be run on data the model has not seen.

The test stays the one that cannot inherit a market direction: long the top
slice of the signal, short the bottom slice, and grade the daily spread of the
two equal-weight baskets. A single session contributes one return, never five.
"""

from __future__ import annotations

import argparse
import json
import sys
from math import erf, sqrt
from pathlib import Path

import numpy as np
import polars as pl


def find_cache() -> tuple[Path, Path]:
    """Locate the lab cache by looking, rather than by spelling a path out."""
    daily: Path | None = None
    sweeps: Path | None = None
    for candidate in Path(r"D:\Codex\work").glob("vnpy-*"):
        bars = candidate / "alpha_lab" / "daily"
        if bars.is_dir() and len(list(bars.glob("*.parquet"))) > 1000:
            daily = bars
        signals = candidate / "sweeps"
        if (
            signals.is_dir()
            and (signals / "market3000_size_rolling250_250_h5_ens.parquet").exists()
        ):
            sweeps = signals
    if daily is None or sweeps is None:
        raise SystemExit("cache not found under D:\\Codex\\work")
    return daily, sweeps


def load_returns(daily: Path) -> pl.DataFrame:
    """One non-overlapping close-to-close return per symbol per session."""
    rows: list[pl.DataFrame] = []
    for path in sorted(daily.glob("*.parquet")):
        frame = pl.read_parquet(path, columns=["datetime", "close"]).sort("datetime")
        if frame.height < 3:
            continue
        frame = frame.with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"),
            pl.lit(path.stem).alias("vt_symbol"),
        )
        rows.append(frame.select("datetime", "vt_symbol", "ret"))
    return pl.concat(rows, how="vertical")


def spread_days(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
    buffer_ratio: float,
    rebalance: int,
) -> np.ndarray:
    """Daily long-minus-short spread under a banded, rebalanced selection."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    )
    groups = {
        d: g.sort("signal", descending=True) for d, g in window.group_by("datetime")
    }
    dates = sorted(groups)
    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    spreads: list[float] = []
    for date in dates:
        group = groups[date]
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
        if not held_long or not held_short:
            continue
        longs = group.filter(pl.col("vt_symbol").is_in(list(held_long)))
        shorts = group.filter(pl.col("vt_symbol").is_in(list(held_short)))
        long_rets = [x for x in longs["ret"].to_list() if x is not None]
        short_rets = [x for x in shorts["ret"].to_list() if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spreads.append(float(np.mean(long_rets)) - float(np.mean(short_rets)))
    return np.array(spreads)


def grade(sample: np.ndarray) -> tuple[float, float, float]:
    """Two-tailed t statistic and its normal approximation."""
    n = len(sample)
    if n < 3:
        return float(sample.mean()), float("nan"), float("nan")
    mean = float(sample.mean())
    sd = float(sample.std(ddof=1))
    if sd == 0:
        return mean, 0.0, 1.0
    t = mean / sd * sqrt(n)
    # Two-tailed normal approximation: the tail mass on one side is
    # (1 - erf(|t| / sqrt(2))) / 2, so doubling it gives back exactly
    # 1 - erf(...). Writing it the doubled way is what produced p values
    # above one on small t.
    return mean, t, 1.0 - erf(abs(t) / sqrt(2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Long-short test on the 250 window")
    parser.add_argument("--signal", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / ("market3000_size_rolling250_250_h5_ens.parquet")
    )
    print("daily:", daily)
    print("signal:", signal_path, flush=True)

    bars = load_returns(daily)
    print("bars:", bars.height, bars["datetime"].n_unique(), "sessions")
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    print("join:", joined.height, flush=True)

    print("稳健性：剔除最大正/负日后的逐日扩散")
    for name, start, end in [
        ("2022-2024", "2022-01-20", "2024-12-31"),
        ("2025-2026", "2025-01-02", "2026-11-13"),
    ]:
        for long_rate, short_rate in [(0.05, 0.05), (0.10, 0.10)]:
            sample = spread_days(joined, start, end, long_rate, short_rate, 0.10, 30)
            if len(sample) < 30:
                continue
            order = np.sort(sample)
            trimmed = order[5:-5]
            mean, t, p = grade(trimmed)
            biggest = order[-5:]
            smallest = order[:5]
            share = float(np.abs(biggest).sum() / np.abs(sample).sum())
            print(
                f"{name} L{int(long_rate * 100)}/S{int(short_rate * 100)}    "
                f"n={len(trimmed):4d} 去极值日均% {mean * 100:8.4f} t={t:6.2f} p={p:6.3f} | "
                f"最大5日占|总| {share * 100:5.1f}%  最大 {biggest[-1] * 100:+.3f}%  最小 {smallest[0] * 100:+.3f}%",
                flush=True,
            )
    print(flush=True)

    windows = [
        ("2022 全年      ", "2022-01-20", "2022-12-30"),
        ("2023 全年      ", "2023-01-01", "2023-12-29"),
        ("2024 全年      ", "2024-01-02", "2024-12-31"),
        ("2022-2024 三年 ", "2022-01-20", "2024-12-31"),
        ("2025 全年      ", "2025-01-02", "2025-12-31"),
        ("2026 至今      ", "2026-01-05", "2026-11-13"),
        ("全窗口        ", "2022-01-20", "2026-11-13"),
    ]
    configs = [(0.05, 0.05), (0.05, 0.10), (0.10, 0.10)]
    print()
    print("多空日收益（非重叠 1 日收盘收益，T 决策 / T+1 生效）")
    print(f"{'window':15} {'config':12} {'n':>5} {'日mean%':>9} {'t':>7} {'p':>7}")
    report: dict[str, dict] = {}
    for name, start, end in windows:
        for long_rate, short_rate in configs:
            sample = spread_days(joined, start, end, long_rate, short_rate, 0.10, 30)
            mean, t, p = grade(sample)
            print(
                f"{name} L{int(long_rate * 100)}/S{int(short_rate * 100)}    "
                f"{len(sample):5d} {mean * 100:9.4f} {t:7.2f} {p:7.3f}",
                flush=True,
            )
            report[
                f"{name.strip()} L{int(long_rate * 100)}/S{int(short_rate * 100)}"
            ] = {
                "n": int(len(sample)),
                "mean_bp": mean * 10000,
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
