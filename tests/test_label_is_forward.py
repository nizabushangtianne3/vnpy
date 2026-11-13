"""The label has to be a forward return, or nothing downstream means anything.

``ts_delay(close, -n) / ts_delay(close, -1) - 1`` reads like a ratio of two
backward shifts, and while this was being built that reading was taken
seriously enough to suspect the label carried no forward information at all.
The negative window in ``ts_delay`` moves the series forward, not back, so the
expression is the return from the next tradeable session to n sessions later -
which is exactly the window a T+1 market lets a book capture.

Both facts are checked here: the literal expression against a hand-computed
return, and the cached dataset's label against the realised forward return, so
the next person does not have to re-derive it.
"""

import os
from pathlib import Path

import polars as pl
import pytest

from vnpy.alpha.dataset.utility import calculate_by_expression


DEFAULT_LAB: str = os.environ.get(
    "VNPY_ALPHA_LAB", "D:/Codex/work/vnpy-alpha-cache/alpha_lab"
)


def _frame(closes: list[float]) -> pl.DataFrame:
    """One symbol, consecutive sessions, nothing else."""
    return pl.DataFrame(
        {
            "datetime": [f"2024-01-{day:02d}" for day in range(2, 2 + len(closes))],
            "vt_symbol": ["A.SSE"] * len(closes),
            "close": closes,
        }
    )


def test_negative_window_is_a_forward_shift() -> None:
    """``ts_delay(close, -2)`` is two sessions ahead, not two behind."""
    df: pl.DataFrame = _frame([10.0, 11.0, 12.0, 13.0, 14.0, 13.0])

    shifted = calculate_by_expression(df, "ts_delay(close, -2)")["data"].to_list()

    # Each row reads the close two sessions ahead of its own.
    assert shifted == [12.0, 13.0, 14.0, 13.0, None, None]
    # The final two rows have nothing two sessions ahead to land on.
    assert shifted[4] is None
    assert shifted[5] is None


def test_label_expression_is_the_forward_return() -> None:
    """The label equals the return from the next session to n later."""
    df: pl.DataFrame = _frame([10.0, 11.0, 12.0, 13.0, 14.0, 13.0])

    label = calculate_by_expression(
        df, "ts_delay(close, -2) / ts_delay(close, -1) - 1"
    )["data"].to_list()

    # The denominator is one session ahead, so the horizon widens the window
    # # by a single bar: row 0 compares close at +2 against close at +1.
    assert label[0] == 12.0 / 11.0 - 1
    assert label[1] == 13.0 / 12.0 - 1


def test_label_on_the_real_dataset_tracks_the_forward_return() -> None:
    """The cached dataset's label is the forward return it claims to be."""
    dataset_dir: Path = Path(DEFAULT_LAB) / "dataset"
    if not dataset_dir.is_dir():
        pytest.skip("no cached alpha lab on this machine")

    from vnpy.alpha import AlphaLab

    lab = AlphaLab(DEFAULT_LAB)
    dataset = lab.load_dataset("m800clean")
    if dataset is None:
        pytest.skip("m800clean is not cached")

    result = dataset.result_df.select(
        ["datetime", "vt_symbol", "close", "label"]
    ).sort(["vt_symbol", "datetime"])

    realised = result.with_columns(
        (pl.col("close").shift(-5).over("vt_symbol") / pl.col("close") - 1).alias("fwd")
    )

    both = realised.filter(pl.col("label").is_not_null() & pl.col("fwd").is_not_null())
    if both.height < 1000:
        pytest.skip("not enough rows to correlate")

    correlation: float = both.select(pl.corr("label", "fwd")).item()

    assert correlation > 0.85
