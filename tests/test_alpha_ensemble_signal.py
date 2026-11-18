"""The ensemble CLI must train and score the same label horizon."""

import importlib.util
import sys
import types
from pathlib import Path

import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
SCRIPT: Path = REPO / "scripts" / "alpha_ensemble_signal.py"


def _load_script():
    """Import the CLI without invoking main()."""
    spec = importlib.util.spec_from_file_location("alpha_ensemble_signal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDataset:
    """Just enough of AlphaLab's dataset to prove relabel is called first."""

    def __init__(self) -> None:
        rows = {
            "datetime": ["2024-01-02", "2024-01-03"],
            "vt_symbol": ["A.SSE", "A.SSE"],
            "close": [10.0, 11.0],
            "label": [0.0, 0.0],
        }
        frame = pl.DataFrame(rows)
        self.result_df = frame
        self.raw_df = frame
        self.infer_df = frame
        self.learn_df = frame
        self.label_expression = "old"
        self.data_periods = {"train": "2020", "valid": "2023", "test": "2024"}


def test_default_output_is_horizon_scoped() -> None:
    """An h=10 refit must never overwrite the h=5 ensemble cache."""
    script = _load_script()

    assert script.default_output("market3000_size", 900, 250, 5) == script.DEFAULT_OUT
    assert script.default_output("market3000_size", 900, 250, 10).endswith(
        "market3000_size_rolling900_250_h10_ens.parquet"
    )
    assert script.default_output("market3000", 900, 250, 5).endswith(
        "market3000_rolling900_250_h5_ens.parquet"
    )


def test_main_relabels_before_rolling(monkeypatch, tmp_path: Path) -> None:
    """A --hold-days refit cannot silently train on the cached 5-day label."""
    script = _load_script()
    dataset = FakeDataset()
    calls: list[tuple[str, int]] = []

    class FakeLab:
        def __init__(self, lab: str) -> None:
            assert lab.endswith("alpha_lab")

        def load_dataset(self, name: str):
            assert name == "market3000_size"
            return dataset

    def fake_relabel(candidate, hold_days: int) -> None:
        candidate.label_expression = f"hold={hold_days}"
        calls.append(("relabel", hold_days))

    def fake_predict_rolling(candidate, seeds, window, step, model_kwargs=None):
        calls.append(("rolling", step))
        return candidate.infer_df.select(["datetime", "vt_symbol"]).with_columns(
            pl.Series("signal", [1.0, -1.0])
        )

    gate = types.SimpleNamespace(
        relabel=fake_relabel,
        predict_rolling=fake_predict_rolling,
        summarise_ic=lambda scored, candidate: {"sessions": 1},
    )
    monkeypatch.setattr(script, "load_gate", lambda: gate)
    fake_alpha = types.ModuleType("vnpy.alpha")
    fake_alpha.AlphaLab = FakeLab
    monkeypatch.setitem(sys.modules, "vnpy.alpha", fake_alpha)
    argv = [
        "alpha_ensemble_signal.py",
        "--hold-days",
        "10",
        "--out",
        str(tmp_path / "fake.parquet"),
    ]
    monkeypatch.setattr("sys.argv", argv)

    assert script.main() == 0
    assert calls == [("relabel", 10), ("rolling", 250)]
    assert dataset.label_expression == "hold=10"
