
"""Grade what the retained book actually holds, by name rather than by aggregate.

The buffer scan reported the long book occupying the fortieth percentile of the
cross-section when it was asked to hold the top five percent. That is either a
bookkeeping error in the diagnostic or the real composition of the book, and the
difference decides whether every band reading so far was a measurement of a top
quintile at all.

This prints one session's retained membership beside its rank, so the claim is
checkable by eye. A five percent band with a ten percent buffer retains names
down to the fifteenth percentile on the day they were ranked, and the question
this answers is how far a retained name actually drifts before the next
rebalance, because a name that leaves the top five percent on the day it is
selected and never re-enters the buffer is a name the book is holding on a rank
that no longer exists.

Usage::

    python scripts/alpha_book_composition.py
"""

from __future__ import annotations

import sys

import numpy as np
import polars as pl

from alpha_ls_window import find_cache, load_returns

WINDOW_START = "2022-01-20"
WINDOW_END = "2026-11-13"


def main() -> int:
    daily, sweeps = find_cache()
    signal_path = sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    print("signal:", signal_path, flush=True)
    bars = load_returns(daily)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(WINDOW_START).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(WINDOW_END).str.to_datetime())
    ).sort("datetime")
    groups = {
        d[0] if isinstance(d, tuple) else d: g.sort("signal", descending=True)
        for d, g in window.group_by("datetime")
    }
    dates = sorted(groups)
    print(f"sessions: {len(dates)}  first {dates[0]}  last {dates[-1]}")

    long_rate = 0.05
    short_rate = 0.05
    buffer_pp = 10.0
    rebalance = 30
    held_long: set[str] = set()
    held_short: set[str] = set()
    since = rebalance
    for index, date in enumerate(dates):
        group = groups[date].filter(pl.col("ret").is_not_null())
        n = group.height
        if n < 300:
            continue
        universe = group["vt_symbol"].to_list()
        rank = {sym: i for i, sym in enumerate(universe)}
        long_end = max(int(n * long_rate), 1)
        short_start = n - max(int(n * short_rate), 1)
        long_keep = min(n, long_end + int(n * buffer_pp / 100.0))
        short_keep_start = max(0, short_start - int(n * buffer_pp / 100.0))
        since += 1
        rebalanced = False
        if since >= rebalance:
            since = 0
            rebalanced = True
            zone_long = set(universe[:long_keep])
            zone_short = set(universe[short_keep_start:])
            new_long = (held_long & zone_long) | set(universe[:long_end])
            new_short = (held_short & zone_short) | set(universe[short_start:])
            held_long, held_short = new_long, new_short
        if not held_long:
            continue
        present = [rank[sym] for sym in held_long if sym in rank]
        if not present:
            continue
        mean_rank = float(np.mean(present)) / n * 100
        if index % 60 == 0 or rebalanced:
            print(
                f"{str(date)[:10]} reb={int(rebalanced)} held={len(held_long):4d} "
                f"mean_rank={mean_rank:6.2f}%  p90_rank={np.percentile(present, 90) / n * 100:6.2f}% "
                f"band_edge={long_end / n * 100:5.2f}%",
                flush=True,
            )
        if index > 300:
            break
    print()
    print("mean_rank 明显高于 band_edge 即簿子持有的中位名字已跌出目标带")
    return 0


if __name__ == "__main__":
    sys.exit(main())
