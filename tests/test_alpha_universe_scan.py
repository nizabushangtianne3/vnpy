"""Tests for the market-slice scan that grades the alpha signal.

The scan has to survive two kinds of mistake: reading the future when it
slices the market, and picking the wrong end of the signal when it keeps a
slice of it. Both are silent - the script still prints numbers - so they are
pinned here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "alpha_universe_scan", str(REPO / "scripts" / "alpha_universe_scan.py")
)
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


def test_as_of_turnover_uses_only_earlier_sessions():
    """The trailing window must end before the session it labels."""
    bars = pl.DataFrame(
        {
            "code": ["A"] * 4,
            "datetime": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
            "amount": [1.0, 2.0, 99.0, 4.0],
        }
    )
    out = scan.as_of_turnover(bars, window=2, quantiles=2)
    row = out.filter(
        (pl.col("code") == "A") & (pl.col("datetime") == "2024-01-03")
    )
    # mean(1, 2) = 1.5; the 99 on the scored session itself must not be in it.
    assert row["trailing_amount"][0] == pytest.approx(1.5)


def test_as_of_turnover_never_uses_the_scored_session():
    """Changing a symbol's own bar must not move its liquidity rank.

    The bucket is a cross-sectional rank, so one symbol's turnover can move
    another symbol's bucket - that is inherent to a quantile cut and is not
    lookahead. What must not happen is a symbol's own bucket depending on
    its own turnover on the session being scored.
    """
    base = pl.DataFrame(
        {
            "code": ["A"] * 4 + ["B"] * 4,
            "datetime": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 2,
            "amount": [1.0, 2.0, 3.0, 4.0, 4.0, 3.0, 2.0, 1.0],
        }
    )
    changed = base.with_columns(
        pl.when(pl.col("datetime") == "2024-01-03")
        .then(pl.col("amount") * 1000.0)
        .otherwise(pl.col("amount"))
        .alias("amount")
    )
    first = scan.as_of_turnover(base, window=2, quantiles=2)
    second = scan.as_of_turnover(changed, window=2, quantiles=2)
    key = ["code", "datetime"]
    assert first.select(key + ["bucket"]).join(
        second.select(key + ["bucket"]), on=key, how="inner"
    ).height == first.height
    joined = first.select(key + ["bucket"]).join(
        second.select(key + ["bucket"]),
        on=key,
        how="inner",
        suffix="_changed",
    )
    # On the changed session, code A's own turnover moved by a factor of 1000.
    # Its bucket must not have followed.
    same_day = joined.filter(pl.col("datetime") == "2024-01-03")
    assert same_day.filter(pl.col("bucket") != pl.col("bucket_changed")).is_empty()


def test_keep_top_by_keeps_the_strongest_calls():
    """The slice must keep the model's best calls, not its worst."""
    frame = pl.DataFrame(
        {
            "datetime": ["2024-01-03"] * 4,
            "code": ["A", "B", "C", "D"],
            "signal": [0.1, 0.9, 0.5, 0.7],
            "label": [0.0, 1.0, 0.0, 1.0],
            "bucket": [0, 0, 1, 1],
        }
    )
    picked = scan.keep_top_by(frame, 0.5, by=["datetime", "bucket"])
    assert set(picked["code"]) == {"B", "D"}


def test_keep_top_by_without_grouping_uses_the_whole_cross_section():
    frame = pl.DataFrame(
        {
            "datetime": ["2024-01-03"] * 4,
            "code": ["A", "B", "C", "D"],
            "signal": [0.1, 0.9, 0.5, 0.7],
            "label": [0.0, 1.0, 0.0, 1.0],
            "bucket": [0, 0, 1, 1],
        }
    )
    picked = scan.keep_top_by(frame, 0.5)
    assert set(picked["code"]) == {"B", "D"}


def test_date_text_strips_the_time_part():
    """A datetime and a date string have to meet as the same key."""
    import datetime as dt

    series = pl.Series("datetime", [dt.datetime(2024, 1, 3, 0, 0, 0)])
    assert scan.as_date_text(series)[0] == "2024-01-03"


def test_daily_ic_ignores_sessions_with_one_name():
    frame = pl.DataFrame(
        {
            "datetime": ["2024-01-03", "2024-01-04", "2024-01-04"],
            "signal": [1.0, 1.0, 2.0],
            "label": [1.0, 1.0, 2.0],
        }
    )
    ic = scan.daily_ic(frame)
    assert len(ic) == 1


def test_signal_cache_path_finds_the_file_the_fit_writes():
    """The cache lookup must land on the key, not on the caller's filename.

    The default signal path has no seed suffix in its name while the key
    always carries one. A lookup that matched the caller's stem against the
    key would never find the cache, and every scan would refit a model it
    already had on disk.
    """
    path = scan.signal_cache_path(
        "D:/cache/market3000_rolling900_250.parquet",
        "market3000",
        900,
        250,
        0,
        [42],
    )
    assert path.name == "market3000_rolling900_250_s42.parquet"
    assert path.parent.name == "cache"


def test_signal_cache_path_separates_seeds_and_horizons():
    """A signal fitted with one seed or horizon is not another one's."""
    one_seed = scan.signal_cache_path(
        "D:/cache/x.parquet", "market3000", 900, 250, 0, [42]
    )
    three_seeds = scan.signal_cache_path(
        "D:/cache/x.parquet", "market3000", 900, 250, 0, [42, 1, 2]
    )
    ten_days = scan.signal_cache_path(
        "D:/cache/x.parquet", "market3000", 900, 250, 10, [42]
    )
    assert one_seed != three_seeds
    assert one_seed != ten_days
    assert "s42-1-2" in three_seeds.name
    assert "_h10" in ten_days.name


def test_cached_signal_carries_its_label_in_the_file_metadata(tmp_path):
    """The label must be readable from the cache, or every scan refits.

    The label is written into the parquet's custom metadata. Reading it
    through DataFrame.schema instead returns only the column types, the
    comparison against the dataset's label then fails on an empty string,
    and the scan refits a model it already had on disk.
    """
    path = tmp_path / "signal.parquet"
    frame = pl.DataFrame(
        {
            "datetime": ["2024-01-03"],
            "vt_symbol": ["000001.SZSE"],
            "signal": [0.5],
        }
    )
    label = "ts_delay(close, -5) / ts_delay(close, -1) - 1"
    frame.write_parquet(path, metadata={"label_expression": label})

    metadata = dict(pl.read_parquet_metadata(path))
    assert str(metadata.get("label_expression", "")) == label

    # The wrong reader finds nothing, which is why the code above has to use
    # the metadata reader.
    assert "label_expression" not in dict(pl.read_parquet(path).schema)
