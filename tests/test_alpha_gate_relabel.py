"""Rehorizoning must rewrite the label everywhere it is read from.

``alpha_gate.py --hold-days`` is what makes a horizon scan cheap: the 158
factors stay cached and only the label is recomputed. That only works because
the label is a column on several frames at once - ``result_df`` keeps the raw
``close`` the expression reads, ``LgbModel`` trains off ``learn_df``, and
``fetch_infer`` scores off ``infer_df``. Rewriting one and not the rest would
fit the model on one horizon and score it on another, which is the kind of bug
that reports a plausible IC for a signal that never existed.
"""

import importlib.util
from pathlib import Path

import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
GATE: Path = REPO / "scripts" / "alpha_gate.py"


def _load_gate():
    """Import the script as a module without running its CLI."""
    spec = importlib.util.spec_from_file_location("alpha_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDataset:
    """The four frames relabel touches, with the columns they really carry."""

    def __init__(self) -> None:
        rows: dict[str, list] = {
            "datetime": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
            "vt_symbol": ["A.SSE", "A.SSE", "A.SSE", "A.SSE"],
            "close": [10.0, 11.0, 12.0, 13.0],
            "label": [0.0, 0.0, 0.0, 0.0],
        }
        self.result_df: pl.DataFrame = pl.DataFrame(rows)
        self.raw_df: pl.DataFrame = pl.DataFrame(rows)
        self.infer_df: pl.DataFrame = pl.DataFrame(rows)
        self.learn_df: pl.DataFrame = pl.DataFrame(rows)
        self.label_expression: str = "old"


def test_relabel_writes_every_frame_it_reads() -> None:
    """No frame may be left carrying the horizon the model was not fitted on."""
    gate = _load_gate()
    dataset = FakeDataset()

    # hold_days 2, because at hold_days 1 the numerator and the denominator are
    # the same bar and the label is identically zero - a horizon of one session
    # is not a horizon, it is a constant.
    gate.relabel(dataset, 2)

    for frame in [dataset.result_df, dataset.raw_df, dataset.infer_df, dataset.learn_df]:
        assert "label" in frame.columns
    # close shifted back 2 sessions over close shifted back 1, so 11/10-1
    # and then 12/11-1: the horizon widens the measurement window by one bar.
    labels = dataset.learn_df["label"].to_list()
    assert labels[0] == 0.09090909090909083
    assert labels[1] == 0.08333333333333326
    assert dataset.label_expression == "ts_delay(close, -2) / ts_delay(close, -1) - 1"


def test_relabel_replaces_rather_than_duplicates_the_column() -> None:
    """Rehorizoning twice in one process must not leave a doubled column."""
    gate = _load_gate()
    dataset = FakeDataset()

    gate.relabel(dataset, 2)
    gate.relabel(dataset, 3)

    for frame in [dataset.result_df, dataset.raw_df, dataset.infer_df, dataset.learn_df]:
        assert frame.columns.count("label") == 1
    assert dataset.learn_df["label"][0] == 0.18181818181818188


def test_relabel_keeps_the_features_and_the_close_column() -> None:
    """The cached factors are the whole point, so they must survive."""
    gate = _load_gate()
    dataset = FakeDataset()
    dataset.result_df = dataset.result_df.with_columns(
        pl.Series("roc_5", [1.0, 1.0, 1.0, 1.0])
    )

    gate.relabel(dataset, 2)

    assert "roc_5" in dataset.result_df.columns
    assert "close" in dataset.result_df.columns
