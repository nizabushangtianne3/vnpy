"""Guard the order predictions are attached in.

``LgbModel.predict`` sorts the inference frame by ``datetime, vt_symbol``
before building its input matrix, so the returned array is ordered by that
sort. A caller that attaches it to an unsorted frame hands each prediction to
a neighbour: on the pilot sample the first unsorted row is ``600009.SSE`` and
the first sorted row is ``600000.SSE``, so every name is ranked on somebody
else's signal. That is silent, and it is what turned a 1 percent book into a
13 percent one on paper, so the pilot builds its signal by sorting first and
this test pins the shape of that job.
"""

import polars as pl


def attach_signal(infer: pl.DataFrame, predicted: list[float]) -> pl.DataFrame:
    """Mirror the pilot's own join: sort, then attach, then select."""
    infer = infer.sort(["datetime", "vt_symbol"])
    return infer.with_columns(pl.Series("signal", predicted)).select(
        ["datetime", "vt_symbol", "signal"]
    )


def test_signal_follows_the_sorted_order() -> None:
    """The highest score must land on the row that produced it."""
    infer: pl.DataFrame = pl.DataFrame(
        {
            "datetime": ["2024-01-02"] * 3,
            "vt_symbol": ["600009.SSE", "600000.SSE", "600001.SSE"],
            "close": [10.0, 20.0, 30.0],
        }
    )
    # Predictions in sorted order: 600000 first, 600001 last.
    predicted: list[float] = [0.1, 0.5, 0.3]

    signal: pl.DataFrame = attach_signal(infer, predicted)

    ranked: list[str] = signal.sort("signal", descending=True)["vt_symbol"].to_list()
    # 0.5 lands on 600001, 0.3 on 600009, 0.1 on 600000.
    assert ranked == ["600001.SSE", "600009.SSE", "600000.SSE"]
    assert signal.height == 3


def test_unsorted_join_would_swap_two_names() -> None:
    """Show the failure mode the sort removes."""
    infer: pl.DataFrame = pl.DataFrame(
        {
            "datetime": ["2024-01-02"] * 3,
            "vt_symbol": ["600009.SSE", "600000.SSE", "600001.SSE"],
            "close": [10.0, 20.0, 30.0],
        }
    )
    predicted: list[float] = [0.1, 0.5, 0.3]

    unsorted: pl.DataFrame = infer.with_columns(
        pl.Series("signal", predicted)
    ).select(["datetime", "vt_symbol", "signal"])

    top_unsorted: str = unsorted.sort("signal", descending=True)["vt_symbol"][0]
    top_aligned: str = attach_signal(infer, predicted).sort(
        "signal", descending=True
    )["vt_symbol"][0]

    assert top_unsorted != top_aligned
    assert top_aligned == "600001.SSE"
