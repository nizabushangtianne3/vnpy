"""How much the booster is allowed to fit decides the signal's stability.

The rolling fit currently gives the booster a thousand rounds with no early
stopping that can fire: the rolling code points the train and the validate
segment at the same window, so the validation loss is the training loss and
the best iteration is always the last one. On a market-sized cross-section
that is a model free to spend a thousand rounds memorising the window it was
given.

This script measures what that costs. It fits the same window with several
regularisation settings and scores each on the sessions that follow it, so
the comparison is between signals rather than between fitting times. The
window is the one the rolling fit uses, and the scoring window is the one
the rolling fit's first block would cover.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
LAB: str = "D:/Codex/work/vnpy-alpha-cache/alpha_lab"


def daily_ic(joined: pl.DataFrame) -> pl.Series:
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


CONFIGS: list[tuple[str, dict]] = [
    ("baseline", {}),
    ("leaves15", {"num_leaves": 15}),
    ("leaves7", {"num_leaves": 7}),
    ("minleaf200", {"min_data_in_leaf": 200}),
    ("minleaf1000", {"min_data_in_leaf": 1000}),
    ("feat0.5", {"feature_fraction": 0.5, "bagging_fraction": 0.8, "bagging_freq": 1}),
    ("feat0.3", {"feature_fraction": 0.3, "bagging_fraction": 0.8, "bagging_freq": 1}),
    ("l2_10", {"lambda_l2": 10.0}),
    ("l2_100", {"lambda_l2": 100.0}),
    ("regular", {"num_leaves": 15, "min_data_in_leaf": 200, "feature_fraction": 0.5,
                 "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 10.0}),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="market3000")
    parser.add_argument("--lab", default=LAB)
    parser.add_argument("--train-window", type=int, default=900)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--configs",
        nargs="*",
        default=[],
        help="names of the configurations to run; default is all",
    )
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab, Segment
    from vnpy.alpha.model.models.lgb_model import LgbModel

    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print("dataset missing")
        return 2

    dates = dataset.result_df.select("datetime").unique().sort("datetime")["datetime"].to_list()
    horizon = 5
    train_end_index = len(dates) - 250
    train_from = dates[train_end_index - args.train_window - horizon]
    train_to = dates[train_end_index - horizon - 1]
    test_from = dates[train_end_index]
    test_to = dates[min(train_end_index + 249, len(dates) - 1)]
    print(f"train {train_from} to {train_to}")
    print(f"test  {test_from} to {test_to}")

    dataset.data_periods[Segment.TRAIN] = (train_from, train_to)
    dataset.data_periods[Segment.VALID] = (train_from, train_to)
    dataset.data_periods[Segment.TEST] = (test_from, test_to)

    label = dataset.result_df.select(["datetime", "vt_symbol", "label"])
    label = label.with_columns(pl.col("datetime").cast(pl.Utf8).str.slice(0, 10))

    wanted = set(args.configs)
    for name, kwargs in CONFIGS:
        if wanted and name not in wanted:
            continue
        model = LgbModel(seed=args.seed, **kwargs)
        model.fit(dataset)
        predicted = model.predict(dataset, Segment.TEST)
        infer = dataset.fetch_infer(Segment.TEST).sort(["datetime", "vt_symbol"])
        frame = infer.with_columns(pl.Series("signal", predicted)).select(
            ["datetime", "vt_symbol", "signal"]
        )
        frame = frame.with_columns(
            pl.col("datetime").cast(pl.Utf8).str.slice(0, 10).alias("datetime")
        )
        joined = frame.join(label, on=["datetime", "vt_symbol"], how="inner")
        measured = stats(daily_ic(joined))
        rounds = model.model.best_iteration if model.model else 0
        print(
            f"{name:12s} rounds={rounds:4d} sessions={measured['sessions']:4d} "
            f"ic={measured['ic_mean']:.4f} icir={measured['icir']:.3f} "
            f"pos={measured['pos']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
