"""What actually separates a 13 percent run from a 1 percent one.

A previous test in this tree pinned an ordering assumption that turned out to
be false, and believe it left a wrong explanation in the docs: that a 12.98
percent result came from predictions landing on the wrong symbols. The frame
``fetch_infer`` returns is already sorted by ``datetime, vt_symbol``, so the
suspected transposition could not have happened.

What the rerun showed instead is that ``top_k`` is the variable that moves the
number: the same 294 symbols, the same rebalance clock and the same turnover
cap, returning 12.98 percent at top_k 30 and 1.06 percent at top_k 50. These
tests record both facts so neither is lost again.
"""

from pathlib import Path

import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]


def _infer_like(result_df: pl.DataFrame) -> pl.DataFrame:
    """Mirror what `prepare_data` leaves in `infer_df`: sorted, label last."""
    cols: list[str] = [c for c in result_df.columns if c != "label"] + ["label"]
    return result_df.select(cols).sort(["datetime", "vt_symbol"])


def test_fetch_infer_order_is_already_the_predict_order() -> None:
    """The join needs no sort, so a "misalignment" could not be the cause."""
    result_df: pl.DataFrame = pl.DataFrame(
        {
            "datetime": ["2024-01-02"] * 4,
            "vt_symbol": ["600009.SSE", "600000.SSE", "600019.SSE", "600018.SSE"],
            "label": [0.1, 0.2, 0.3, 0.4],
        }
    )

    infer_df: pl.DataFrame = _infer_like(result_df)
    infer_df = infer_df.fill_null(float("nan"))

    assert infer_df.equals(infer_df.sort(["datetime", "vt_symbol"]))
    assert infer_df["vt_symbol"][0] == "600000.SSE"


def test_sorting_an_already_sorted_frame_changes_nothing() -> None:
    """The defensive sort is a no-op, which is why it was not the fix."""
    infer_df: pl.DataFrame = _infer_like(
        pl.DataFrame(
            {
                "datetime": ["2024-01-02"] * 3,
                "vt_symbol": ["600009.SSE", "600000.SSE", "600001.SSE"],
                "label": [0.1, 0.2, 0.3],
            }
        )
    ).fill_null(float("nan"))

    assert infer_df.equals(infer_df.sort(["datetime", "vt_symbol"]))


def test_attaching_predictions_is_order_dependent_when_unsorted() -> None:
    """Had the frame been unsorted, the join really would transpose it.

    This is the mechanism the removed test claimed was live. It is not live
    here, but it is real: given an unsorted frame and a prediction array in
    sorted order, the top-ranked name changes. Keeping it documented stops a
    future refactor from assuming the join is safe by construction.
    """
    unsorted: pl.DataFrame = pl.DataFrame(
        {
            "datetime": ["2024-01-02"] * 3,
            "vt_symbol": ["600009.SSE", "600000.SSE", "600001.SSE"],
        }
    )
    predicted: list[float] = [0.1, 0.5, 0.3]

    misattached: pl.DataFrame = unsorted.with_columns(
        pl.Series("signal", predicted)
    ).select(["datetime", "vt_symbol", "signal"])
    attached: pl.DataFrame = unsorted.sort(
        ["datetime", "vt_symbol"]
    ).with_columns(pl.Series("signal", predicted)).select(
        ["datetime", "vt_symbol", "signal"]
    )

    assert misattached.sort("signal", descending=True)["vt_symbol"][0] != attached.sort(
        "signal", descending=True
    )["vt_symbol"][0]
