import json

from vnpy.autonomous.config import (
    AutonomousConfig,
    load_config,
    save_config,
    validate_config,
)


def test_defaults_are_valid_with_gateway() -> None:
    config = AutonomousConfig(gateways={"CTP": {"用户名": "x"}})
    assert validate_config(config) == []


def test_validation_reports_problems() -> None:
    bad = AutonomousConfig()
    errors = validate_config(bad)
    assert any("gateways" in message for message in errors)

    bad.mode = "hedge"
    bad.gateways = {"CTP": {}}
    errors = validate_config(bad)
    assert any("mode" in message for message in errors)

    bad.mode = "live"
    bad.portfolio.method = "sharpe"
    bad.notification.heartbeat_secs = 0
    errors = validate_config(bad)
    assert any("portfolio.method" in message for message in errors)
    assert any("heartbeat" in message for message in errors)


def test_roundtrip_save_and_load(tmp_path) -> None:
    config = AutonomousConfig(
        mode="paper",
        gateways={"CTP": {"用户名": "demo", "密码": "secret"}},
        apps=["CtaStrategy"],
    )
    config.risk.max_order_volume = 10
    config.runtime.state_file = "state.json"

    path = tmp_path / "autonomous.json"
    save_config(config, path)

    loaded = load_config(path)
    assert loaded.mode == "paper"
    assert loaded.gateways["CTP"]["用户名"] == "demo"
    assert loaded.apps == ["CtaStrategy"]
    assert loaded.risk.max_order_volume == 10
    assert loaded.runtime.state_file == "state.json"


def test_from_dict_ignores_unknown_keys() -> None:
    raw = {
        "mode": "live",
        "unknown_top": 1,
        "risk": {"max_order_volume": 5, "unknown_risk": True},
        "calendar": {"profile": "full_day"},
    }
    config = AutonomousConfig.from_dict(raw)
    assert config.mode == "live"
    assert config.risk.max_order_volume == 5
    assert config.calendar["profile"] == "full_day"


def test_load_config_rejects_non_object(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    try:
        load_config(path)
    except ValueError:
        pass
    else:
        raise AssertionError("列表配置应当抛出 ValueError")
