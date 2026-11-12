"""Promotion gates must be able to say no.

The point of these tests is the refusal, not the pass. A promotion rule that
only ever produces "promote" cannot be autonomous: it is autopilot. Every
candidate here is shaped like a real walk-forward payload, because the gates
read whatever the JSON writers emit and a gate that only works on fabricated
ideal shapes is decoration.
"""

from __future__ import annotations

import json

import pytest

from vnpy.autonomous.promotion import (
    PROMOTE,
    RETIRE,
    WATCH,
    PromotionDecision,
    PromotionRules,
    apply_verdicts,
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
            "test_trade_count": 40,
            "test_round_trips": 9,
            "test_win_rate": 0.34,
            "test_profit_factor": 1.3,
            "test_max_drawdown": 1000.0,
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
        "aggregate_test_trade_count": 40 * len(pnls),
        "aggregate_test_round_trips": 9 * len(pnls),
        "aggregate_test_win_rate": 0.34,
        "aggregate_test_profit_factor": 1.3,
    }


def test_promotes_a_candidate_that_clears_every_gate() -> None:
    report = evaluate_candidate("m9999.DCE", _payload([50000.0, 60000.0, 30000.0, 40000.0]))
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


def test_retires_when_too_few_folds_support_the_ratio() -> None:
    """One winner and one loser cannot average into a healthy edge.

    A two-fold run where one window wins and the other loses reports a 50%
    profitable ratio, which reads as "half the time it works". It is not a
    repeated edge, it is two outcomes, and the ratio gate was measuring
    noise. This is the shape that let a tin candidate promote on a coarse
    window split while the same data on a finer split retired it.
    """
    payload = _payload([4701564.99, -2051770.90])
    payload["aggregate_test_sharpe"] = 0.774
    payload["aggregate_train_sharpe"] = 1.024
    report = evaluate_candidate("sn9999.SHFE", payload)
    assert report.verdict == RETIRE
    assert any("折叠仅" in reason for reason in report.reasons)


def test_fold_floor_is_configurable() -> None:
    """An operator with more history may lower the floor."""
    payload = _payload([4701564.99, -2051770.90])
    rules = PromotionRules(min_folds=2)
    report = evaluate_candidate("sn9999.SHFE", payload, rules)
    assert report.verdict != RETIRE or not any("折叠仅" in r for r in report.reasons)


def test_reads_min_folds_as_an_integer() -> None:
    """A fold count is a count, not a ratio; keep the type honest."""
    rules = PromotionRules.from_dict({"min_folds": "6", "min_out_pnl": "100"})
    assert rules.min_folds == 6
    assert isinstance(rules.min_folds, int)
    assert rules.min_out_pnl == pytest.approx(100.0)


def test_watch_when_one_leg_carries_the_whole_portfolio() -> None:
    """A portfolio result carried by one instrument is not diversification."""
    payload = _payload([50000.0, 60000.0, 30000.0, 40000.0])
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
    report = evaluate_candidate("m9999.DCE", _payload([50000.0, 60000.0, 30000.0, 40000.0]), rules)
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
    report = evaluate_candidate("m9999.DCE", _payload([50000.0, 60000.0, 30000.0, 40000.0]))
    payload = report.summary()
    assert payload["verdict"] == PROMOTE
    assert payload["name"] == "m9999.DCE"
    assert isinstance(payload["reasons"], list)
    json.dumps(payload, ensure_ascii=False)


def test_malformed_payload_does_not_crash_the_gate() -> None:
    """A payload from a failed run must retire, not raise."""
    report = evaluate_candidate("m9999.DCE", {"folds": [{"fold": 1}]})
    assert report.verdict == RETIRE


def _decision(
    name: str, verdict: str, setting: dict | None = None
) -> PromotionDecision:
    return PromotionDecision(
        name=name,
        verdict=verdict,
        setting=setting or {"fast_window": 5},
    )


def test_apply_adds_a_promoted_candidate() -> None:
    """A promote verdict must reach the config a runner reads."""
    document, changes = apply_verdicts(
        {},
        {"m9999.DCE": _decision("m9999.DCE", PROMOTE, {"fast_window": 5, "trail_stop": 4.0})},
    )
    strategies = document["app_settings"]["CtaStrategy"]["strategies"]  # type: ignore[index]
    assert strategies == [
        {
            "class_name": "AutonomousTrendStrategy",
            "vt_symbol": "m9999.DCE",
            "auto_active": True,
            "setting": {"fast_window": 5, "trail_stop": 4.0},
        }
    ]
    assert changes == ["m9999.DCE 通过门槛，已按验证参数装配"]


def test_apply_removes_a_retired_candidate() -> None:
    """A retired instrument must not keep trading on stale parameters."""
    document, changes = apply_verdicts(
        {
            "app_settings": {
                "CtaStrategy": {
                    "strategies": [
                        {
                            "class_name": "AutonomousTrendStrategy",
                            "vt_symbol": "rb9999.SHFE",
                            "setting": {"fast_window": 5},
                        }
                    ]
                }
            }
        },
        {"rb9999.SHFE": _decision("rb9999.SHFE", RETIRE)},
    )
    assert document["app_settings"]["CtaStrategy"]["strategies"] == []  # type: ignore[index]
    assert changes == ["rb9999.SHFE retire，已移出配置"]


