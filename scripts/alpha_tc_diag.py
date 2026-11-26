
"""Grade how much of the signal's ranking survives into the book.

Three of the swept knobs flip sign between the halves, and the one test that
removes the market entirely reads a t statistic below one on both windows.
That is not what a genuine IC of 0.05 should look like. Grinold's fundamental
law says the information ratio of the book is ``TC * IC * sqrt(BR)``, so at an
IC of 0.05 on a three-thousand-name universe the breadth term alone supports an
information ratio well above one and a measured t near one says the transfer
coefficient is what is missing, not the signal.

This script measures that coefficient directly instead of sweeping another
parameter. It reuses the banded selection of ``spread_days`` so the book it
grades is the same book the previous notes graded, and it reports three things
that have never been separated before:

1. ``turnover`` -- the fraction of the book that changes at each rebalance.
   A book that turns its whole size over to capture an IC of 0.05 pays several
   times the ranking it earns.
2. ``decile`` -- the mean return of each of ten signal-ranked deciles. An IC is
   an average over the whole cross-section while the book only holds the tails,
   so the question is whether the ranking is concentrated there or spread thin.
3. ``vol`` -- the realised annualised volatility of the spread. The information
   ratio is the mean divided by that, so it has to be reported beside it.

Usage::

    python scripts/alpha_tc_diag.py [--signal <parquet>] [--json out.json]
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


SESSIONS_PER_YEAR: float = 244.0


def grade(sample: np.ndarray) -> tuple[float, float, float]:
    """Mean, naive two-tailed t and its normal approximation."""
    if len(sample) < 3:
        return float(sample.mean()) if len(sample) else 0.0, float("nan"), float("nan")
    mean = float(sample.mean())
    sd = float(sample.std(ddof=1))
    if sd == 0:
        return mean, 0.0, 1.0
    t = mean / sd * sqrt(len(sample))
    # The doubled-tail form written as (1 - erf(|t| / sqrt(2))) * 2 returns
    # p values above one on small t; the correct two-tailed value is the
    # single-tail mass doubled by erf directly, which is what this is.
    return mean, t, 1.0 - erf(abs(t) / sqrt(2))


def select_book(
    group: pl.DataFrame,
    held_long: set[str],
    held_short: set[str],
    long_rate: float,
    short_rate: float,
    buffer_ratio: float,
) -> tuple[set[str], set[str], int, int]:
    """The banded selection of ``spread_days``, with the turnover counted too."""
    n: int = group.height
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
    new_long = (held_long & set(universe[i] for i in long_band)) | set(
               universe[:long_end]
    )
    new_short = (held_short & set(universe[i] for i in short_band)) | set(
        universe[short_start:]
    )
    entered = len(new_long - held_long) + len(new_short - held_short)
    left = len((held_long - new_long) | (held_short - new_short))
    return new_long, new_short, entered, left


def depth_profile(group: pl.DataFrame) -> list[float]:
    """Mean return of each of ten signal-ranked deciles, best decile first."""
    universe = group.filter(pl.col("ret").is_not_null()).sort("signal", descending=True)
    n: int = universe.height
    if n < 100:
        return []
    size: int = n // 10
    returns = universe["ret"].to_numpy()
    return [
        float(np.mean(returns[i * size : (i + 1) * size])) for i in range(10)
    ]


def diagnose(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
    buffer_ratio: float,
    rebalance: int,
) -> dict:
    """Run the graded book over one window and reduce it to TC diagnostics."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {d: g.sort("signal", descending=True) for d, g in window.group_by("datetime")}
    dates = sorted(groups)
    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    spreads: list[float] = []
    turnover: list[float] = []
    sizes: list[int] = []
    depth_rows: list[list[float]] = []
    for date in dates:
        group = groups[date]
        if group.height < 300:
            continue
        since += 1
        if since >= rebalance:
            since = 0
            held_long, held_short, entered, left = select_book(
                group, held_long, held_short, long_rate, short_rate, buffer_ratio
            )
            book = (len(held_long) + len(held_short)) or 1
            turnover.append((entered + left) / 2.0 / book)
            sizes.append(book)
        if not held_long or not held_short:
            continue
        longs = group.filter(pl.col("vt_symbol").is_in(list(held_long)))
        shorts = group.filter(pl.col("vt_symbol").is_in(list(held_short)))
        long_rets = [x for x in longs["ret"].to_list() if x is not None]
        short_rets = [x for x in shorts["ret"].to_list() if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spreads.append(float(np.mean(long_rets)) - float(np.mean(short_rets)))
        depth = depth_profile(group)
        if depth:
            depth_rows.append(depth)
    sample = np.array(spreads)
    mean, t_naive, p_naive = grade(sample)
    _, t_nw, p_nw = newey_west_t(sample, 10)
    sd = float(sample.std(ddof=1)) if len(sample) > 1 else 0.0
    decile = np.array(depth_rows)
    return {
        "window": f"{start}..{end}",
        "config": f"L{int(long_rate * 100)}/S{int(short_rate * 100)}",
        "n_sessions": int(len(sample)),
        "mean_bp": mean * 10000,
        "t_naive": t_naive,
        "p_naive": p_naive,
        "t_nw": t_nw,
        "p_nw": p_nw,
        "annual_vol_pct": sd * sqrt(SESSIONS_PER_YEAR) * 100,
        "ir": mean / sd * sqrt(SESSIONS_PER_YEAR) if sd else 0.0,
        "mean_turnover_ratio": float(np.mean(turnover)) if turnover else 0.0,
        "rebalances": len(turnover),
        "mean_book_names": float(np.mean(sizes)) if sizes else 0.0,
        "annual_turnover_x": float(np.mean(turnover) * SESSIONS_PER_YEAR / rebalance)
        if turnover
        else 0.0,
        "decile_mean_pct": [float(v) * 100 for v in decile.mean(axis=0)]
        if len(decile)
        else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Transfer-coefficient diagnostics")
    parser.add_argument("--signal", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    print("daily:", daily)
    print("signal:", signal_path, flush=True)

    bars = load_returns(daily)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    print("join rows:", joined.height, flush=True)

    report: dict[str, dict] = {}
    for long_rate, short_rate in [(0.05, 0.05), (0.10, 0.10)]:
        for name, start, end in [
            ("2022-2024", "2022-01-20", "2024-12-31"),
            ("2025-2026", "2025-01-02", "2026-11-13"),
            ("full", "2022-01-20", "2026-11-13"),
        ]:
            key = f"L{int(long_rate * 100)}/S{int(short_rate * 100)} {name}"
            report[key] = diagnose(
                joined, start, end, long_rate, short_rate, 0.10, 30
            )
    print()
    header = f"{'config':22} {'sess':>5} {'mean bp':>9} {'t_naive':>8} {'t_NW':>7}"
    print(f"{header} {'p_NW':>7} {'vol%':>7} {'IR':>6} {'turn/x':>7} {'names':>6}")
    for key, value in report.items():
        print(
            f"{key:22} {value['n_sessions']:5d} {value['mean_bp']:9.2f} "
            f"{value['t_naive']:8.2f} {value['t_nw']:7.2f} {value['p_nw']:7.3f} "
            f"{value['annual_vol_pct']:7.2f} {value['ir']:6.2f} "
            f"{value['annual_turnover_x']:7.1f} {value['mean_book_names']:6.0f}",
            flush=True,
        )
    print()
    print("十档分组日均收益（%，最优在前）：排序集中在哪一段")
    for key, value in report.items():
        row = " ".join(f"{v:+7.3f}" for v in value["decile_mean_pct"])
        print(f"{key:22} {row}")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
