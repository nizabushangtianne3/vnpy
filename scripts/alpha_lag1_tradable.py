
"""Grade the daily book on a universe that clears liquidity and listing rules.

A one percent band on a three-thousand-name universe is roughly thirty names,
and on a bar file that still carries enterprises near delisting and names that
have stopped trading, a thirty-name tail can be dominated by stocks no account
can actually buy. The reading so far is a statement about the bars, and the
question this answers is whether it survives being restricted to names a real
account could trade: listed for at least a year, with positive turnover on the
decision session, and priced above one yuan so a short leg is not blocked by
the minimum lot.

The filter is applied to the cross-section before it is ranked, so it changes
which names are eligible rather than which names are held, and the same band,
the same execution prices and the same cost grade the result. If the edge
survives, it is an edge on tradable names. If it collapses, it was a statement
about the untradable tail.

Usage::

    python scripts/alpha_lag1_tradable.py [--json out.json]
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

COST: float = 0.0016
MIN_PRICE: float = 1.0
MIN_TURNOVER: float = 1.0e7
MIN_LISTED_SESSIONS: int = 244

WINDOWS = [
    ("full", "2022-01-20", "2026-11-13"),
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
]

YEARS = [
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026", "2026-01-01", "2026-11-13"),
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


def tradable_bars(daily: Path) -> pl.DataFrame:
    """Bars restricted to names an account could actually take a position in."""
    rows: list[pl.DataFrame] = []
    for path in sorted(daily.glob("*.parquet")):
        frame = pl.read_parquet(
            path, columns=["datetime", "open", "close", "volume", "turnover"]
        ).sort("datetime")
        if frame.height < MIN_LISTED_SESSIONS + 8:
            continue
        # Session index within this symbol's own history, so the listing test
        # counts the sessions a name has actually traded rather than calendar
        # days since a date that is not in the file.
        frame = frame.with_columns(
            pl.int_range(pl.len()).over("vt_symbol").alias("session_index")
            if "vt_symbol" in frame.columns
            else pl.int_range(pl.len()).alias("session_index"),
            pl.lit(path.stem).alias("vt_symbol"),
        )
        # Entry is the open of the session after the decision, exit one later.
        entry = pl.col("open").shift(-1)
        exit_ = pl.col("open").shift(-2)
        frame = frame.with_columns(
            (pl.col("open").shift(-1) * 0.0 + pl.col("open")).alias("_scratch"),
            (exit_ / entry - 1.0).alias("fwd"),
        )
        eligible = (
            (pl.col("session_index") >= MIN_LISTED_SESSIONS)
            & (pl.col("close") >= MIN_PRICE)
            & (pl.col("turnover") >= MIN_TURNOVER)
            & (pl.col("volume") > 0)
            & pl.col("fwd").is_not_null()
        )
        rows.append(
            frame.filter(eligible).select("datetime", "vt_symbol", "fwd")
        )
    return pl.concat(rows, how="vertical")


def spread_series(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
) -> dict:
    """Median spread of the daily book plus the turnover share it pays."""
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
    sizes: list[int] = []
    held_long: set[str] = set()
    held_short: set[str] = set()
    for date in sorted(groups):
        group = groups[date]
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
        sizes.append(book)
    return {
        "spreads": np.array(spreads),
        "turnover_share": float(np.mean(shares)) if shares else 0.0,
        "mean_names": float(np.mean(sizes)) if sizes else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily book on tradable names")
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
    bars = tradable_bars(daily)
    print("tradable bars:", bars.height, flush=True)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    print(
        f"band L{int(args.long_rate * 100)}/S{int(args.short_rate * 100)}  "
        f"join rows: {joined.height}",
        flush=True,
    )

    report: dict[str, dict] = {}
    print()
    print("可交易宇宙（上市满一年、收盘价>=1元、成交额>=1000万、有量）")
    print(
        f"{'window':10} {'gross bp':>10} {'t_NW':>7} {'net bp':>9} {'net tNW':>8} "
        f"{'net%':>9} {'turn/日':>8} {'names':>7}"
    )
    for name, start, end in WINDOWS + YEARS:
        outcome = spread_series(
            joined, start, end, args.long_rate, args.short_rate
        )
        sample = outcome["spreads"]
        share = outcome["turnover_share"]
        gross = float(sample.mean()) * 10000
        _, g_t_nw, _ = newey_west_t(sample, 10)
        netted = sample - COST * share
        n_mean = float(netted.mean()) * 10000
        _, n_t_nw, _ = newey_west_t(netted, 10)
        total = float(np.prod(1.0 + netted) - 1.0) * 100
        print(
            f"{name:10} {gross:10.2f} {g_t_nw:7.2f} {n_mean:9.2f} {n_t_nw:8.2f} "
            f"{total:9.1f} {share * 100:7.2f}% {outcome['mean_names']:7.0f}",
            flush=True,
        )
        report[name] = {
            "gross_mean_bp": gross,
            "gross_t_nw": float(g_t_nw),
            "net_mean_bp": n_mean,
            "net_t_nw": float(n_t_nw),
            "net_total_pct": total,
            "turnover_share": float(share),
            "mean_names": float(outcome["mean_names"]),
            "n": int(len(sample)),
        }
    print()
    full = report["full"]
    print(
        f"全窗口净 {full['net_mean_bp']:.2f} bp/日 → 年化 {full['net_mean_bp'] * 244:.0f}%" if full["net_mean_bp"] < 1000 else ""
    )
    print(f"全窗口累计净收益 {full['net_total_pct']:.0f}%（{full['n']} 个交易日）")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
