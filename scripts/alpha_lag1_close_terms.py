
"""Grade the daily book with the overnight exposures the offline ruler skipped.

The offline daily book credits the long leg with a single return per session and
charges one round trip, which treats a name bought and sold on consecutive
days as two independent round trips. It is not. A book that rebalances daily
carries, between the session it sells a name and the session it bought it, the
same overnight gap the long leg is being credited with, and the sell-side price
is the next open rather than the close the offline ruler used.

The gap is therefore charged twice rather than once on every daily turnover:
once as the return of the name being sold, and once as the return of the name
being bought, and the offline ruler only carried the second. This measures the
book on a session's own close-to-close return instead of an open-to-open span,
which is the reading a daily-rebalance book actually earns once its sells and
its buys have both paid the gap.

Usage::

    python scripts/alpha_lag1_close_terms.py [--json out.json]
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

WINDOWS = [
    ("full", "2022-01-20", "2026-11-13"),
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
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


def shifted(daily: Path, lag: int) -> pl.DataFrame:
    """Close-to-close return credited ``lag`` sessions after the decision."""
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
    """Median spread of a daily book plus the turnover share it pays."""
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
        values = group["fwd"].to_list()
        long_rets = [x for x in values[:long_end] if x is not None]
        short_rets = [x for x in values[short_start:] if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spreads.append(float(np.median(long_rets)) - float(np.median(short_rets)))
        shares.append((entered + left) / 2.0 / book)
    return {
        "spreads": np.array(spreads),
        "turnover_share": float(np.mean(shares)) if shares else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Close-to-close daily book")
    parser.add_argument("--signal", default="")
    parser.add_argument("--long-rate", type=float, default=0.01)
    parser.add_argument("--short-rate", type=float, default=0.01)
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

    COSTS = [0.0, 0.00076, 0.0016]
    print()
    print("收盘价口径的每日重选簿（band 1%，成本按全额往返计）")
    print(
        f"{'window':10} {'lag':>4} {'turn/日':>8} "
        + "".join(f"{c * 10000:>16.1f}bp" for c in COSTS)
    )
    report: dict[str, dict] = {}
    for name, start, end in WINDOWS:
        for lag in [0, 1, 2]:
            bars = shifted(daily, lag)
            joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
            outcome = spread_series(
                joined, start, end, args.long_rate, args.short_rate
            )
            sample = outcome["spreads"]
            share = outcome["turnover_share"]
            gross = float(sample.mean()) * 10000
            _, g_t_nw, _ = newey_west_t(sample, 10)
            row = f"{name:10} {lag:4d} {share * 100:7.2f}% g {gross:7.1f}/{g_t_nw:<4.1f}"
            report[f"{name} lag {lag}"] = {
                "gross_mean_bp": gross,
                "gross_t_nw": float(g_t_nw),
                "turnover_share": float(share),
            }
            for cost in COSTS:
                netted = sample - cost
                n_mean = float(netted.mean()) * 10000
                _, n_t_nw, _ = newey_west_t(netted, 10)
                total = float(np.prod(1.0 + netted) - 1.0) * 100
                row += f" {n_mean:7.1f}/{n_t_nw:<4.1f}"
                report[f"{name} lag {lag}"][f"{cost * 10000:.1f}bp"] = {
                    "net_mean_bp": n_mean,
                    "net_t_nw": float(n_t_nw),
                    "net_total_pct": total,
                }
            print(row, flush=True)
    print()
    print("每格为 净bp/净tNW。0.0bp 毛、7.6bp 真实 A 股往返、16bp 仓库旧假设")
    print("这里成本按全额往返计（每日重建），不是按变动比例")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
