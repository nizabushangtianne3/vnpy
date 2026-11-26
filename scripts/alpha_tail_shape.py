"""Grade the top decile apart from the fat tail inside it.

The ten-decile reading shows the last decile returning five times the ninth,
which reads like the whole signal lives in the tail. It does not: grading the
extreme slices on their own leaves the 2022-2024 gross at 1.5 basis points a
session with a Newey-West t of 0.34, so the extreme slice is not a better book
than the band. The remaining explanation for a decile that returns five times
its neighbour is that its mean is carried by a handful of extreme names rather
than being an average the whole decile shares.

This script separates the two. For each decile it reports the mean, the median,
the trimmed mean, and the share of the decile's total return that its top five
percent contribute. A decile whose mean and median agree is a decile the
ranking genuinely orders. A decile whose mean is several times its median is a
decile whose reading is a tail, and taking that tail is a different and much
riskier book than the one the signal grades.

Usage::

    python scripts/alpha_tail_shape.py [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

from alpha_ls_window import find_cache, load_returns

WINDOWS = [
    ("2022-2024", "2022-01-20", "2024-12-31"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
    ("full", "2022-01-20", "2026-11-13"),
]


def decile_stats(group: pl.DataFrame) -> list[dict]:
    """Per-decile mean, median, trimmed mean and top-share of return."""
    universe = group.filter(pl.col("ret").is_not_null()).sort("signal", descending=True)
    n: int = universe.height
    if n < 100:
        return []
    size: int = n // 10
    rows: list[dict] = []
    for i in range(10):
        chunk = universe.slice(i * size, size)
        returns = chunk["ret"].to_numpy()
        if len(returns) < 20:
            continue
        ordered = np.sort(returns)
        trimmed = ordered[: int(len(ordered) * 0.9)]
        total = float(returns.sum())
        top = float(ordered[-max(1, int(len(ordered) * 0.05)) :].sum())
        rows.append(
            {
                "decile": i + 1,
                "n": int(len(returns)),
                "mean_pct": float(returns.mean()) * 100,
                "median_pct": float(np.median(returns)) * 100,
                # A trimmed mean that sits near the median means the decile's
                # reading is an average its members share; a trimmed mean far
                # above the median means the mean is a tail.
                "trim90_mean_pct": float(trimmed.mean()) * 100,
                "top5_share": float(top / total) if total else float("nan"),
                "p99_pct": float(np.quantile(returns, 0.99)) * 100,
                "p01_pct": float(np.quantile(returns, 0.01)) * 100,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Decile tail shape")
    parser.add_argument("--signal", default="")
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

    report: dict[str, dict] = {}
    for name, start, end in WINDOWS:
        window = joined.filter(
            (pl.col("datetime") >= pl.lit(start).str.to_datetime())
            & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
        )
        groups = list(window.group_by("datetime"))
        collected: list[list[dict]] = []
        for _, group in groups:
            rows = decile_stats(group)
            if rows:
                collected.append(rows)
        deciles: list[dict] = []
        for i in range(10):
            values = [r[i] for r in collected if len(r) == 10]
            if not values:
                continue
            deciles.append(
                {
                    "decile": i + 1,
                    "sessions": len(values),
                    "mean_pct": float(np.mean([v["mean_pct"] for v in values])),
                    "median_pct": float(np.mean([v["median_pct"] for v in values])),
                    "trim90_mean_pct": float(
                        np.mean([v["trim90_mean_pct"] for v in values])
                    ),
                    "top5_share": float(np.mean([v["top5_share"] for v in values])),
                    "p99_pct": float(np.mean([v["p99_pct"] for v in values])),
                    "p01_pct": float(np.mean([v["p01_pct"] for v in values])),
                }
            )
        report[name] = {"deciles": deciles}
        print()
        print(f"=== {name} ===")
        print(
            f"{'decile':>7} {'sess':>5} {'mean%':>8} {'median%':>8} {'trim90%':>8} "
            f"{'top5share':>10} {'p99%':>8} {'p01%':>8}"
        )
        for row in deciles:
            print(
                f"{row['decile']:7d} {row['sessions']:5d} {row['mean_pct']:8.3f} "
                f"{row['median_pct']:8.3f} {row['trim90_mean_pct']:8.3f} "
                f"{row['top5_share']:10.3f} {row['p99_pct']:8.3f} {row['p01_pct']:8.3f}",
                flush=True,
            )
    print()
    print("mean/median 接近 = 该档读数由组内平均支撑；mean 远高于 median = 读数由尾部贡献")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
