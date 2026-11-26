
"""Grade the book that rebalances daily on a lag-one return.

The same-session spread is the strongest reading in the repository and it is
strongly negative: the top slice against the bottom slice, measured on the
session the ranking was made, returns -33 basis points a session on the widest
band with a Newey-West t of -7.19, and it is negative in every calendar year
measured. That is not a failing signal, it is the signature of a ranking built
from a label the session has not finished computing: the signal is a prediction
of the return that follows it, and crediting it with the session's own return
measures the reverse.

The reverse is the boundary of the finding. A book that rebalances every
session and holds for exactly one session measures the return the signal was
actually trained on, which is the definition of the horizon it used. This is the
one combination never measured: daily rebalancing, one-session holding, median
member on both legs, charged the full round trip each day.

The cost is the whole question. A full turnover a day at three basis points to
buy and thirteen to sell is sixteen basis points, which is larger than the
spread every other reading measured, so the net reading is expected to be
negative and the gross reading is the one that decides whether the idea is
sound before any transaction-cost work is done.

Usage::

    python scripts/alpha_lag1_daily_book.py [--json out.json]
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
    ("2022-2024", "2022-01-20", "2024-12-31"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
    ("full", "2022-01-20", "2026-11-13"),
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
    """Close-to-close return of the session ``lag`` sessions after each session."""
    rows: list[pl.DataFrame] = []
    for path in sorted(daily.glob("*.parquet")):
        frame = pl.read_parquet(path, columns=["datetime", "close"]).sort("datetime")
        if frame.height < lag + 3:
            continue
        frame = frame.with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"),
            pl.lit(path.stem).alias("vt_symbol"),
        )
        # The decision session is the one the signal was computed on, and the
        # return it earns starts the session after, so the return series is
        # shifted forward by ``lag`` sessions against the decision date.
        frame = frame.with_columns(pl.col("ret").shift(-lag).alias("fwd"))
        rows.append(frame.select("datetime", "vt_symbol", "fwd"))
    return pl.concat(rows, how="vertical")


def run(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
    cost_rate: float,
) -> dict:
    """Median spread of a book rebuilt every session on the lag-one return."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {
        d[0] if isinstance(d, tuple) else d: g.sort("signal", descending=True)
        for d, g in window.group_by("datetime")
    }
    spreads: list[float] = []
    for date in sorted(groups):
        group = groups[date].filter(pl.col("fwd").is_not_null())
        n = group.height
        if n < 300:
            continue
        long_end = max(int(n * long_rate), 1)
        short_start = n - max(int(n * short_rate), 1)
        long_rets = group["fwd"].to_numpy()[:long_end]
        short_rets = group["fwd"].to_numpy()[short_start:]
        long_rets = long_rets[~np.isnan(long_rets)]
        short_rets = short_rets[~np.isnan(short_rets)]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spreads.append(float(np.median(long_rets)) - float(np.median(short_rets)))
    stocks = np.array(spreads)
    charged = np.full(len(spreads), cost_rate)
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
        "gross_t": t,
        "gross_t_nw": t_nw,
        "gross_p_nw": p_nw,
        "net_mean_bp": n_mean * 10000,
        "net_t_nw": n_t_nw,
        "net_p_nw": n_p_nw,
        "net_total_pct": float(cumulative[-1] * 100),
        "max_dd_pct": float((cumulative - peak).min() * 100),
        "annual_vol_pct": sd_g * sqrt(SESSIONS_PER_YEAR) * 100,
        "net_ir": n_mean / (sd_n * sqrt(SESSIONS_PER_YEAR)) if sd_n else 0.0,
        "gross_ir": mean / (sd_g * sqrt(SESSIONS_PER_YEAR)) if sd_g else 0.0,
        "positive_share": float((stocks > 0).mean()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily book on the lag-one return")
    parser.add_argument("--signal", default="")
    parser.add_argument("--cost-rate", type=float, default=BUY_RATE + SELL_RATE)
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
    print("join rows:", joined.height, flush=True)

    report: dict[str, dict] = {}
    for label, long_rate, short_rate in [
        ("L5/S5", 0.05, 0.05),
        ("L10/S10", 0.10, 0.10),
        ("L10/S5", 0.10, 0.05),
        ("L20/S20", 0.20, 0.20),
    ]:
        for name, start, end in WINDOWS:
            key = f"{label} {name}"
            report[key] = run(joined, start, end, long_rate, short_rate, args.cost_rate)
    print()
    print(f"每日重选、持有一日（成本 {args.cost_rate * 10000:.0f}bp/日，全额换手）")
    print(
        f"{'config':18} {'n':>5} {'gross bp':>9} {'g_tNW':>7} {'pNW':>7} {'net bp':>8} "
        f"{'n_tNW':>7} {'n_pNW':>7} {'net%':>8} {'maxDD%':>8} {'vol%':>6} {'netIR':>6} "
        f"{'gIR':>6} {'pos%':>6}"
    )
    for key, value in report.items():
        print(
            f"{key:18} {value['n']:5d} {value['gross_mean_bp']:9.2f} "
            f"{value['gross_t_nw']:7.2f} {value['gross_p_nw']:7.3f} "
            f"{value['net_mean_bp']:8.2f} {value['net_t_nw']:7.2f} "
            f"{value['net_p_nw']:7.3f} {value['net_total_pct']:8.2f} "
            f"{value['max_dd_pct']:8.2f} {value['annual_vol_pct']:6.2f} "
            f"{value['net_ir']:6.2f} {value['gross_ir']:6.2f} "
            f"{value['positive_share'] * 100:6.1f}",
            flush=True,
        )
    print()
    print("逐年（L10/S5）：")
    yearly: dict[str, dict] = {}
    for name, start, end in YEARS:
        key = f"L10/S5 {name}"
        yearly[key] = run(joined, start, end, 0.10, 0.05, args.cost_rate)
        value = yearly[key]
        print(
            f"{name} n={value['n']:4d} gross {value['gross_mean_bp']:8.2f}bp "
            f"(tNW {value['gross_t_nw']:5.2f})  net {value['net_mean_bp']:8.2f}bp "
            f"(tNW {value['net_t_nw']:5.2f})",
            flush=True,
        )
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"report": report, "yearly": yearly}, ensure_ascii=False, indent=1)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
