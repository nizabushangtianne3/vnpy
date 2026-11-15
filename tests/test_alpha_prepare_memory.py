"""What the market-sized build is allowed to cost in memory.

A full-market run of ``prepare_data`` dies in the OS allocator rather than in
arithmetic. The frame holds 3.4M rows and 165 columns, and the old code paid for
that shape four extra times: a factor list that was never released, a
``fill_null`` over rows the component filter was about to drop, one materialised
frame per span, and a ``concat`` and ``sort`` on top of them. Each of those is
invisible on a 294-symbol sample and fatal on 3021.

These tests pin the behaviour that keeps the peak down without changing a value:
the surviving rows, their order, their nulls and the columns they carry all have
to look exactly as they did before.
"""

from datetime import datetime

import polars as pl

from vnpy.alpha.dataset import AlphaDataset


D1 = datetime(2024, 1, 2)
D2 = datetime(2024, 1, 3)
D3 = datetime(2024, 1, 4)


def _frame() -> pl.DataFrame:
    """Three bars over two symbols, with the close column the label reads."""
    return pl.DataFrame(
        {
            "datetime": [D1, D1, D2, D2, D3, D3],
            "vt_symbol": ["A.SSE", "B.SSE", "A.SSE", "B.SSE", "A.SSE", "B.SSE"],
            "close": [10.0, 20.0, 11.0, 21.0, 12.0, 22.0],
        }
    )


def _dataset() -> AlphaDataset:
    dataset: AlphaDataset = AlphaDataset(
        _frame(),
        train_period=("2024-01-02", "2024-01-03"),
        valid_period=("2024-01-03", "2024-01-04"),
        test_period=("2024-01-04", "2024-01-04"),
    )
    # A factor with a null and a label, so both the fill and the label's
    # position survive the stages under test.
    dataset.add_feature("roc", "close / ts_delay(close, 1) - 1")
    dataset.set_label("ts_delay(close, -1) / close - 1")
    return dataset


def test_filter_keeps_exactly_the_rows_the_spans_name() -> None:
    """One gather must select the same rows the per-symbol union selected."""
    dataset: AlphaDataset = _dataset()
    # A is a member for the whole window, B only from D2, so B's D1 row must go.
    dataset.prepare_data(
        {
            "A.SSE": [(D1, D3)],
            "B.SSE": [(D2, D3)],
        },
        max_workers=1,
    )
    dataset.process_data()

    df: pl.DataFrame = dataset.raw_df.select(["vt_symbol", "datetime"]).sort("vt_symbol")
    assert df["vt_symbol"].to_list() == ["A.SSE", "A.SSE", "A.SSE", "B.SSE", "B.SSE"]
    assert df["datetime"].to_list() == [D1, D2, D3, D2, D3]


def test_filter_result_is_sorted_by_datetime_then_symbol() -> None:
    """The gather replaces a concat, so the order it produced must be explicit."""
    dataset: AlphaDataset = _dataset()
    dataset.prepare_data({"A.SSE": [(D1, D3)]}, max_workers=1)
    dataset.process_data()

    df: pl.DataFrame = dataset.raw_df
    assert df.equals(df.sort(["datetime", "vt_symbol"]))


def test_nulls_are_filled_on_the_rows_that_survive() -> None:
    """The factor is null on each symbol's first bar, and must stay NaN there."""
    dataset: AlphaDataset = _dataset()
    dataset.prepare_data({"B.SSE": [(D1, D3)]}, max_workers=1)
    dataset.process_data()

    col: pl.Series = dataset.raw_df["roc"]
    first = col[0]
    assert first is None or first != first


def test_factors_are_stored_as_float32() -> None:
    """The booster reads 32 bits, so the frame must not carry 64."""
    dataset: AlphaDataset = _dataset()
    dataset.prepare_data({"A.SSE": [(D1, D3)]}, max_workers=1)

    assert dataset.raw_df["roc"].dtype == pl.Float32


def test_label_stays_last_and_close_is_kept_for_rehorizoning() -> None:
    """LgbModel reads columns[2:-1] and relabel reads close, so neither moves."""
    dataset: AlphaDataset = _dataset()
    dataset.prepare_data({"A.SSE": [(D1, D3)]}, max_workers=1)
    dataset.process_data()

    df: pl.DataFrame = dataset.result_df
    assert df.columns[-1] == "label"
    assert "close" in df.columns
