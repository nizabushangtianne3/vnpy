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
