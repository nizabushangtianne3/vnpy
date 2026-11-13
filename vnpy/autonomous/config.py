"""Configuration model for the autonomous trading runner."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


CONFIG_DIR_NAME: str = ".vntrader"
CONFIG_FILE_NAME: str = "autonomous.json"


def default_config_path() -> Path:
    """Default config location inside the .vntrader folder."""
    return Path.cwd() / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def state_file_path(path: str | None) -> Path:
    """Resolve a runtime state file path relative to .vntrader."""
    if path:
        candidate: Path = Path(path)
        if candidate.is_absolute():
            return candidate
    else:
        candidate = Path("autonomous_state.json")
    base: Path = Path.cwd() / CONFIG_DIR_NAME
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
    return base / candidate


@dataclass
class RiskLimits:
    """Pre-trade risk limits enforced by the risk guard."""

    max_order_volume: float = 0              # 0 means unlimited
    max_order_value: float = 0               # volume * price * contract size
    max_active_orders: int = 0               # account-wide active orders
    max_position_volume: float = 0           # default per-symbol net position cap
    symbol_position_limits: dict[str, float] = field(default_factory=dict)
    daily_loss_limit: float = 0              # absolute loss cap per trading day
    max_drawdown: float = 0                  # drawdown cap from session peak equity
    kill_switch_on_breach: bool = True       # flatten and block new entries
    allow_reduce_when_armed: bool = True     # closing orders still allowed
    enforce_t_plus_one: bool = True          # reject selling shares bought today

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RiskLimits:
        """Build limits from a raw config dict, ignoring unknown keys."""
        known: set[str] = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})   # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict."""
        return asdict(self)


@dataclass
class HealthRules:
    """Strategy health circuit breaker rules."""

    max_consecutive_losses: int = 5
    max_daily_loss: float = 0                # per strategy realized loss cap
    max_error_count: int = 10                # errors within the error window
    error_window_secs: int = 3600
    halt_cooldown_secs: int = 1800           # minimum halt duration before resume

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> HealthRules:
        """Build rules from a raw config dict, ignoring unknown keys."""
        known: set[str] = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})   # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict."""
        return asdict(self)


@dataclass
class AllocationConfig:
    """Capital allocation settings across strategies."""

    method: str = "equal"                    # equal | inverse_volatility
    max_weight: float = 0.5                  # per-strategy weight cap
    min_weight: float = 0.0                  # per-strategy weight floor
    cash_fraction: float = 0.1               # fraction kept in cash
    vol_window: int = 60                     # equity samples for volatility

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AllocationConfig:
        """Build allocation config from a raw config dict."""
        known: set[str] = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})   # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict."""
        return asdict(self)


@dataclass
class NotificationConfig:
    """Notification routing for lifecycle events."""

    notify_on_start: bool = True
    notify_on_error: bool = True
    notify_on_breach: bool = True
    notify_on_session_end: bool = True
    heartbeat_secs: int = 1800
    webhook_url: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> NotificationConfig:
        """Build notification config from a raw config dict."""
        known: set[str] = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})   # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict."""
        return asdict(self)


@dataclass
class RuntimeConfig:
    """Runtime behavior of the autonomous process."""

    poll_interval_secs: float = 1.0
    state_file: str = "autonomous_state.json"
    contract_wait_secs: float = 60.0
    strategy_init_wait_secs: float = 30.0
    flat_on_session_end: bool = False        # close positions when session ends
    stop_strategies_on_exit: bool = True
    restart_backoff_secs: float = 30.0
    max_restarts_per_session: int = 5
    watchdog_stall_secs: float = 300.0       # no event from gateway for this long
    auto_reconnect: bool = True
    # When no gateway supplies ticks, derive them from the public minute-bar
    # endpoint so a paper account has something to match orders against.
    # Disabled by default because a config that already receives real ticks from
    # a broker gateway must not have a second, derived feed behind its back.
    enable_derived_feed: bool = False
    derived_feed_poll_secs: float = 60.0
    derived_feed_max_catchup: int = 5

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RuntimeConfig:
        """Build runtime config from a raw config dict."""
        known: set[str] = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})   # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict."""
        return asdict(self)


