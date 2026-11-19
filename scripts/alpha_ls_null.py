"""Grade the lag-one spread against a signal that cannot know anything.

The lag-one reading is the first significant long-short result in this note, and
a first significant result is exactly the thing a null control has to be run
against. A shuffled signal keeps every distributional property of the real one
-- the same names, the same cross-sectional spread, the same rebalance clock --
and carries no information at all, so whatever the book earns on it is the noise
floor of the measurement itself rather than the ranking.

Any reading of the real signal must be compared to the same reading on several
independent shuffles. If the real spread is inside the shuffle distribution,
the measurement cannot tell the ranking from a dice roll.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

from alpha_ls_window import find_cache, load_returns
from alpha_ls_newey import newey_west_t
from alpha_ls_cost import series


def shuffled(signal: pl.DataFrame, seed: int) -> pl.DataFrame:
    """Permute the signal within each session, keeping every other column."""
    # A single global shuffle would also destroy the per-session cross-sectional
    # shape, so the permutation is done inside each date's block, which is what
    # the book actually reads.
    parts: list[pl.DataFrame] = []
    for _, group in signal.group_by("datetime", maintain_order=True):
        values = group["signal"].to_numpy().copy()
        generator = np.random.default_rng(seed + int(group["datetime"][0].timestamp()))
        generator.shuffle(values)
        parts.append(group.with_columns(pl.Series("signal", values)))
    return pl.concat(parts, how="vertical")


def main() -> int:
    parser = argparse.ArgumentParser(description="Random-signal control")
    parser.add_argument("--signal", default="")
    parser.add_argument("--shuffles", type=int, default=5)
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
    bars = load_returns(daily)
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")

    settings = [
        ("2022-2024", "2022-01-20", "2024-12-31", 30),
        ("2022-2024", "2022-01-20", "2024-12-31", 5),
        ("2025-2026", "2025-01-02", "2026-11-13", 30),
        ("2025-2026", "2025-01-02", "2026-11-13", 5),
    ]
    print(
        f"{'window':12} {'clock':>5} {'真实 net%':>9} {'t':>6} {'p':>6} | {'shuffles net%':>34} {'shuf t':>18}"
    )
    for name, start, end, rebalance in settings:
        net, _, share = series(
            joined,
            start,
            end,
            0.05,
            0.05,
            0.10,
            rebalance,
            1,
            args.buy_rate,
            args.sell_rate,
        )
        _, t, p = newey_west_t(net, 10)
        null_nets: list[float] = []
        null_ts: list[float] = []
        for seed in range(args.shuffles):
            shuffled_signal = shuffled(signal, seed)
            null_joined = shuffled_signal.join(
                bars, on=["datetime", "vt_symbol"], how="inner"
            )
            null_net, _, _ = series(
                null_joined,
                start,
                end,
                0.05,
                0.05,
                0.10,
                rebalance,
                1,
                args.buy_rate,
                args.sell_rate,
            )
            if len(null_net) < 40:
                continue
            _, null_t, _ = newey_west_t(null_net, 10)
            null_nets.append(null_net.mean() * 100)
            null_ts.append(null_t)
        inside = bool(null_nets and min(null_nets) < net.mean() * 100 < max(null_nets))
        print(
            f"{name} {rebalance:5d} {net.mean() * 100:9.4f} {t:6.2f} {p:6.3f} | "
            f"min={min(null_nets) if null_nets else float('nan'):+.4f} "
            f"max={max(null_nets) if null_nets else float('nan'):+.4f} "
            f"{'CANNOT TELL APART' if inside else 'OUTSIDE the null'} "
            f"{max(abs(x) for x in null_ts) if null_ts else float('nan'):6.2f}"
            f" 成本占毛 {share * 100:.1f}%",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
