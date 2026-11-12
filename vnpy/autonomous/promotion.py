"""Turn a backtest verdict into a promotion decision.

The promotion manual in ``docs/community/autonomous/promotion.md`` asks for a
decision per candidate instrument, but the manual is a document: every step
assumed a human reading a walk-forward table and editing the config. That is
the gap between "can trade by itself" and "can decide for itself".

This module holds the rules, not the workflow. Each gate maps a measured
figure to one of three verdicts, and the verdicts stay boring: promote,
watch, retire. Refusing to invent a fourth "promising" state matters, since a
system that declares its own results promising has lost the ability to stop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime


PROMOTE: str = "promote"
WATCH: str = "watch"
RETIRE: str = "retire"


@dataclass
class PromotionRules:
    """Thresholds one candidate must clear to reach the paper pool.

    Defaults follow the promotion manual's stage one: out-of-sample pnl
    positive, and out-of-sample Sharpe within half the in-sample figure, which
    is the manual's "not significantly worse" wording restated as a number.
    """

    min_out_pnl: float = 0.0
    min_out_sharpe_ratio: float = 0.5
    min_profitable_fold_ratio: float = 0.5
    min_folds: int = 4

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PromotionRules:
        """Build rules from a raw config dict, ignoring unknown keys."""
        defaults: PromotionRules = cls()
        known: set[str] = set(cls().__dataclass_fields__)
        values: dict[str, float] = {}
        int_fields: set[str] = {
            name for name in known if isinstance(getattr(defaults, name), int)
        }
        for key, value in data.items():
            if key not in known or value is None:
                continue
            values[key] = (
                int(_as_float(value)) if key in int_fields else _as_float(value)
            )
        return cls(**values)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, float]:
        """Serialize to a plain dict for config files."""
        return {name: float(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass
class CandidateReport:
    """Verdict for one candidate, plus the numbers behind it."""

    name: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    out_pnl: float = 0.0
    out_sharpe: float = 0.0
    in_sharpe: float = 0.0
    profitable_fold_ratio: float = 0.0
    leg_pnls: dict[str, float] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        """Flat mapping for JSON output and state files."""
        return {
            "name": self.name,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "out_pnl": self.out_pnl,
            "out_sharpe": self.out_sharpe,
            "in_sharpe": self.in_sharpe,
            "profitable_fold_ratio": self.profitable_fold_ratio,
            "leg_pnls": dict(self.leg_pnls),
        }


def evaluate_candidate(
    name: str,
    payload: Mapping[str, object],
    rules: PromotionRules | None = None,
) -> CandidateReport:
    """Decide whether one candidate earns a place in the paper pool.

    ``payload`` is the JSON a walk-forward run writes with ``--output``: an
    ``optimize`` run, or a ``portfolio`` run. Both share ``folds``,
    ``aggregate_test_pnl`` and ``aggregate_test_sharpe``; only the portfolio
    run carries per-instrument ``leg_pnls``, so its verdict also reports
    whether one leg carried the whole result.

    A candidate that fails keeps every reason, not just the first: an operator
    reading a single "rejected" cannot tell which assumption broke, and the
    next campaign repeats the mistake.
    """
    if rules is None:
        rules = PromotionRules()

    folds: list[Mapping[str, object]] = [
        fold for fold in _as_list(payload.get("folds")) if isinstance(fold, Mapping)
    ]
    out_pnl: float = _as_float(payload.get("aggregate_test_pnl"))
    out_sharpe: float = _as_float(payload.get("aggregate_test_sharpe"))
    in_sharpe: float = _as_float(payload.get("aggregate_train_sharpe"))

    fold_pnls: list[float] = [_as_float(fold.get("test_pnl")) for fold in folds]
    profitable_folds: int = sum(1 for value in fold_pnls if value > 0)
    fold_ratio: float = profitable_folds / len(fold_pnls) if fold_pnls else 0.0

    reasons: list[str] = []
    if not folds:
        reasons.append("没有可用的样本外折叠，无法判定")
    if out_pnl <= rules.min_out_pnl:
        reasons.append(f"样本外合计盈亏 {out_pnl:.2f} 未超过 {rules.min_out_pnl:.2f}")
    if in_sharpe > 0 and out_sharpe < in_sharpe * rules.min_out_sharpe_ratio:
        reasons.append(
            f"样本外夏普 {out_sharpe:.3f} 低于样本内 {in_sharpe:.3f} 的 "
            f"{rules.min_out_sharpe_ratio:.0%}"
        )
    if folds and fold_ratio < rules.min_profitable_fold_ratio:
        reasons.append(
            f"盈利折叠 {profitable_folds}/{len(fold_pnls)} 低于 "
            f"{rules.min_profitable_fold_ratio:.0%}"
        )
    if folds and len(folds) < rules.min_folds:
        # Two folds where one wins and one loses average into "healthy"
        # through no merit of their own. A fold count below the floor means
        # the ratio gate is measuring noise, not a repeated edge.
        reasons.append(
            f"样本外折叠仅 {len(folds)} 个，少于 {rules.min_folds} 个，"
            "不足以支撑「多数折叠盈利」的判断"
        )

    # A portfolio result carried by one leg is not diversification, and the
    # manual asks for several uncorrelated legs, so a single surviving leg that
    # otherwise clears the gates is held on watch rather than promoted.
    leg_pnls: dict[str, float] = {}
    carry_risk: str = ""
    portfolio_folds: list[Mapping[str, object]] = [
        fold
        for fold in folds
        if isinstance(_as_mapping(fold.get("leg_pnls")), dict)
    ]
    if portfolio_folds:
        named: set[str] = set()
        for fold in portfolio_folds:
            named.update(_as_floats(fold.get("leg_pnls")))
        for leg_name in named:
            leg_pnls[leg_name] = sum(
                _as_float(_as_floats(fold.get("leg_pnls")).get(leg_name))
                for fold in portfolio_folds
            )
        profitable_legs: int = sum(1 for value in leg_pnls.values() if value > 0)
        if profitable_legs == 1 and len(leg_pnls) > 1:
            carry_risk = "组合只有一个品种盈利，其余品种未提供分散化贡献"

    verdict: str
    if reasons:
        verdict = RETIRE
    elif carry_risk:
        verdict = WATCH
        reasons.append(carry_risk)
    else:
        verdict = PROMOTE
        reasons.append(
            f"样本外盈亏 {out_pnl:.2f}、夏普 {out_sharpe:.3f}、"
            f"盈利折叠 {profitable_folds}/{len(fold_pnls)}，通过全部门槛"
        )

    return CandidateReport(
        name=name,
        verdict=verdict,
        reasons=reasons,
        out_pnl=out_pnl,
        out_sharpe=out_sharpe,
        in_sharpe=in_sharpe,
        profitable_fold_ratio=fold_ratio,
        leg_pnls=leg_pnls,
    )


def selected_setting(payload: Mapping[str, object]) -> dict[str, object] | None:
    """The parameters to arm, taken from the most recent validated fold.

    The grid is never the answer: ``_pick_best_params`` chose one combination
    per fold and the out-of-sample figure is the evidence, so replaying the
    grid live would arm combinations nobody validated. The last fold wins
    because parameters that stopped working are exactly what a stale
    configuration keeps trading.
    """
    chosen: list[Mapping[str, object]] = [
        fold
        for fold in _as_list(payload.get("folds"))
        if isinstance(fold, Mapping)
        and isinstance(_as_mapping(fold.get("best_params")), dict)
    ]
    if not chosen:
        return None
    best: dict[str, object] | None = _as_mapping(chosen[-1].get("best_params"))
    if best is None:
        return None
    return dict(best)


def promotion_timestamp() -> str:
    """Stamp for the audit trail, isolated so tests need no clock."""
    return datetime.now().isoformat(timespec="seconds")

def _as_mapping(value: object) -> dict[str, object] | None:
    """Return ``value`` as a JSON object, or None when it is not one.

    ``json.loads`` gives ``Any`` for every nested node, so walking a payload
    without narrowing leaves mypy unable to say whether the next level is an
    object, a list, or a number. Shapes are validated here once instead of
    trusting that every writer produced them.
    """
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return None


def _as_floats(value: object) -> dict[str, float]:
    """Return a numeric mapping, dropping anything that is not a number."""
    mapping: dict[str, object] | None = _as_mapping(value)
    if mapping is None:
        return {}
    numbers: dict[str, float] = {}
    for key, item in mapping.items():
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            numbers[key] = float(item)
    return numbers


def _as_float(value: object, default: float = 0.0) -> float:
    """Coerce a JSON scalar to float, falling back when it is not numeric."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # Hand-written config files quote their numbers often enough that
        # rejecting "6" silently would turn a threshold into zero.
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _as_list(value: object) -> list[object]:
    """Return ``value`` as a JSON array, or empty when it is not one."""
    if isinstance(value, list):
        return list(value)
    return []


