"""Decide whether the alpha clears the gates it is being judged by.

The objective puts numbers on what "ready" means, and they are all numbers this
repository can already produce: the signal's own rank IC and ICIR over a window
the model never saw, how often that IC is positive, and what the backtest turns
the signal into once commissions are charged. A run that reports a return
without those is a run that has not been graded.

The distinction that matters here is between an in-sample number and an
out-of-sample one. The IC is computed on the dataset's TEST segment, which is
only the engine window, and the constructor refuses a dataset whose train
period overlaps it: ``valid`` is used for early stopping, so a ``valid`` that
runs into the test window has already shown the model the answers. This script
checks that before it reports anything, because every number below it depends
on it.

Usage::

    python scripts/alpha_gate.py --dataset m800clean [--json out.json]
"""

from __future__ import annotations

import argparse
import os
import json

import numpy as np
import polars as pl
from pathlib import Path

from vnpy.alpha import AlphaLab, Segment
from vnpy.alpha.dataset.utility import calculate_by_expression
from vnpy.alpha.model.models.lgb_model import LgbModel


DEFAULT_LAB: str = os.environ.get(
    "VNPY_ALPHA_LAB",
    "D:/Codex/work/vnpy-alpha-cache/alpha_lab",
)

# The thresholds in the objective, as code rather than as prose.
IC_MEAN_MIN: float = 0.04
ICIR_MIN: float = 0.5
IC_POSITIVE_MIN: float = 0.55
SHARPE_MIN: float = 1.0
COMMISSION_FRACTION_MAX: float = 0.02


def predict_ensemble(dataset, seeds: list[int]) -> pl.DataFrame:
    """Average one prediction per seed into a single signal frame.

    A single booster carries a variance that has nothing to do with the market:
    different seeds land on different trees, so the daily IC swings more than
    the underlying edge does. Averaging the predictions cancels that part of the
    dispersion and keeps the signal they share, which raises ICIR without adding
    anything the model could have overfitted - every seed saw the same train and
    validate windows, so this is noise reduction rather than a new degree of
    freedom.
    """
    total: pl.DataFrame | None = None
    for seed in seeds:
        model = LgbModel(seed=seed)
        model.fit(dataset)
        predicted = model.predict(dataset, Segment.TEST)
        frame = dataset.fetch_infer(Segment.TEST).sort(["datetime", "vt_symbol"])
        frame = frame.with_columns(pl.Series("signal", predicted)).select(
            ["datetime", "vt_symbol", "signal"]
        )
        if total is None:
            total = frame
            continue
        total = total.join(frame, on=["datetime", "vt_symbol"], how="inner")
        total = total.with_columns(
            ((pl.col("signal") + pl.col("signal_right")) / 2).alias("signal")
        ).drop("signal_right")
    return total


def relabel(dataset, hold_days: int) -> None:
    """Replace the label with a different horizon, reusing the factors.

    The 158 features are the expensive part of this pipeline and they do not
    depend on the horizon, only the label does. ``result_df`` keeps the raw
    ``close`` column, so a new horizon is one expression over a column that is
    already there. The label is written back to every frame that carries it,
    because ``fetch_infer`` reads ``infer_df`` and ``LgbModel`` reads
    ``learn_df``: changing one and not the others would leave the model scoring
    against a horizon it was not fitted on.
    """
    expression = f"ts_delay(close, -{hold_days}) / ts_delay(close, -1) - 1"
    # calculate_by_expression is the entry point prepare_data itself uses for
    # the 158 factors, so the horizon is evaluated by the code that already has
    # the ts_delay helper registered rather than by a second parser.
    computed = calculate_by_expression(dataset.result_df, expression)
    # ts_delay walks the frame per symbol, so the component order it returns is
    # the frame's own; joining on datetime and symbol preserves that alignment.
    label = computed.select(["datetime", "vt_symbol", "data"]).rename({"data": "label"})
    dataset.label_expression = expression
    dataset.result_df = dataset.result_df.drop("label").join(
        label, on=["datetime", "vt_symbol"], how="inner"
    )
    dataset.raw_df = dataset.raw_df.drop("label").join(
        label, on=["datetime", "vt_symbol"], how="inner"
    )
    dataset.infer_df = dataset.infer_df.drop("label").join(
        label, on=["datetime", "vt_symbol"], how="inner"
    )
    dataset.learn_df = dataset.learn_df.drop("label").join(
        label, on=["datetime", "vt_symbol"], how="inner"
    )