@dataclass
class AutonomousConfig:
    """Full configuration for autonomous trading."""

    mode: str = "paper"                      # paper | live
    gateways: dict[str, dict[str, object]] = field(default_factory=dict)
    apps: list[str] = field(default_factory=list)
    app_settings: dict[str, dict[str, object]] = field(default_factory=dict)
    calendar: dict[str, object] = field(default_factory=dict)
    promotion: dict[str, object] = field(default_factory=dict)
    risk: RiskLimits = field(default_factory=RiskLimits)
    health: HealthRules = field(default_factory=HealthRules)
    portfolio: AllocationConfig = field(default_factory=AllocationConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AutonomousConfig:
        """Assemble nested dataclasses from a raw config dict."""
        risk_raw: dict[str, object] = _as_dict(data.get("risk"))
        health_raw: dict[str, object] = _as_dict(data.get("health"))
        portfolio_raw: dict[str, object] = _as_dict(data.get("portfolio"))
        notification_raw: dict[str, object] = _as_dict(data.get("notification"))
        runtime_raw: dict[str, object] = _as_dict(data.get("runtime"))
        gateways_raw: dict[str, dict[str, object]] = {}
        raw_gateways: object = data.get("gateways")
        if isinstance(raw_gateways, dict):
            gateway_name: str
            gateway_setting: object
            for gateway_name, gateway_setting in raw_gateways.items():
                gateways_raw[str(gateway_name)] = _as_dict(gateway_setting)
        apps_raw: list[str] = []
        raw_apps: object = data.get("apps")
        if isinstance(raw_apps, list):
            app_item: object
            for app_item in raw_apps:
                apps_raw.append(str(app_item))
        return cls(
            mode=str(data.get("mode", "paper")),
            gateways=gateways_raw,
            apps=apps_raw,
            app_settings=_as_nested_dict(data.get("app_settings")),
            calendar=_as_dict(data.get("calendar")),
            promotion=_as_dict(data.get("promotion")),
            risk=RiskLimits.from_dict(risk_raw),
            health=HealthRules.from_dict(health_raw),
            portfolio=AllocationConfig.from_dict(portfolio_raw),
            notification=NotificationConfig.from_dict(notification_raw),
            runtime=RuntimeConfig.from_dict(runtime_raw),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize the whole configuration to a plain dict."""
        return {
            "mode": self.mode,
            "gateways": self.gateways,
            "apps": self.apps,
            "app_settings": self.app_settings,
            "calendar": self.calendar,
            "risk": self.risk.to_dict(),
            "health": self.health.to_dict(),
            "portfolio": self.portfolio.to_dict(),
            "notification": self.notification.to_dict(),
            "runtime": self.runtime.to_dict(),
        }


def _as_dict(value: object) -> dict[str, object]:
    """Coerce an optional JSON value into a dict."""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _as_nested_dict(value: object) -> dict[str, dict[str, object]]:
    """Coerce an optional JSON value into a dict of dicts."""
    outer: dict[str, object] = _as_dict(value)
    nested: dict[str, dict[str, object]] = {}
    key: str
    for key, item in outer.items():
        nested[key] = _as_dict(item)
    return nested


def validate_config(config: AutonomousConfig) -> list[str]:
    """Return a list of validation error messages, empty when valid."""
    errors: list[str] = []

    if config.mode not in ("paper", "live"):
        errors.append(f"mode 仅支持 paper 或 live，当前为 {config.mode!r}")

    if not config.gateways:
        errors.append("gateways 为空，至少需要配置一个交易接口")

    for name, setting in config.gateways.items():
        if not isinstance(setting, dict):
            errors.append(f"gateways[{name}] 必须为字典")

    method: str = config.portfolio.method
    if method not in ("equal", "inverse_volatility"):
        errors.append(f"portfolio.method 仅支持 equal 或 inverse_volatility，当前为 {method!r}")

    if not 0.0 <= config.portfolio.max_weight <= 1.0:
        errors.append("portfolio.max_weight 应在 0 到 1 之间")

    if not 0.0 <= config.portfolio.cash_fraction < 1.0:
        errors.append("portfolio.cash_fraction 应在 0 到 1 之间")

    if config.risk.max_order_volume < 0 or config.risk.max_position_volume < 0:
        errors.append("风控上限参数不能为负数")

    if config.notification.heartbeat_secs <= 0:
        errors.append("notification.heartbeat_secs 必须大于 0")

    if config.runtime.poll_interval_secs <= 0:
        errors.append("runtime.poll_interval_secs 必须大于 0")

    return errors


def load_config(path: str | Path | None = None) -> AutonomousConfig:
    """Load an autonomous config from a JSON file."""
    config_path: Path = Path(path) if path else default_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")

    with open(config_path, encoding="utf-8") as file:
        payload: object = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError(f"配置文件格式错误，应为 JSON 对象：{config_path}")

    raw: dict[str, object] = {str(k): v for k, v in payload.items()}
    config: AutonomousConfig = AutonomousConfig.from_dict(raw)

    errors: list[str] = validate_config(config)
    if errors:
        raise ValueError("配置校验失败：" + "；".join(errors))
    return config


def save_config(config: AutonomousConfig, path: str | Path | None = None) -> Path:
    """Persist an autonomous config to a JSON file."""
    config_path: Path = Path(path) if path else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as file:
        json.dump(config.to_dict(), file, ensure_ascii=False, indent=2)
    return config_path
