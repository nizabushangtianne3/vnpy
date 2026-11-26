
"""Grade the lag-one daily book under a holding period and a turnover cap.

The daily-reselection book on the lag-one return is the first reading in the
repository that is strongly positive and strongly significant: 28 basis points a
session gross with a Newey-West t of 5.74 over the full window, and 22.62 with
a t of 2.37 on the hold half, with every calendar year positive. It charges a
full round trip every session, which no real account can execute, so the gross
number is a ceiling rather than a plan.

The two constraints that decide whether the reading converts are the holding
period and the turnover it forces. Holding longer than one session means the
signal is stale on the sessions after the first, which the same-session reading
shows is where the reversal lives; holding shorter is not possible. So the
question is not whether one session is best, which it is by construction, but
how much of the edge survives holding K sessions while only the best-ranked
names are refreshed each day, which is what a book with a turnover cap does.

This scans both: the holding period, and the fraction of the book refreshed on
each session, charging only the turnover that actually happens. The reading that
matters is the net one, and the window that matters is the hold half.

Usage::

    python scripts/alpha_lag1_execution.py [--json out.json]
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

BUY_RATE: float = 0.0003
SELL_RATE: float = 0.0013
SESSIONS_PER_YEAR: float = 244.0

WINDOWS = [
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
    ("full", "2022-01-20", "2026-11-13"),
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
        frame = frame.with_columns(pl.col("ret").shift(-lag).alias("fwd"))
        rows.append(frame.select("datetime", "vt_symbol", "fwd"))
    return pl.concat(rows, how="vertical")


def run(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
    refresh: float,
    hold_days: int,
) -> dict:
    """Median spread with a partial refresh each session and a holding horizon.

    ``refresh`` is the fraction of each leg that is replaced every session, so a
    book with a full K-day holding has ``refresh`` of ``1 / K`` and a fully
    rebuilt book has ``refresh`` of one. Names are taken from the top of the
    ranking first, so the refresh keeps the names the signal ranks highest.
    """
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {
        d[0] if isinstance(d, tuple) else d: g.sort("signal", descending=True)
        for d, g in window.group_by("datetime")
    }
    dates = sorted(groups)
    held_long: list[str] = []
    held_short: list[str] = []
    age: dict[str, int] = {}
    spreads: list[float] = []
    costs: list[float] = []
    turnover: list[float] = []
    for date in dates:
        group = groups[date].filter(pl.col("fwd").is_not_null())
        n = group.height
        if n < 300:
            continue
        universe = group["vt_symbol"].to_list()
        long_end = max(int(n * long_rate), 1)
        short_start = n - max(int(n * short_rate), 1)
        target_long = universe[:long_end]
        target_short = universe[short_start:]

        # Everything older than the holding horizon is sold whether or not it
        # is refreshed, so the horizon is a hard maximum rather than a target.
        held_long = [s for s in held_long if age.get(s, 0) < hold_days]
        held_short = [s for s in held_short if age.get(s, 0) < hold_days]

        # The refresh replaces the weakest-ranked names the book still holds, so
        # the best of the existing holdings survive and the book converges on
        # the current ranking rather than being rebuilt from scratch.
        by_rank = {sym: i for i, sym in enumerate(universe)}
        long_swap = max(int(len(target_long) * refresh), 1)
        short_swap = max(int(len(target_short) * refresh), 1)
        keep_long = sorted(
            [s for s in held_long if s in by_rank],
            key=lambda s: by_rank[s],
            reverse=True,
        )[: max(len(target_long) - long_swap, 0)]
        keep_short = sorted(
            [s for s in held_short if s in by_rank],
            key=lambda s: by_rank[s],
        )[: max(len(target_short) - short_swap, 0)]
        new_long = keep_long + [s for s in target_long if s not in keep_long][:long_swap]
        new_short = keep_short + [
            s for s in target_short if s not in keep_short
        ][:short_swap]

        entered_long = len(set(new_long) - set(held_long))
        entered_short = len(set(new_short) - set(held_short))
        entered = entered_long + entered_short
        left = len(set(held_long) - set(new_long)) + len(
            set(held_short) - set(new_short)
        )
        book = (len(new_long) + len(new_short)) or 1
        turnover.append((entered + left) / 2.0 / book)
        costs.append((entered * BUY_RATE + left * SELL_RATE) / book)

        for sym in new_long + new_short:
            age[sym] = age.get(sym, 0) + 1
        for sym in list(age):
            if sym not in new_long and sym not in new_short:
                age.pop(sym, None)

        held_long, held_short = new_long, new_short
        long_map = {
            sym: ret
            for sym, ret in zip(universe, group["fwd"].to_list(), strict=False)
        }
        short_map = long_map
        long_rets = [long_map[s] for s in held_long if s in long_map]
        short_rets = [short_map[s] for s in held_short if s in short_map]
        long_rets = [x for x in long_rets if x is not None and not np.isnan(x)]
        short_rets = [x for x in short_rets if x is not None and not np.isnan(x)]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spreads.append(float(np.median(long_rets)) - float(np.median(short_rets)))
    stocks = np.array(spreads)
    charged = np.array(costs)
    net = stocks - charged
    mean, t, p = grade(stocks)
    _, t_nw, p_nw = newey_west_t(stocks, 10)
    n_mean, n_t, n_p = grade(net)
    _, n_t_nw, n_p_nw = newey_west_t(net, 10)
    cumulative = np.cumprod(1.0 + net) - 1.0
    peak = np.maximum.accumulate(cumulative)
    sd_g = float(stocks.std(ddof=1))
    sd_n = float(net.std(ddof=1))
    return {
        "n": int(len(stocks)),
        "gross_mean_bp": mean * 10000,
        "gross_t_nw": t_nw,
        "gross_p_nw": p_nw,
        "net_mean_bp": n_mean * 10000,
        "net_t_nw": n_t_nw,
        "net_p_nw": n_p_nw,
        "net_total_pct": float(cumulative[-1] * 100),
        "max_dd_pct": float((cumulative - peak).min() * 100),
        "annual_vol_pct": sd_g * sqrt(SESSIONS_PER_YEAR) * 100,
        "net_ir": n_mean / (sd_n * sqrt(SESSIONS_PER_YEAR)) if sd_n else 0.0,
        "annual_turnover_x": float(np.mean(turnover) * SESSIONS_PER_YEAR)
        if turnover
        else 0.0,
        "net_annual_pct": float(n_mean * SESSIONS_PER_YEAR * 100),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Lag-one book under execution limits")
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

    report: dict[str, dict] = {}
    for refresh in [1.0, 0.5, 0.25, 0.1]:
        for hold_days in [1, 3, 5, 10, 30]:
            key = f"refresh {refresh} hold {hold_days}"
            report[key] = {}
            for name, start, end in WINDOWS:
                report[key][name] = run(
                    joined,
                    start,
                    end,
                    args.long_rate,
                    args.short_rate,
                    refresh,
                    hold_days,
                )
    print()
    print("净收益与净 t（Newey-West），按持有天数与每日刷新比例")
    print(
        f"{'config':22} {'full net bp':>12} {'tNW':>6} {'p':>6} {'full%':>9} "
        f"{'netIR':>6} {'turn/x':>7} {'hold net':>9} {'hold t':>7} {'fit net':>9}"
    )
    for key, windows in report.items():
        full = windows["full"]
        hold = windows["hold"]
        fit = windows["fit"]
        print(
            f"{key:22} {full['net_mean_bp']:12.2f} {full['net_t_nw']:6.2f} "
            f"{full['net_p_nw']:6.3f} {full['net_total_pct']:9.2f} "
            f"{full['net_ir']:6.2f} {full['annual_turnover_x']:7.1f} "
            f"{hold['net_mean_bp']:9.2f} {hold['net_t_nw']:7.2f} "
            f"{fit['net_mean_bp']:9.2f}",
            flush=True,
        )
    print()
    print("逐年（refresh 0.25, hold 5）：")
    yearly: dict[str, dict] = {}
    for name, start, end in YEARS:
        key = name
        yearly[key] = run(
            joined, start, end, args.long_rate, args.short_rate, 0.25, 5
        )
        value = yearly[key]
        print(
            f"{name} n={value['n']:4d} gross {value['gross_mean_bp']:7.2f}bp "
            f"(tNW {value['gross_t_nw']:5.2f})  net {value['net_mean_bp']:7.2f}bp "
            f"(tNW {value['net_t_nw']:5.2f})  turn {value['annual_turnover_x']:5.1f}x",
            flush=True,
        )
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
