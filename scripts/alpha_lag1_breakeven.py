
"""Find the round-trip cost a daily-rebalance book can pay and stay positive.

The lag-one daily-reselection book is the first strongly positive reading in the
repository: 28 basis points a session gross with a Newey-West t of 5.74 on the
full window, 22.62 with a t of 2.37 on the hold half, and every calendar year
positive. It was charged a full round trip every session, which is the worst
case, and it still netted +12.11 basis points.

A full round trip every session is not what the book actually pays, though. The
tails of a cross-sectional ranking are persistent: a name in the top five percent
of one session is usually still in the top five percent the next, so the names
that change are a fraction of the book rather than all of it. This measures that
fraction and grades the book across the cost per round trip from zero to forty
basis points, so the break-even is a measurement rather than a projection and
the turnover the book really generates is stated instead of assumed.

Usage::

    python scripts/alpha_lag1_breakeven.py [--json out.json]
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
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
    ("full", "2022-01-20", "2026-11-13"),
    ("2022-2024", "2022-01-20", "2024-12-31"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
]

YEARS = [
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026", "2026-01-01", "2026-11-13"),
]

COSTS = [0.0, 0.0001, 0.0002, 0.0005, 0.0016, 0.0020, 0.0030, 0.0040]


def grade(sample: np.ndarray) -> tuple[float, float, float]:
    """Mean, naive two-tailed t, and the normal approximation of its p."""
    if len(sample) < 3:
        return float(sample.mean()) if len(sample) else 0.0, float("nan"), float("nan")
    mean = float(sample.mean())
    sd = float(sample.std(ddof=1))
    if sd == 0:
        return mean, 0.0, 1.0
    t = mean / sd * sqrt(len(sample))
    return mean, t, 1.0 - erf(abs(t) / sqrt(2))


def shifted_returns(daily: Path, lag: int) -> pl.DataFrame:
    """Close-to-close return of the session ``lag`` sessions after the decision."""
    rows: list[pl.DataFrame] = []
    for path in sorted(daily.glob("*.parquet")):
        frame = pl.read_parquet(path, columns=["datetime", "close"]).sort("datetime")
        if frame.height < lag + 3:
            continue
        frame = frame.with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"),
            pl.lit(path.stem).alias("vt_symbol"),
        )
        # The decision session is the one the signal was computed on, so the
        # return it can earn is the following session's close-to-close move.
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
    """Gross spread, turnover share and cost per session of the daily book."""
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
        # The share of the book that changed, which is the fraction of a round
        # trip the session actually pays.
        shares.append((entered + left) / 2.0 / book)
    return {
        "spreads": np.array(spreads),
        "turnover_share": float(np.mean(shares)) if shares else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Lag-one break-even scan")
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
    bars = shifted_returns(daily, 1)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    print(
        f"band L{int(args.long_rate * 100)}/S{int(args.short_rate * 100)}  "
        f"join rows: {joined.height}",
        flush=True,
    )

    names = [w[0] for w in WINDOWS] + [y[0] for y in YEARS]
    series: dict[str, dict] = {}
    for name in names:
        for window_name, start, end in WINDOWS + YEARS:
            if window_name == name:
                series[name] = spread_series(
                    joined, start, end, args.long_rate, args.short_rate
                )

    turnover = series["full"]["turnover_share"]
    print()
    print(f"全窗口 Thornton 换手比例：每日 {turnover * 100:.2f}% 的簿子变动")
    print(f"年化换手 = {turnover * 244:.0f}x 名义本金")
    print()
    print("净收益 bp/日 与净 Newey-West t（ هزینه = 单笔往返费率 × 当日变动比例）")
    header = f"{'window':10}" + "".join(f"{c * 10000:>13.1f}bp" for c in COSTS)
    print(header)
    report: dict[str, dict] = {}
    for name in names:
        sample = series[name]["spreads"]
        share = series[name]["turnover_share"]
        row = f"{name:10}"
        report[name] = {"turnover_share": share}
        for cost in COSTS:
            netted = sample - cost * share
            mean, t, p = grade(netted)
            _, t_nw, p_nw = newey_west_t(netted, 10)
            row += f"{mean * 10000:>7.1f}/{t_nw:<5.1f}"
            report[name][f"{cost * 10000:.1f}bp"] = {
                "net_mean_bp": float((sample - cost * share).mean()) * 10000,
                "net_total_pct": float(np.prod(1.0 + netted) - 1.0) * 100,
                "net_t_nw": float(t_nw),
            }
        print(row, flush=True)
    print()
    print("读法：'净bp/t'。0.0bp 即毛价差，1.6bp 即仓库长期 A 股假设（买3卖13 全额时为 16bp）")
    gross = series["full"]["spreads"].mean() * 10000
    print(f"全窗口毛价差 {gross:.2f} bp/日，年化毛 {gross * 244:.0f} bp")
    break_even = gross / turnover if turnover else 0.0
    print(f"盈亏平衡的往返费率 = {break_even * 10000:.1f} bp（毛价差 / 当日变动比例）")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