def test_apply_removes_a_watched_candidate() -> None:
    """Watch refused arming, so it must not end up armed in the config."""
    document, _ = apply_verdicts(
        {
            "app_settings": {
                "CtaStrategy": {
                    "strategies": [
                        {
                            "class_name": "AutonomousTrendStrategy",
                            "vt_symbol": "sn9999.SHFE",
                            "setting": {"fast_window": 5},
                        }
                    ]
                }
            }
        },
        {"sn9999.SHFE": _decision("sn9999.SHFE", WATCH)},
    )
    assert document["app_settings"]["CtaStrategy"]["strategies"] == []  # type: ignore[index]


def test_apply_leaves_unjudged_entries_alone() -> None:
    """An operator's own declaration is not a promotion decision."""
    original = {
        "app_settings": {
            "CtaStrategy": {
                "strategies": [
                    {"class_name": "ManualStrategy", "vt_symbol": "ag9999.SHFE"}
                ]
            }
        }
    }
    document, changes = apply_verdicts(
        original,
        {"m9999.DCE": _decision("m9999.DCE", PROMOTE)},
    )
    strategies = document["app_settings"]["CtaStrategy"]["strategies"]  # type: ignore[index]
    assert strategies[0]["class_name"] == "ManualStrategy"
    assert strategies[0]["vt_symbol"] == "ag9999.SHFE"
    assert len(strategies) == 2
    assert changes == ["m9999.DCE 通过门槛，已按验证参数装配"]


def test_apply_preserves_the_rest_of_the_document() -> None:
    """Gateway credentials and risk limits are none of promotion's business."""
    original = {
        "mode": "live",
        "gateways": {"CTP": {"用户名": "operator", "密码": "secret"}},
        "risk": {"daily_loss_limit": 5000},
        "runtime": {"state_file": "autonomous_state.json"},
        "app_settings": {},
    }
    document, _ = apply_verdicts(
        original, {"m9999.DCE": _decision("m9999.DCE", PROMOTE)}
    )
    assert document["mode"] == "live"
    assert document["gateways"] == {"CTP": {"用户名": "operator", "密码": "secret"}}
    assert document["risk"] == {"daily_loss_limit": 5000}
    assert document["runtime"] == {"state_file": "autonomous_state.json"}


def test_apply_refreshes_parameters_on_an_existing_entry() -> None:
    """The armed setting is the one the latest fold validated."""
    document, changes = apply_verdicts(
        {
            "app_settings": {
                "CtaStrategy": {
                    "strategies": [
                        {
                            "class_name": "AutonomousTrendStrategy",
                            "vt_symbol": "m9999.DCE",
                            "setting": {"fast_window": 10, "trail_stop": 0.0},
                        }
                    ]
                }
            }
        },
        {
            "m9999.DCE": _decision(
                "m9999.DCE", PROMOTE, {"fast_window": 5, "trail_stop": 4.0}
            )
        },
    )
    strategies = document["app_settings"]["CtaStrategy"]["strategies"]  # type: ignore[index]
    assert len(strategies) == 1
    assert strategies[0]["setting"] == {"fast_window": 5, "trail_stop": 4.0}
    assert changes == ["m9999.DCE 参数已刷新为最新验证值"]


def test_apply_never_emits_a_setting_for_a_refused_verdict() -> None:
    """A watch decision carries no setting, so nothing stale can be armed."""
    decision = PromotionDecision(
        name="sn9999.SHFE", verdict=WATCH, setting={"fast_window": 5}
    )
    document, _ = apply_verdicts(
        {},
        {"sn9999.SHFE": decision},
    )
    assert document["app_settings"]["CtaStrategy"]["strategies"] == []  # type: ignore[index]

def test_retires_when_out_of_sample_trade_count_is_too_thin() -> None:
    """A profit factor over three round trips is not a claim about anything.

    The edge gates grade repeatability, so grading them on a sample of three
    hands the operator a verdict that describes noise. A candidate that clears
    every other gate but traded too little is retired, not promoted.
    """
    payload = _payload([50000.0, 60000.0, 30000.0, 40000.0])
    payload["aggregate_test_round_trips"] = 6

    report = evaluate_candidate("m9999.DCE", payload)

    assert report.verdict == RETIRE
    assert any("回合数" in reason for reason in report.reasons)


def test_retires_when_losses_are_not_covered() -> None:
    """A total pnl that only one outlier produced is a lottery ticket.

    Profit factor below one means the winners do not even cover the losers,
    however large the remaining figure looks.
    """
    payload = _payload([50000.0, 60000.0, 30000.0, 40000.0])
    payload["aggregate_test_profit_factor"] = 0.6

    report = evaluate_candidate("m9999.DCE", payload)

    assert report.verdict == RETIRE
    assert any("盈亏比" in reason for reason in report.reasons)


def test_report_summary_reports_trade_quality() -> None:
    """The machine-readable summary must carry the measured figures.

    A decision an operator cannot audit is decoration; the same numbers the
    gates read have to appear in the JSON and the state file.
    """
    report = evaluate_candidate("m9999.DCE", _payload([50000.0, 60000.0, 30000.0, 40000.0]))
    payload: dict = report.summary()

    assert payload["out_round_trips"] > 0
    assert payload["out_win_rate"] > 0
    assert payload["out_profit_factor"] > 0
