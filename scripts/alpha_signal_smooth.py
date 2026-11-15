"""Does smoothing the signal over time raise ICIR?

ICIR is the mean daily IC over its standard deviation, so the gate is asking
for a signal whose daily ranking is stable. One source of instability is
obvious and free to remove: the model is refitted on a rolling schedule and
each refit produces its own ranking, so the same name can jump from the top
of the ranking to the middle on the day the booster changed.

Averaging a prediction over the last few sessions does not change what the
signal claims about the future - it still ranks names by expected return -
but it dampens the day-to-day reshuffling. Whether that raises ICIR or just
smears a real edge into the next week is an empirical question, so this
script measures both: the IC statistics, and the forward return of the
smoothed ranking, so a smoother signal that earns less is visible.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
LAB: str = "D:/Codex/work/vnpy-alpha-cache/alpha_lab"
SIGNAL_DIR: str = "D:/Codex/work/vnpy-alpha-cache/sweeps"


def daily_ic(joined: pl.DataFrame) -> pl.Series:
    """Daily cross-sectional rank IC."""
    pdf = joined.to_pandas()
    if pdf.empty:
        return pl.Series("ic", [], dtype=pl.Float64)

    def rank_ic(group) -> float:
        sig = group["signal"]
        lab = group["label"]
        if sig.nunique() < 2 or lab.nunique() < 2:
            return float("nan")
        return float(np.corrcoef(sig.rank(), lab.rank())[0, 1])

    ic = pdf.groupby("datetime").apply(rank_ic, include_groups=False).dropna()
    return pl.Series("ic", ic.astype(float).to_list())


def stats(ic: pl.Series) -> dict:
    if len(ic) == 0:
        return {"sessions": 0, "ic_mean": 0.0, "icir": 0.0, "pos": 0.0}
    return {
        "sessions": len(ic),
        "ic_mean": float(ic.mean()),
        "icir": float(ic.mean() / ic.std()) if ic.std() else 0.0,
        "pos": float((ic > 0).mean()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="market3000")
    parser.add_argument("--lab", default=LAB)
    parser.add_argument("--signal-dir", default=SIGNAL_DIR)
    parser.add_argument("--rolling", nargs=2, type=int, default=[900, 250])
    parser.add_argument(
        "--windows",
        nargs="+",
        type=int,
        default=[1, 2, 3, 5, 10, 20],
        help="number of sessions to average the prediction over",
    )
    parser.add_argument("--date-from", default="2024-09-26")
    parser.add_argument("--date-to", default="2026-11-13")
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab

    train_window, step = args.rolling
    cache = (
        Path(args.signal_dir)
        / f"{args.dataset}_rolling{train_window}_{step}.parquet"
    )
    if not cache.exists():
        print(f"signal cache missing: {cache}")
        print("run scripts/alpha_universe_scan.py once to build it")
        return 2

    signal = pl.read_parquet(cache)
    signal = signal.with_columns(
        pl.col("vt_symbol").str.split(".").list.first().alias("code")
    )
    signal = signal.with_columns(pl.col("datetime").cast(pl.Utf8).str.slice(0, 10))
    signal = signal.filter(
        (pl.col("datetime") >= args.date_from) & (pl.col("datetime") <= args.date_to)
    )
    print(f"signal rows: {signal.height}")

    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print("dataset missing")
        return 2
    label = dataset.result_df.select(["datetime", "vt_symbol", "label"])
    label = label.with_columns(pl.col("datetime").cast(pl.Utf8).str.slice(0, 10))

    joined = signal.join(label, on=["datetime", "vt_symbol"], how="inner")
    print(f"joined rows: {joined.height}")

    for window in args.windows:
        if window == 1:
            smoothed = joined.with_columns(pl.col("signal").alias("smooth_signal"))
        else:
            # The average is taken over the previous sessions for the same
            # symbol, so a name's smoothed score on session T uses only
            # signals from sessions before T.
            smoothed = joined.with_columns(
                pl.col("signal")
                .rolling_mean(window_size=window, min_samples=window)
                .over("code")
                .alias("smooth_signal")
            )
            smoothed = smoothed.filter(pl.col("smooth_signal").is_not_null())
        frame = smoothed.select(
            [
                "datetime",
                "label",
                pl.col("smooth_signal").alias("signal"),
            ]
        )
        ic = daily_ic(frame)
        measured = stats(ic)
        print(
            f"window={window:3d} sessions={measured['sessions']:4d} "
            f"ic={measured['ic_mean']:.4f} icir={measured['icir']:.3f} "
            f"pos={measured['pos']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
