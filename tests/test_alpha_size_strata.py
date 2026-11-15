from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl


REPO = Path(__file__).resolve().parents[1]


def load_script():
    spec = importlib.util.spec_from_file_location(
        "alpha_size_strata", str(REPO / "scripts" / "alpha_size_strata.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_size_buckets_are_cut_on_the_day_s_own_cross_section() -> None:
    """The bucket must depend only on what was known on that session."""
    script = load_script()
    frame = pl.DataFrame(
        {
            "code": ["a", "b", "c", "d"] * 2,
            "datetime": ["2024-01-02"] * 4 + ["2024-01-03"] * 4,
            "trailing_amount": [10.0, 30.0, 20.0, 40.0] * 2,
        }
    )
    out = script.size_buckets(frame, 2)
    assert out.schema["size_bucket"] == pl.Int64
    # On each session the largest turnover is bucket 0 and the smallest is
    # bucket 1, regardless of what the other session looked like.
    for date in ("2024-01-02", "2024-01-03"):
        day = out.filter(pl.col("datetime") == date)
        assert day.filter(pl.col("code") == "d")["size_bucket"][0] == 0
        assert day.filter(pl.col("code") == "a")["size_bucket"][0] == 1
        assert day.filter(pl.col("code") == "b")["size_bucket"][0] == 0
        assert day.filter(pl.col("code") == "c")["size_bucket"][0] == 1


def test_size_buckets_keep_the_bucket_sizes_stable() -> None:
    """Every bucket must hold the same share of the cross-section."""
    script = load_script()
    frame = pl.DataFrame(
        {
            "code": [f"s{i}" for i in range(30)],
            "datetime": ["2024-01-02"] * 30,
            "trailing_amount": [float(i) for i in range(30)],
        }
    )
    out = script.size_buckets(frame, 3)
    counts = out.group_by("size_bucket").agg(pl.len().alias("n")).sort("size_bucket")
    assert counts["n"].to_list() == [10, 10, 10]


def test_size_bucket_order_is_descending_by_turnover() -> None:
    """Bucket 0 must be the largest stratum, not the smallest."""
    script = load_script()
    frame = pl.DataFrame(
        {
            "code": ["small", "big"],
            "datetime": ["2024-01-02", "2024-01-02"],
            "trailing_amount": [1.0, 1_000_000.0],
        }
    )
    out = script.size_buckets(frame, 2)
    assert out.filter(pl.col("code") == "big")["size_bucket"][0] == 0
    assert out.filter(pl.col("code") == "small")["size_bucket"][0] == 1


def test_size_buckets_do_not_reach_a_future_session() -> None:
    """A name's bucket must not be decided by a later session's turnover."""
    script = load_script()
    frame = pl.DataFrame(
        {
            "code": ["a", "b"],
            "datetime": ["2024-01-02", "2024-01-03"],
            "trailing_amount": [1.0, 1_000_000.0],
        }
    )
    out = script.size_buckets(frame, 2)
    first = out.filter(pl.col("datetime") == "2024-01-02")
    # Only one name traded on the first session, so it is the whole
    # cross-section there and cannot be moved by the next session.
    assert first.height == 1
    assert first["size_bucket"][0] == 0


def test_size_buckets_carry_the_turnover_through() -> None:
    """The turnover column must survive, so a report can name the stratum."""
    script = load_script()
    frame = pl.DataFrame(
        {
            "code": ["a"],
            "datetime": ["2024-01-02"],
            "trailing_amount": [42.0],
        }
    )
    out = script.size_buckets(frame, 3)
    assert out["trailing_amount"][0] == 42.0
    assert set(out.columns) == {"code", "datetime", "trailing_amount", "size_bucket"}
