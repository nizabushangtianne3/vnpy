"""Tests for the portfolio probe's market attribution.

A long-only book and a rank IC disagree in exactly one situation: the market
moved. The ranking is invariant to a shift of every label by the same amount,
the book is not, so on a session where the market fell a book can lose money
while its IC is at its best. The attribution has to tell those two apart, and
a neutral slice has to keep the book's ordering value while dropping the
market's. Both are pinned here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest


REPO: Path = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "alpha_portfolio_probe", str(REPO / "scripts" / "alpha_portfolio_probe.py")
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_beta_attribution_charges_the_market_for_its_own_direction():
    """A book that tracks the market one-for-one must keep no alpha.

    The point of the split is that a book earning the market's own return is
    not a strategy. When the book is exactly the market, the slope is one and
    the intercept is zero, and that is the case the numbers have to land on
    regardless of how large the market's move was.
    """
    paired = pl.DataFrame(
        {
            "datetime": ["2024-01-02", "2024-01-03", "2024-01-04"],
            # The market swings hard; the book is the market.
            "market": [-0.05, 0.01, 0.05],
            "book": [-0.05, 0.01, 0.05],
        }
    )
    out = probe.market_beta(paired)
    assert out["beta"] == pytest.approx(1.0)
    assert out["alpha"] == pytest.approx(0.0)
    assert out["corr"] == pytest.approx(1.0)


def test_beta_attribution_survives_a_falling_market_with_a_good_ranking():
    """Ordering that holds up must separate from a market that does not.

    This is the 2026 case. The market's mean forward return is negative every
    day and the book earns a fixed amount over it, so the book's mean is
    whatever the market paid. The alpha has to come out as the constant the
    signal added, not as the book's raw mean.
    """
    market = [-0.02, -0.01, -0.03, -0.005]
    book = [market[i] + 0.004 for i in range(len(market))]
    paired = pl.DataFrame(
        {
            "datetime": [f"2026-01-0{i + 2}" for i in range(len(market))],
            "market": market,
            "book": book,
        }
    )
    out = probe.market_beta(paired)
    # The book's raw mean is negative and would look like a losing strategy.
    assert out["book"] < 0
    assert out["market"] < 0
    # The signal's own contribution is the constant that was added to it.
    assert out["alpha"] == pytest.approx(0.004)


def test_beta_attribution_needs_at_least_three_sessions():
    """Fewer than three days cannot fit a line, so it declines rather than invents one."""
    paired = pl.DataFrame(
        {
            "datetime": ["2024-01-02", "2024-01-03"],
            "market": [0.0, 0.0],
            "book": [0.0, 0.0],
        }
    )
    assert probe.market_beta(paired) == {}


def test_neutral_top_slice_is_unchanged_by_a_uniform_market_move():
    """Adding the same return to every name must not move the neutral book.

    The neutral slice exists precisely because a book that is paid for the
    market's direction is not being paid for its selection. If shifting every
    label up or down changed the neutral result, it would still be carrying
    the market and could not be used to grade the signal.
    """
    scored = pl.DataFrame(
        {
            "datetime": ["2024-01-02"] * 4,
            # signal 1 and 2 are the strongest calls.
            "signal_rank": [1, 2, 3, 4],
            "fwd": [0.010, 0.006, 0.002, 0.000],
        }
    )
    base = probe.neutral_top_slice(scored, top_k=2)
    # Mean fwd over all four names is 0.0045, so the top two names sit at
    # 0.010 - 0.0045 = 0.0055 and 0.006 - 0.0045 = 0.0015, averaging 0.0035.
    assert base["rel"][0] == pytest.approx(0.0035)

    for shift in (-0.05, 0.05):
        moved = scored.with_columns((pl.col("fwd") + shift).alias("fwd"))
        shifted = probe.neutral_top_slice(moved, top_k=2)
        assert shifted["rel"][0] == pytest.approx(0.0035)


def test_neutral_top_slice_keeps_the_ordering_of_the_selection():
    """A better selection has to show up as a larger neutral return.

    The neutral number is not a relabelling of the absolute one; the strongest
    names must hold their advantage once the market is removed, or the slice
    is measuring the market and not the ranking.
    """
    weak = pl.DataFrame(
        {
            "datetime": ["2024-01-02"] * 4,
            "signal_rank": [1, 2, 3, 4],
            "fwd": [0.005, 0.004, 0.003, 0.002],
        }
    )
    strong = pl.DataFrame(
        {
            "datetime": ["2024-01-02"] * 4,
            "signal_rank": [1, 2, 3, 4],
            "fwd": [0.030, 0.020, 0.003, 0.002],
        }
    )
    assert (
        probe.neutral_top_slice(strong, top_k=2)["rel"][0]
        > probe.neutral_top_slice(weak, top_k=2)["rel"][0]
    )