@dataclass
class PromotionDecision:
    """One verdict plus the validated parameters it is allowed to arm."""

    name: str
    verdict: str
    setting: dict[str, object] = field(default_factory=dict)
    strategy_class: str = "AutonomousTrendStrategy"

    def summary(self) -> dict[str, object]:
        """Flat mapping for JSON output and assertions."""
        return {
            "name": self.name,
            "verdict": self.verdict,
            "setting": dict(self.setting),
            "strategy_class": self.strategy_class,
        }


def apply_verdicts(
    raw_config: Mapping[str, object],
    verdicts: Mapping[str, PromotionDecision],
    app_name: str = "CtaStrategy",
) -> tuple[dict[str, object], list[str]]:
    """Return a config document with promotions written into it.

    Only ``app_settings[app_name].strategies`` is touched. The rest of the
    document is carried through untouched, because a config file also holds
    gateway credentials and risk limits that a promotion decision has no
    business rewriting, and round-tripping through ``AutonomousConfig`` would
    drop any key its dataclasses do not model.

    A ``retire`` or ``watch`` entry for an instrument already declared is
    removed, so a candidate that stopped working cannot keep trading on
    parameters validated in a window that no longer exists. A ``promote``
    entry is refreshed with its validated parameters, or appended when the
    instrument is not yet declared. Instruments nobody judged are left
    exactly as the operator declared them. The change list states what
    happened while writing rather than being diffed afterwards, because a
    diff guesses at intent and the log has to state it.
    """
    changes: list[str] = []
    document: dict[str, object] = dict(raw_config)

    app_settings: dict[str, object] = {}
    existing_app_settings: object = document.get("app_settings")
    if isinstance(existing_app_settings, dict):
        app_settings = dict(existing_app_settings)

    app_block: dict[str, object] = {}
    existing_block: object = app_settings.get(app_name)
    if isinstance(existing_block, dict):
        app_block = dict(existing_block)

    entries: list[object] = []
    declared: object = app_block.get("strategies")
    if isinstance(declared, list):
        entries = list(declared)

    for symbol, decision in verdicts.items():
        index: int | None = None
        for position, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("vt_symbol") == symbol:
                index = position
                break

        if decision.verdict != PROMOTE:
            # Any verdict other than promote means do not arm, so an existing
            # declaration is removed instead of silently left trading.
            if index is not None:
                entries.pop(index)
                changes.append(f"{symbol} {decision.verdict}，已移出配置")
            continue

        if index is None:
            entries.append(
                {
                    "class_name": decision.strategy_class,
                    "vt_symbol": symbol,
                    "auto_active": True,
                    "setting": dict(decision.setting),
                }
            )
            changes.append(f"{symbol} 通过门槛，已按验证参数装配")
            continue

        existing: object = entries[index]
        if isinstance(existing, dict):
            refreshed: dict[str, object] = dict(existing)
            refreshed["setting"] = dict(decision.setting)
            entries[index] = refreshed
            changes.append(f"{symbol} 参数已刷新为最新验证值")

    app_block["strategies"] = entries
    app_settings[app_name] = app_block
    document["app_settings"] = app_settings
    return document, changes

