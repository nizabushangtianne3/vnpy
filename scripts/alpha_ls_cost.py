"""Decide whether the lag-one spread survives the costs of getting it.

Every daily-spread reading so far credited the book with the return of the very
session it was selected on, which a signal computed at that session's close
cannot earn. Moving the return one session out is the honest ruler, and it
changes the sign of the answer, so the reading has to be re-graded with the
costs of trading attached rather than reported as a gross number.

A rebalance that changes a fraction ``f`` of the book pays the buy rate on the
names entering and the sell rate on the names leaving, which is charged against
the same daily return series the t statistic is computed on. The net series is
what a spread has to clear, not the gross one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

from alpha_ls_window import find_cache, load_returns
from alpha_ls_newey import newey_west_t


def series(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
    buffer_ratio: float,
    rebalance: int,
    lag: int,
    buy_rate: float,
    sell_rate: float,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Net spread, gross spread, and the share of gross paid to trade.

    A rebalance pays one rate on the names that enter and the other on the
    names that leave. The cost is charged to the session the book enters, which
    is the session whose return is being credited, so the bookkeeping appends it
    beside the gross spread rather than indexing into a list that is still short.
    """
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    dates = sorted(window["datetime"].unique().to_list())
    by_date = {
        d: window.filter(pl.col("datetime") == d).sort("signal", descending=True)
        for d in dates
    }
    index = {d: i for i, d in enumerate(dates)}

    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    gross: list[float] = []
    costs: list[float] = []
    for date in dates:
        group = by_date[date]
        n = group.height
        if n < 300:
            continue
        since += 1
        cost = 0.0
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
            new_long = (held_long & set(universe[i] for i in long_band)) | set(
                universe[:long_end]
            )
            new_short = (held_short & set(universe[i] for i in short_band)) | set(
                universe[short_start:]
            )
            entered = len(new_long - held_long) + len(new_short - held_short)
            left = len(held_long - new_long) + len(held_short - new_short)
            held_long, held_short = new_long, new_short
            cost = (entered * buy_rate + left * sell_rate) / max(
                len(held_long) + len(held_short), 1
            )

        target = index[date] + lag
        if target >= len(dates) or not held_long or not held_short:
            continue
        future = by_date[dates[target]]
        longs = future.filter(pl.col("vt_symbol").is_in(list(held_long)))
        shorts = future.filter(pl.col("vt_symbol").is_in(list(held_short)))
        long_rets = [x for x in longs["ret"].to_list() if x is not None]
        short_rets = [x for x in shorts["ret"].to_list() if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        costs.append(cost)
        gross.append(float(np.mean(long_rets)) - float(np.mean(short_rets)))

    gross_arr = np.array(gross)
    cost_arr = np.array(costs[: len(gross_arr)])
    share = (
        float(cost_arr.sum() / abs(gross_arr).sum()) if abs(gross_arr).sum() else 0.0
    )
    return gross_arr - cost_arr, gross_arr, share


def main() -> int:
    parser = argparse.ArgumentParser(description="Cost-aware lag-one grade")
    parser.add_argument("--signal", default="")
    parser.add_argument("--buy-rate", type=float, default=0.0003)
    parser.add_argument("--sell-rate", type=float, default=0.0013)
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(load_returns(daily), on=["datetime", "vt_symbol"], how="inner")

    windows = [
        ("2022-2024", "2022-01-20", "2024-12-31"),
        ("2025-2026", "2025-01-02", "2026-11-13"),
    ]
    print(
        f"成本口径：买入 {args.buy_rate * 1e4:.0f}bp / 卖出 {args.sell_rate * 1e4:.0f}bp（含过户与卖出印花税）"
    )
    print(
        f"{'window':12} {'clock':>6} {'band':10} {'n':>5} {'净日均%':>9} {'t':>6} {'p':>6} {'成本占毛':>9}"
    )
    for name, start, end in windows:
        for rebalance in (5, 30):
            for long_rate, short_rate in [(0.05, 0.05), (0.05, 0.10), (0.10, 0.10)]:
                net, gross, share = series(
                    joined,
                    start,
                    end,
                    long_rate,
                    short_rate,
                    0.10,
                    rebalance,
                    1,
                    args.buy_rate,
                    args.sell_rate,
                )
                if len(net) < 40:
                    continue
                _, t, p = newey_west_t(net, 10)
                print(
                    f"{name} {rebalance:6d} L{int(long_rate * 100)}/S{int(short_rate * 100)}    "
                    f"{len(net):5d} {net.mean() * 100:9.4f} {t:6.2f} {p:6.3f} {share * 100:8.1f}%",
                    flush=True,
                )
        print(flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
