from __future__ import annotations

from vnpy.autonomous.__main__ import (
    MIN_WALK_FORWARD_FOLDS,
    _walk_forward_objections,
)


def _fold(pnl: float) -> dict:
    """One walk-forward fold row with just the field the verdict reads."""
    return {"test_pnl": pnl}


def test_one_profitable_fold_is_not_proven_edge() -> None:
    """The single fold that scored +1235 must not read as an edge.

    The reference strategy produced exactly this: a +1235 fold and nothing
    else. A positive total over one regime is luck-sized evidence, and the
    verdict has to say so rather than promote it.
    """
    objections = _walk_forward_objections([_fold(1235.98)], 1235.98)

    assert objections, "a single profitable fold was accepted as an edge"
    assert any("折叠数" in reason for reason in objections)


def test_a_negative_total_is_objected_to_regardless_of_folds() -> None:
    """Three folds with a negative total does not become an edge by volume."""
    objections = _walk_forward_objections(
        [_fold(-100.0), _fold(50.0), _fold(-80.0)], -130.0
    )

    assert objections
    assert any("合计" in reason for reason in objections)


def test_a_majority_of_profitable_folds_over_enough_folds_clears() -> None:
    """Three folds with a positive total and a profitable majority is an edge."""
    objections = _walk_forward_objections(
        [_fold(100.0), _fold(20.0), _fold(-5.0)], 115.0
    )

    assert objections == []


def test_a_tie_in_folds_is_not_a_majority() -> None:
    """Two profitable folds out of three is not a majority of a three-fold run."""
    objections = _walk_forward_objections(
        [_fold(100.0), _fold(-40.0), _fold(-5.0)], 55.0
    )

    assert any("折叠" in reason for reason in objections)


def test_the_fold_minimum_is_at_least_two() -> None:
    """A one-fold minimum would defeat the whole point of the check."""
    assert MIN_WALK_FORWARD_FOLDS >= 2
