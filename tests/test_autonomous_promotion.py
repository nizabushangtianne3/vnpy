"""Promotion gates must be able to say no.

The point of these tests is the refusal, not the pass. A promotion rule that
only ever produces "promote" cannot be autonomous: it is autopilot. Every
candidate here is shaped like a real walk-forward payload, because the gates
read whatever the JSON writers emit and a gate that only works on fabricated
ideal shapes is decoration.
"""

from __future__ import annotations

import json

from vnpy.autonomous.promotion import (
    PROMOTE,
    RETIRE,
    WATCH,
    PromotionRules,
    evaluate_candidate,
    selected_setting,
)


def _folds(pnls: list[float], params: dict | None = None) -> list[dict]:
    """Fold payloads shaped like what `optimize --output` writes."""
    return [
        {
            "fold": index + 1,
            "best_params": params or {"fast_window": 5},
            "train_sharpe": 1.0,
            "train_pnl": 100000.0,
            "test_sharpe": 0.8,
            "test_pnl": pnl,
        }
        for index, pnl in enumerate(pnls)
    ]


def _payload(pnls: list[float], train_sharpe: float = 1.0) -> dict:
    return {
        "data_bars": 4000,
        "train_bars": 1400,
        "test_bars": 700,
        "folds": _folds(pnls),
        "aggregate_train_pnl": 350000.0,
        "aggregate_test_pnl": sum(pnls),
        "aggregate_train_sharpe": train_sharpe,
        "aggregate_test_sharpe": 0.6,
    }


def test_promotes_a_candidate_that_clears_every_gate() -> None:
    report = evaluate_candidate("m9999.DCE", _payload([50000.0, 60000.0]))
    assert report.verdict == PROMOTE
    assert report.profitable_fold_ratio == 1.0
    assert report.reasons


def test_retires_a_losing_candidate() -> None:
    report = evaluate_candidate("rb9999.SHFE", _payload([-10000.0, -5000.0]))
    assert report.verdict == RETIRE
    assert any("样本外" in reason for reason in report.reasons)


def test_retires_when_out_sharpe_collapses() -> None:
    """A small positive pnl with a dead Sharpe is still a failure."""
    payload = _payload([1000.0, 2000.0], train_sharpe=1.0)
    payload["aggregate_test_sharpe"] = 0.05
    report = evaluate_candidate("m9999.DCE", payload)
    assert report.verdict == RETIRE
    assert any("夏普" in reason for reason in report.reasons)


def test_retires_when_only_one_fold_is_profitable() -> None:
    """Three of four folds losing means the edge was one window, not a rule."""
    report = evaluate_candidate("m9999.DCE", _payload([90000.0, -1000.0, -2000.0, -3000.0]))
    assert report.verdict == RETIRE
    assert any("盈利折叠" in reason for reason in report.reasons)


def test_watch_when_one_leg_carries_the_whole_portfolio() -> None:
    """A portfolio result carried by one instrument is not diversification."""
    payload = _payload([50000.0, 60000.0])
    for fold in payload["folds"]:
        fold["leg_pnls"] = {"m": fold["test_pnl"] + 100.0, "rb": -100.0}
    report = evaluate_candidate("portfolio", payload)
    assert report.verdict == WATCH
    assert report.leg_pnls["m"] > 0
    assert report.leg_pnls["rb"] < 0
    assert any("分散化" in reason for reason in report.reasons)


def test_retires_when_the_payload_has_no_folds() -> None:
    """An empty payload must not promote on a missing aggregate figure."""
    payload = _payload([1.0])
    payload["folds"] = []
    report = evaluate_candidate("m9999.DCE", payload)
    assert report.verdict == RETIRE
    assert any("没有可用的样本外折叠" in reason for reason in report.reasons)


def test_rules_are_configurable() -> None:
    """A stricter operator raises the bar and the verdict must follow."""
    rules = PromotionRules(min_out_sharpe_ratio=0.99, min_profitable_fold_ratio=1.0)
    report = evaluate_candidate("m9999.DCE", _payload([50000.0, 60000.0]), rules)
    assert report.verdict == RETIRE


def test_selected_setting_uses_the_latest_validated_fold() -> None:
    payload = _payload([100.0, 200.0])
    payload["folds"][0]["best_params"] = {"fast_window": 5, "trail_stop": 0.0}
    payload["folds"][1]["best_params"] = {"fast_window": 10, "trail_stop": 2.0}
    setting = selected_setting(payload)
    assert setting == {"fast_window": 10, "trail_stop": 2.0}


def test_selected_setting_refuses_to_arm_a_grid() -> None:
    """No validated fold means nothing to arm, not the whole grid."""
    payload = _payload([100.0])
    for fold in payload["folds"]:
        fold.pop("best_params")
    assert selected_setting(payload) is None


def test_report_summary_is_machine_readable() -> None:
    report = evaluate_candidate("m9999.DCE", _payload([50000.0]))
    payload = report.summary()
    assert payload["verdict"] == PROMOTE
    assert payload["name"] == "m9999.DCE"
    assert isinstance(payload["reasons"], list)
    json.dumps(payload, ensure_ascii=False)


def test_malformed_payload_does_not_crash_the_gate() -> None:
    """A payload from a failed run must retire, not raise."""
    report = evaluate_candidate("m9999.DCE", {"folds": [{"fold": 1}]})
    assert report.verdict == RETIRE