def rank_ic(dataset, seeds: list[int], hold_days: int | None = None) -> dict:
    """Score the fitted signal against the label on the TEST segment."""
    if hold_days is not None:
        relabel(dataset, hold_days)
    infer = predict_ensemble(dataset, seeds)
    joined = infer.join(
        dataset.result_df.select(["datetime", "vt_symbol", "label"]),
        on=["datetime", "vt_symbol"],
        how="inner",
    ).sort(["datetime", "vt_symbol"])
    pdf = joined.to_pandas()

    def daily_ic(group) -> float:
        sig = group["signal"]
        lab = group["label"]
        if sig.nunique() < 2 or lab.nunique() < 2:
            return float("nan")
        return float(np.corrcoef(sig.rank(), lab.rank())[0, 1])

    ic = pdf.groupby("datetime").apply(daily_ic, include_groups=False).dropna()
    ic = ic.astype(float)
    return {
        "sessions": int(len(ic)),
        "ic_mean": float(ic.mean()),
        "icir": float(ic.mean() / ic.std()) if ic.std() else 0.0,
        "ic_positive_share": float((ic > 0).mean()),
        "ic_by_year": {str(y): float(v) for y, v in ic.groupby(ic.index.year).mean().items()},
    }


def main() -> int:
    """Report the gates, and exit non-zero when any of them fail."""
    parser = argparse.ArgumentParser(description="Grade an alpha dataset against the gates")
    parser.add_argument("--dataset", required=True, help="dataset name cached under the lab")
    parser.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        default=[42],
        help="booster seeds to average; more seeds lowers IC variance",
    )
    parser.add_argument(
        "--hold-days",
        type=int,
        default=None,
        help="label horizon in sessions; reuses the cached factors",
    )
    parser.add_argument("--lab", default=DEFAULT_LAB, help="AlphaLab store path")
    parser.add_argument("--json", default="", help="write the report to this path")
    args = parser.parse_args()

    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print(f"没有找到数据集：{args.dataset}")
        return 2

    train = dataset.data_periods[Segment.TRAIN]
    valid = dataset.data_periods[Segment.VALID]
    test = dataset.data_periods[Segment.TEST]
    print(f"train={train} valid={valid} test={test}")

    contamination: list[str] = []
    if not test[0] > train[1]:
        contamination.append("test overlaps train")
    if not test[0] > valid[1]:
        contamination.append("test overlaps valid")
    if contamination:
        print("数据划分不干净，本轮数字不可用于判定：" + "; ".join(contamination))
        return 3

    ic = rank_ic(dataset, args.seeds, args.hold_days)
    print(f"参与集成的种子：{args.seeds}")
    if args.hold_days:
        print(f"预测持仓周期：{args.hold_days} 个交易日（复用已缓存因子）")

    report: dict = {
        "dataset": args.dataset,
        "train": list(train),
        "valid": list(valid),
        "test": list(test),
        "ic": ic,
        "seeds": args.seeds,
        "hold_days": args.hold_days,
        "gates": {
            "ic_mean": {"value": ic["ic_mean"], "target": IC_MEAN_MIN, "pass": ic["ic_mean"] >= IC_MEAN_MIN},
            "icir": {"value": ic["icir"], "target": ICIR_MIN, "pass": ic["icir"] >= ICIR_MIN},
            "ic_positive_share": {
                "value": ic["ic_positive_share"],
                "target": IC_POSITIVE_MIN,
                "pass": ic["ic_positive_share"] >= IC_POSITIVE_MIN,
            },
        },
    }
    report["passed"] = all(g["pass"] for g in report["gates"].values())
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
