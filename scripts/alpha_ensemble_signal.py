"""Refit the rolling signal over several seeds and cache the average.

One booster carries a variance that has nothing to do with the market:
different seeds land on different trees, so the daily IC swings more than
the edge itself does. The single-seed size signal reads an ICIR of 0.518
against a gate of 0.5, and only four of its seven rolling six-month
windows clear that gate on their own. A margin that thin is not a margin,
it is a window.

The fix is not a better hyperparameter, it is averaging the variance out.
Several seeds are fitted on the same training window and their predictions
are averaged, which keeps the signal they agree on and cancels the part
that belongs to the seed rather than to the edge. This writes the average
to a parquet the backtest runner can read, so the engine grades the same
signal the gate scored.

Usage::

    python scripts/alpha_ensemble_signal.py --seeds 42 7 2024
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


REPO: Path = Path(__file__).resolve().parents[1]
DEFAULT_LAB: str = "D:/Codex/work/vnpy-alpha-cache/alpha_lab"
DEFAULT_OUT: str = (
    "D:/Codex/work/vnpy-alpha-cache/sweeps/"
    "market3000_size_rolling900_250_ens.parquet"
)


def load_gate():
    """Import alpha_gate without running its CLI."""
    spec = importlib.util.spec_from_file_location(
        "alpha_gate", str(REPO / "scripts" / "alpha_gate.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    """Refit over the seeds and cache the average."""
    parser = argparse.ArgumentParser(description="Multi-seed rolling signal")
    parser.add_argument("--lab", default=DEFAULT_LAB)
    parser.add_argument("--dataset", default="market3000_size")
    parser.add_argument("--seeds", type=int, nargs="+", default=[42, 7, 2024])
    parser.add_argument("--train-window", type=int, default=900)
    parser.add_argument("--step", type=int, default=250)
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab

    gate = load_gate()
    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print(f"没有找到数据集：{args.dataset}")
        return 2

    print(
        f"数据集 {args.dataset}: train={dataset.data_periods} "
        f"seeds={args.seeds} window={args.train_window} step={args.step}"
    )

    scored = gate.predict_rolling(
        dataset,
        args.seeds,
        args.train_window,
        args.step,
    )
    if scored is None or scored.is_empty():
        print("滚动预测为空")
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    scored.write_parquet(out)
    print(f"写入 {out} 行数={scored.height}")

    report = gate.summarise_ic(scored, dataset)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
