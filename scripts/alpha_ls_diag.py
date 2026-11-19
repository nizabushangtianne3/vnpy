"""Separate gross spread from trading cost, for the real signal and for shuffles.

The lag-one reading is the first significant long-short result in the note, and
it is only trustworthy if the gross spread is what carries it. A rebalance
charges one rate on the names entering and the other on the names leaving, so a
book whose gross is zero still loses money, and a shuffle pays that cost without
earning anything back. Comparing the real signal against shuffles on gross alone
is what tells whether the ranking earns a spread or merely avoids the cost the
shuffle pays.

The same-day reading is printed beside it because it is the number that
previously carried the conclusion, and it moves in the opposite direction to the
lag-one reading. Something has to explain that, and a per-session IC against the
return it is credited with is the cheapest explanation to test.
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
from alpha_ls_null import shuffled


def horizon_ic(joined: pl.DataFrame, lag: int) -> tuple[float, int]:
    """Mean rank IC of the signal against the return it earns ``lag`` on."""
    frame = joined
    if lag:
        frame = frame.sort(["vt_symbol", "datetime"]).with_columns(
            pl.col("ret").shift(-lag).over("vt_symbol")
        )
    daily: list[float] = []
    for _, group in frame.group_by("datetime"):
        sig = group["signal"].rank().to_numpy()
        lab = group["ret"].rank().to_numpy()
        if np.isnan(lab).any() or sig.std() == 0 or np.nanstd(lab) == 0:
            continue
        a = sig[~np.isnan(lab)]
        b = lab[~np.isnan(lab)]
        if len(a) < 50:
            continue
        daily.append(float(np.corrcoef(a, b)[0, 1]))
    return float(np.mean(daily)), len(daily)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gross vs cost, real against null")
    parser.add_argument("--signal", default="")
    parser.add_argument("--shuffles", type=int, default=3)
    args = parser.add_argument if False else parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    bars = load_returns(daily)
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")

    print("信号与『它所赚的收益』的逐日 rank IC：")
    for lag in (0, 1, 2, 3, 5):
        ic, n = horizon_ic(joined, lag)
        print(f"  lag={lag}: IC={ic:+.4f}  sessions={n}", flush=True)
    print(flush=True)

    print("gross / cost / net 分离（band 5%/5%, clock 5）:")
    print(
        f"{'window':12} {'signal':10} {'gross%':>9} {'cost bp/day':>12} {'net%':>9} {'t':>6} {'p':>6}"
    )
    for name, start, end in [
        ("2022-2024", "2022-01-20", "2024-12-31"),
        ("2025-2026", "2025-01-02", "2026-11-13"),
    ]:
        for label, frame_signal in [("real", signal)] + [
            (f"shuf{i}", shuffled(signal, i)) for i in range(args.shuffles)
        ]:
            subject = frame_signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
            net, gross, share = series(
                subject, start, end, 0.05, 0.05, 0.10, 5, 1, 0.0003, 0.0013
            )
            if len(net) < 40:
                continue
            _, t, p = newey_west_t(net, 10)
            cost_bp = float((gross - net).mean() * 10000)
            print(
                f"{name} {label:10} {gross.mean() * 100:9.4f} {cost_bp:12.3f} "
                f"{net.mean() * 100:9.4f} {t:6.2f} {p:6.3f}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
