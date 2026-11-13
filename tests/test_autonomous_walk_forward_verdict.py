from __future__ import annotations

from vnpy.autonomous.promotion import PROMOTE, evaluate_candidate


def _fold(pnl: float, sharpe: float = 0.0) -> dict:
    """One fold row with just the fields the promotion gates read."""
    return {"test_pnl": pnl, "test_sharpe": sharpe}


def _payload(folds: list[dict]) -> dict:
    """The JSON shape a portfolio walk-forward run writes."""
    return {
        "folds": folds,
        "aggregate_test_pnl": sum(f["test_pnl"] for f in folds),
        "aggregate_test_sharpe": sum(f["test_sharpe"] for f in folds) / len(folds),
        "aggregate_train_sharpe": 0.0,
        "aggregate_test_round_trips": 50 * len(folds),
        "aggregate_test_win_rate": 0.5,
        "aggregate_test_profit_factor": 1.2,
    }


def test_one_profitable_fold_is_not_a_proven_edge() -> None:
    """The single fold that scored +1235 must not read as an edge.

    On 77 real A-share stocks the reference overnight strategy produced
    exactly this: a +1235 fold and nothing else, because a 600+300 window
    consumed the whole series. The same series cut into 500+200 folds lost on
    two of three, so the single profitable total was luck rather than an edge.
    """
    report = evaluate_candidate("组合", _payload([_fold(1235.98, 0.05)]))

    assert report.verdict != PROMOTE
    assert any("折叠" in reason for reason in report.reasons)


def test_a_negative_total_is_objected_to_regardless_of_fold_count() -> None:
    """Three folds with a negative total does not become an edge by volume."""
    report = evaluate_candidate(
        "组合",
        _payload([_fold(-100.0, -0.5), _fold(50.0, 0.2), _fold(-80.0, -0.05)]),
    )

    assert report.verdict != PROMOTE


def test_a_majority_of_profitable_folds_over_enough_folds_clears() -> None:
    """Four folds with a positive total and a profitable majority is an edge."""
    report = evaluate_candidate(
        "组合",
        _payload([
            _fold(100.0, 0.5), _fold(20.0, 0.3), _fold(-5.0, -0.1), _fold(15.0, 0.4)
        ]),
    )

    assert report.verdict == PROMOTE, report.reasons
