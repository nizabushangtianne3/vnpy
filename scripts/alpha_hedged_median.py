"""Grade the median book with an ETF hedge over its market direction.

The median-member book is the first structure whose reading is not carried by
the fat tail of the cross-section, and it compounds +10.43% net over five years
where the equal-weight book compounds +0.32%. But its maximum drawdown is
-46.78% on an annualised volatility of 22.90%, which is a market direction the
signal never claimed to predict, and a drawdown of that size is not a book that
can be left to run.

The ETF leg is the only hedge the repository has real data for, and its earlier
rejection was on the selection leg losing in the hold half, not on the hedge
itself failing to remove the direction. This script keeps the median member
selection and puts the ETF short leg beside it, then reports the book with the
hedge applied at the ratios that were fitted before, so the question is
answerable: does hedging the direction of the median book leave a positive
spread, and at what volatility.

The hedge ratio is scanned rather than chosen, because the earlier scans showed
the ratio flips sign between halves and a ratio picked on one half is not a
ratio. What is being graded here is the shape of the curve, not its peak.

Usage::

    python scripts/alpha_hedged_median.py [--json out.json]
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
WINDOW_START = "2022-01-20"
WINDOW_END = "2026-11-13"
BORROW_RATE = 0.08

WINDOWS = [
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
    ("2022-2024", "2022-01-20", "2024-12-31"),
    ("2025-2026", "2025-01-02", "2026-11-13"),
    ("full", "2022-01-20", "2026-11-13"),
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


def load_hedge(daily: Path, symbol: str) -> pl.DataFrame:
    """Close-to-close return of the hedge instrument, one row per session."""
    path = daily / f"{symbol}.parquet"
    frame = pl.read_parquet(path, columns=["datetime", "close"]).sort("datetime")
    # The instrument's first session has no previous close, so the return is
    # null; treating that null as zero keeps the join from carrying it forward.
    return (
        frame.with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("hret")
        )
        .select("datetime", "hret")
        .fill_null(0.0)
    )


def run(
    joined: pl.DataFrame,
    hedge: pl.DataFrame,
    start: str,
    end: str,
    top: float,
    bottom: float,
    buffer_ratio: float,
    rebalance: int,
    ratio: float,
    borrow_rate: float,
) -> dict:
    """Median-member spread, the ETF short leg, and the book with both."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {
        d[0] if isinstance(d, tuple) else d: g.sort("signal", descending=True)
        for d, g in window.group_by("datetime")
    }
    dates = sorted(groups)
    hedge_map = dict(
        zip(hedge["datetime"].to_list(), hedge["hret"].to_list(), strict=False)
    )
    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    spreads: list[float] = []
    charges: list[float] = []
    daily_charge: float = 0.0
    hrows: list[float] = []
    for date in dates:
        group = groups[date].filter(pl.col("ret").is_not_null())
        if group.height < 300:
            continue
        since += 1
        daily_charge = 0.0
        if since >= rebalance:
            since = 0
            n = group.height
            universe = group["vt_symbol"].to_list()
            long_end = max(int(n * top), 1)
            short_start = min(int(n * (1 - bottom)), n - 1)
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
            book = (len(new_long) + len(new_short)) or 1
            # Only the round-trip trading cost lands on the session the book
            # is rebalanced on, because that is the session whose spread is
            # being credited with the trade that paid it.
            daily_charge = (entered * 0.0003 + left * 0.0013) / book
            held_long, held_short = new_long, new_short
        if not held_long or not held_short:
            continue
        long_rets = group.filter(
            pl.col("vt_symbol").is_in(list(held_long))
        )["ret"].to_numpy()
        short_rets = group.filter(
            pl.col("vt_symbol").is_in(list(held_short))
        )["ret"].to_numpy()
        long_rets = long_rets[~np.isnan(long_rets)]
        short_rets = short_rets[~np.isnan(short_rets)]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spread = float(np.median(long_rets)) - float(np.median(short_rets))
        spreads.append(spread)
        # Every held session carries a charge: the borrow fee accrues on the
        # short notional each natural day rather than only on rebalance days.
        charges.append(daily_charge + ratio * borrow_rate / 360.0)
        hrows.append(float(hedge_map.get(date, float("nan"))))
    stocks = np.array(spreads)
    hret = np.array(hrows)
    costs = np.array(charges[: len(spreads)])
    # The ETF leg is short, so its pnl is the negative of the instrument's own
    # return, and the book's return is the stock spread plus that leg less the
    # borrow fee it accrues.
    hedged = stocks - ratio * hret - costs
    s_mean, s_t, s_p = grade(stocks)
    _, s_t_nw, s_p_nw = newey_west_t(stocks, 10)
    h_mean, h_t, h_p = grade(hedged)
    _, h_t_nw, h_p_nw = newey_west_t(hedged, 10)
    sd = float(hedged.std(ddof=1))
    cumulative = np.cumprod(1.0 + hedged) - 1.0
    peak = np.maximum.accumulate(cumulative)
    return {
        "n": int(len(stocks)),
        "stock_mean_bp": s_mean * 10000,
        "stock_t_nw": s_t_nw,
        "hedged_mean_bp": h_mean * 10000,
        "hedged_t_nw": h_t_nw,
        "hedged_p_nw": h_p_nw,
        "hedged_total_pct": float(cumulative[-1] * 100),
        "hedged_max_dd_pct": float((cumulative - peak).min() * 100),
        "hedged_vol_pct": sd * sqrt(SESSIONS_PER_YEAR) * 100,
        "hedged_ir": h_mean / sd * sqrt(SESSIONS_PER_YEAR) if sd else 0.0,
        "fee_bp": float(costs.sum()) * 10000,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hedged median book")
    parser.add_argument("--signal", default="")
    parser.add_argument("--hedge-symbol", default="510300.SSE")
    parser.add_argument("--borrow-rate", type=float, default=BORROW_RATE)
    parser.add_argument("--buffer-ratio", type=float, default=0.10)
    parser.add_argument("--rebalance", type=int, default=30)
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
    hedge = load_hedge(daily, args.hedge_symbol)
    print(f"hedge {args.hedge_symbol}: {hedge.height} sessions", flush=True)

    report: dict[str, dict] = {}
    for ratio in [0.0, 0.25, 0.5, 0.75, 1.0, 1.25]:
        for name, start, end in WINDOWS:
            key = f"ratio {ratio} {name}"
            report[key] = run(
                joined,
                hedge,
                start,
                end,
                0.05,
                0.05,
                args.buffer_ratio,
                args.rebalance,
                ratio,
                args.borrow_rate,
            )
    print()
    print("中位数 L5/S5 簿 + ETF 空头腿（clock 30，借券费 8%）")
    print(
        f"{'config':22} {'n':>5} {'stock bp':>9} {'s_tNW':>7} {'hedged bp':>10} "
        f"{'h_tNW':>7} {'h_pNW':>7} {'tot%':>8} {'maxDD%':>8} {'vol%':>6} {'IR':>6} {'fee bp':>8}"
    )
    for key, value in report.items():
        print(
            f"{key:22} {value['n']:5d} {value['stock_mean_bp']:9.2f} "
            f"{value['stock_t_nw']:7.2f} {value['hedged_mean_bp']:10.2f} "
            f"{value['hedged_t_nw']:7.2f} {value['hedged_p_nw']:7.3f} "
            f"{value['hedged_total_pct']:8.2f} {value['hedged_max_dd_pct']:8.2f} "
            f"{value['hedged_vol_pct']:6.2f} {value['hedged_ir']:6.2f} "
            f"{value['fee_bp']:8.1f}",
            flush=True,
        )
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
