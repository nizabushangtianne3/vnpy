"""Session-aware supervisor for autonomous trading processes.

The runner has two layers, mirroring the daemon pattern used by mature
open-source trading bots (Freqtrade trade loop, Hummingbot orchestrators):

* A parent supervisor process that keeps a trading child alive inside the
  trading calendar, restarting it with backoff and writing runtime state.
* A child process that owns the MainEngine, gateways, apps, risk guard,
  health monitor and capital allocator, and exits when the session ends.
"""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import sys
import time
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from vnpy.event import Event, EventEngine
from vnpy.trader.app import BaseApp
from vnpy.trader.constant import Status
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_LOG, EVENT_ORDER, EVENT_TRADE
from vnpy.trader.logger import logger
from vnpy.trader.object import LogData, OrderData, TradeData
from vnpy.trader.setting import SETTINGS

from .calendar import Session, TradingCalendar, build_profile, load_holidays
from .config import AutonomousConfig, load_config
from .event import EVENT_STRATEGY_SNAPSHOT, StrategySnapshot
from .health import HealthMonitor
from .portfolio import CapitalAllocator
from .risk import RiskGuard


# Well-known gateway classes. Unknown entries may use "module:Class" syntax.
GATEWAY_REGISTRY: dict[str, str] = {
    "CTP": "vnpy_ctp:CtpGateway",
    "CTP Mini": "vnpy_mini:MiniGateway",
    "CTP证券": "vnpy_sopt:SoptGateway",
    "易盛": "vnpy_esunny:EsunnyGateway",
    "XTP": "vnpy_xtp:XtpGateway",
    "TORA": "vnpy_tora:ToraGateway",
    "TTS": "vnpy_tts:TtsGateway",
    "FEMAS": "vnpy_femas:FemasGateway",
    "顶点HTS": "vnpy_hts:HtsGateway",
    "顶点飞创": "vnpy_sec:SecGateway",
    "IB": "vnpy_ib:IbGateway",
}

# Well-known app modules. Unknown entries may use "module:Class" syntax.
APP_REGISTRY: dict[str, str] = {
    "CtaStrategy": "vnpy_ctastrategy:CtaStrategyApp",
    "PortfolioStrategy": "vnpy_portfoliostrategy:PortfolioStrategyApp",
    "RiskManager": "vnpy_riskmanager:RiskManagerApp",
    "PaperAccount": "vnpy_paperaccount:PaperAccountApp",
    "SpreadTrading": "vnpy_spreadtrading:SpreadTradingApp",
    "OptionMaster": "vnpy_optionmaster:OptionMasterApp",
    "AlgoTrading": "vnpy_algotrading:AlgoTradingApp",
    "ScriptTrader": "vnpy_scripttrader:ScriptTraderApp",
    "ChartWizard": "vnpy_chartwizard:ChartWizardApp",
    "DataManager": "vnpy_datamanager:DataManagerApp",
    "DataRecorder": "vnpy_datarecorder:DataRecorderApp",
}


class AppResolutionError(RuntimeError):
    """Raised when a configured gateway or app cannot be imported."""


def import_component(spec: str) -> Any:
    """Import a class from a \"module:Class\" spec string."""
    module_name: str
    attribute: str
    if ":" in spec:
        module_name, _, attribute = spec.partition(":")
    else:
        module_name, _, attribute = spec.rpartition(".")
        if not module_name or not attribute:
            raise AppResolutionError(f"无法解析组件路径：{spec}")

    try:
        module: Any = importlib.import_module(module_name)
    except ImportError as exc:
        raise AppResolutionError(f"模块 {module_name} 导入失败：{exc}") from exc

    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise AppResolutionError(
            f"模块 {module_name} 中不存在 {attribute}"
        ) from exc


def resolve_gateway_class(name: str) -> Any:
    """Resolve a gateway name to its gateway class."""
    spec: str | None = GATEWAY_REGISTRY.get(name)
    if spec is None:
        if ":" in name or "." in name:
            spec = name
        else:
            raise AppResolutionError(
                f"未知交易接口 {name}，请使用内置名称或 \"module:Class\" 写法"
            )
    return import_component(spec)


def resolve_app_class(name: str) -> type[BaseApp]:
    """Resolve an app name to its app class."""
    spec: str | None = APP_REGISTRY.get(name)
    if spec is None:
        if ":" in name or "." in name:
            spec = name
        else:
            raise AppResolutionError(
                f"未知应用模块 {name}，请使用内置名称或 \"module:Class\" 写法"
            )
    app_class: Any = import_component(spec)
    if not isinstance(app_class, type):
        raise AppResolutionError(f"应用模块 {name} 解析结果不是类")
    return app_class


@dataclass
class TradeLedger:
    """Per-strategy trade counters fed by order references."""

    trades: dict[str, int] = field(default_factory=dict)
    last_trade_time: dict[str, datetime] = field(default_factory=dict)

    def record(self, strategy_name: str, when: datetime) -> None:
        """Record one trade for the strategy named in the order reference."""
        self.trades[strategy_name] = self.trades.get(strategy_name, 0) + 1
        self.last_trade_time[strategy_name] = when

    def total(self) -> int:
        """Total trades recorded across strategies."""
        return sum(self.trades.values())

    def count(self, strategy_name: str) -> int:
        """Trades recorded for a strategy."""
        return self.trades.get(strategy_name, 0)


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Write JSON atomically, tolerating concurrent readers on Windows.

    os.replace fails with PermissionError while any reader holds the target
    file open, which happens whenever an external watchdog polls the state
    file, so retry with a short backoff before giving up.
    """
    temp_path: Path = path.with_name(path.name + ".tmp")
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.02 * (attempt + 1))
    if last_error is not None:
        raise last_error


def child_entry(config_path: str) -> None:
    """Multiprocessing entry point for the trading child process."""
    runner: AutonomousRunner = AutonomousRunner.from_config_file(config_path)
    runner.run_child()


def ensure_utf8_output() -> None:
    """Make stdout/stderr tolerate non-ASCII log and print output.

    Traders often run the supervisor from a cmd.exe console with GBK, and CI
    runners default to cp1252. Chinese log lines and print() calls from the
    core engine would raise UnicodeEncodeError there and take the whole
    process down, so switch the streams to UTF-8 when they are not already.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            encoding: str = str(getattr(stream, "encoding", "") or "")
            if encoding.lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        except Exception:   # noqa: BLE001
            # pytest capture objects and exotic streams may not support it.
            pass


class AutonomousRunner:
    """Supervisor and child-process orchestrator for autonomous trading."""

    def __init__(
        self,
        config: AutonomousConfig,
        config_path: Path | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Create a runner from an assembled configuration."""
        self.config: AutonomousConfig = config
        self.config_path: Path | None = config_path
        self.calendar: TradingCalendar = self._build_calendar(config)
        self.stop_event: threading.Event | None = stop_event

        self.state_path: Path = self._resolve_state_path(config)
        self.started_at: datetime = datetime.now()
        self.restarts_in_session: int = 0
        self._last_session_day: object = None

        self.main_engine: MainEngine | None = None
        self.event_engine: EventEngine | None = None
        self.guard: RiskGuard | None = None
        self.health: HealthMonitor = HealthMonitor(config.health)
        self.allocator: CapitalAllocator = CapitalAllocator(config.portfolio)
        self.ledger: TradeLedger = TradeLedger()
        self._last_parent_state: str = ""
        self._last_restart_limit_notice: datetime | None = None
        self._restart_limit_at: datetime | None = None

        self._strategy_engines: list[Any] = []
        self._stats_adapters: list[Any] = []
        self._last_strategy_pnl: dict[str, float] = {}
        self._gateway_last_event: dict[str, datetime] = {}
        self._last_reconnect_attempt: dict[str, datetime] = {}
        self._heartbeat_at: datetime | None = None
        self._status: str = "initialized"

    @classmethod
    def from_config_file(cls, config_path: str | Path | None = None) -> AutonomousRunner:
        """Build a runner by loading a config file."""
        path: Path | None = Path(config_path) if config_path else None
        config: AutonomousConfig = load_config(path)
        return cls(config, config_path=path)

    # ------------------------------------------------------------- calendar

    def _build_calendar(self, config: AutonomousConfig) -> TradingCalendar:
        """Assemble the trading calendar from configuration."""
        calendar_config: dict[str, object] = config.calendar
        profile_name: str = str(calendar_config.get("profile", "cn_futures"))
        night_end: str = str(calendar_config.get("night_end", "23:00"))

        sessions_raw: object = calendar_config.get("sessions")
        sessions: list[dict[str, str]] | None = None
        if isinstance(sessions_raw, list):
            sessions = []
            item: object
            for item in sessions_raw:
                if isinstance(item, dict):
                    sessions.append(
                        {
                            "name": str(item.get("name", "")),
                            "start": str(item.get("start", "")),
                            "end": str(item.get("end", "")),
                        }
                    )

        holidays_file: object = calendar_config.get("holidays_file")
        holidays: set | None = None
        if holidays_file:
            holidays = load_holidays(self._holiday_path(str(holidays_file)))

        profile = build_profile(
            profile_name=profile_name,
            sessions=sessions,
            holidays=holidays,
            night_end=night_end,
        )
        return TradingCalendar.from_profile(profile)

    def _holiday_path(self, holidays_file: str) -> Path:
        """Resolve a holidays file relative to the config or .vntrader folder."""
        candidate: Path = Path(holidays_file)
        if candidate.is_absolute():
            return candidate
        if self.config_path is not None:
            sibling: Path = self.config_path.parent / candidate
            if sibling.exists():
                return sibling
        trader_dir: Path = Path.cwd() / ".vntrader"
        fallback: Path = trader_dir / candidate
        return fallback

    def _resolve_state_path(self, config: AutonomousConfig) -> Path:
        """Resolve the runtime state file path."""
        state_file: str = config.runtime.state_file or "autonomous_state.json"
        candidate: Path = Path(state_file)
        if candidate.is_absolute():
            return candidate
        if self.config_path is not None:
            return self.config_path.parent / candidate
        return Path.cwd() / ".vntrader" / candidate

    # ---------------------------------------------------------------- parent

    def run_parent(self) -> None:
        """Run the supervisor loop (blocking)."""
        ensure_utf8_output()
        config: AutonomousConfig = self.config
        runtime: Any = config.runtime
        poll: float = runtime.poll_interval_secs

        logger.info("[autonomous] 父级守护进程启动")
        self._write_parent_state("supervising")

        child: multiprocessing.process.BaseProcess | None = None
        while True:
            try:
                if self.stop_event is not None and self.stop_event.is_set():
                    logger.info("[autonomous] 父级守护进程收到停止事件")
                    if child is not None and child.is_alive():
                        child.terminate()
                    if child is not None:
                        self._terminate_child(child)
                    self._write_parent_state("stopped")
                    return
                now: datetime = datetime.now()
                session_day: object = self.calendar.session_day(now)

                if session_day != self._last_session_day:
                    self._last_session_day = session_day
                    self.restarts_in_session = 0
                    self._restart_limit_at = None

                in_session: bool = session_day is not None

                if in_session:
                    if child is None or not child.is_alive():
                        if self.restarts_in_session >= runtime.max_restarts_per_session:
                            self._handle_restart_limit(runtime)
                            if child is not None:
                                child.join(timeout=5)
                                child = None
                        else:
                            logger.info("[autonomous] 启动交易子进程")
                            child = self._spawn_child()
                            self.restarts_in_session += 1
                            self._write_parent_state("child_running")
                    else:
                        self._check_child_state_freshness()
                else:
                    if child is not None:
                        if child.is_alive():
                            child.join(timeout=runtime.restart_backoff_secs)
                        if child.is_alive():
                            logger.warning("[autonomous] 子进程未能在时段结束后退出，强制结束")
                            self._terminate_child(child)
                        child = None
                        self.restarts_in_session = 0
                        self._write_parent_state("idle_between_sessions")

                time.sleep(poll)
            except KeyboardInterrupt:
                logger.info("[autonomous] 父级守护进程收到退出信号")
                if child is not None:
                    self._terminate_child(child)
                self._write_parent_state("stopped")
                return

    @staticmethod
    def _terminate_child(child: multiprocessing.process.BaseProcess) -> None:
        """Terminate a child and wait until it is really gone.

        A killed child may still hold the sqlite file handle for a moment on
        Windows; waiting avoids teardown races for the next session.
        """
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)

    def _spawn_child(self) -> multiprocessing.process.BaseProcess:
        """Spawn the trading child process."""
        self._write_parent_state("spawning_child")

        if self.config_path is not None:
            config_arg: str = str(self.config_path)
        else:
            config_arg = str(
                self._persist_temp_config()
            )

        ctx: multiprocessing.context.SpawnContext = multiprocessing.get_context("spawn")
        process: multiprocessing.process.BaseProcess = ctx.Process(
            target=child_entry,
            args=(config_arg,),
            name="vnpy-autonomous-child",
            daemon=True,
        )
        process.start()
        return process

    def _persist_temp_config(self) -> Path:
        """Persist the in-memory config so the child can reload it."""
        target: Path = self.state_path.parent / "autonomous.runtime.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, self.config.to_dict())
        return target

    def _check_child_state_freshness(self) -> None:
        """Warn when the child stops updating its state file."""
        if not self.state_path.exists():
            return
        age: float = (
            datetime.now() - datetime.fromtimestamp(self.state_path.stat().st_mtime)
        ).total_seconds()
        stall_limit: float = max(
            self.config.runtime.watchdog_stall_secs * 2,
            self.config.notification.heartbeat_secs * 2,
        )
        if age > stall_limit:
            self._notify(f"交易子进程心跳超时 {age:.0f} 秒，父进程将按重启策略处理")
            logger.warning("[autonomous] 子进程心跳文件超过 %.0f 秒未更新", age)

    # ----------------------------------------------------------------- child

    def run_child(self) -> None:
        """Run the trading process (blocking)."""
        ensure_utf8_output()
        self._status = "starting"
        self._write_state("starting")

        self.event_engine = EventEngine()
        self.main_engine = MainEngine(self.event_engine)

        self.guard = RiskGuard(
            self.main_engine,
            self.config.risk,
            notify=self._notify,
        )
        self.guard.install()
        self.guard.register_events(self.event_engine)

        self.health.set_pause_callback(self._pause_strategy)
        self.health.set_resume_callback(self._resume_strategy)
        self.event_engine.register(EVENT_TRADE, self._process_trade_event)
        self.event_engine.register(EVENT_ORDER, self._process_order_event)
        self.event_engine.register(EVENT_LOG, self._process_log_event)
        self.event_engine.register(EVENT_STRATEGY_SNAPSHOT, self._process_snapshot_event)
        self.event_engine.register_general(self._monitor_gateway_events)

        try:
            self._connect_gateways()
            self._add_apps()
            self._wait_for_contracts()
            self._run_session_loop()
        except KeyboardInterrupt:
            logger.info("[autonomous] 交易子进程收到退出信号")
        finally:
            self._shutdown_child()

    def _run_session_loop(self) -> None:
        """Main session loop: keep strategies running while the market is open."""
        runtime: Any = self.config.runtime
        poll: float = runtime.poll_interval_secs
        strategies_started: bool = False
        self._status = "trading"
        self._active_session_day: object = None

        while True:
            now: datetime = datetime.now()
            session: Session | None = self.calendar.current_session(now)
            session_day: object = self.calendar.session_day(now)

            if self.stop_event is not None and self.stop_event.is_set():
                if strategies_started and runtime.stop_strategies_on_exit:
                    self._stop_strategy_engines()
                    strategies_started = False
                self._write_state("stop_requested")
                logger.info("[autonomous] 收到停止事件，交易子进程准备退出")
                return

            if session is None:
                if strategies_started and runtime.stop_strategies_on_exit:
                    logger.info("[autonomous] 交易时段结束，停止策略")
                    if self.config.runtime.flat_on_session_end and self.guard is not None:
                        self.guard.flatten_positions()
                    self._stop_strategy_engines()
                    strategies_started = False
                    if self.config.notification.notify_on_session_end:
                        self._notify(
                            f"交易时段结束：当日成交 {self.ledger.total()} 笔，"
                            f"风控{'已武装' if self.guard is not None and self.guard.armed else '未触发'}，"
                            f"状态 {self._status}"
                        )
                self._write_state("session_ended")
                logger.info("[autonomous] 非交易时段，子进程退出，等待守护进程重启")
                return

            if not strategies_started:
                self._start_strategy_engines()
                strategies_started = True
                self._active_session_day = session_day
                self._on_session_start()
                if self.config.notification.notify_on_start:
                    self._notify(
                        f"交易时段 {session.name} 开始，策略已启动"
                    )
                self._write_state("strategies_started")
            elif session_day != self._active_session_day:
                # A new trading day began while strategies stay running
                # (overnight sessions keep the start day, so midnight does
                # not trigger a reset).
                self._active_session_day = session_day
                self._on_session_start()

            if self.guard is not None and self.guard.armed:
                self._write_state("risk_armed")

            self.health.resume_all_due()
            self._heartbeat(now)
            self._check_watchdog(now)
            self._publish_strategy_snapshots()

            time.sleep(poll)

    def _on_session_start(self) -> None:
        """Reset daily risk counters when a trading day begins."""
        if self.guard is not None:
            self.guard.reset_daily()
        logger.info("[autonomous] 新交易日开始，日内风控计数器已重置")

    def _close_engine_databases(self) -> None:
        """Release database handles held by strategy engines.

        On Windows an open sqlite handle blocks deleting or reopening the
        database file, which would slow down the next session spawn.
        """
        for _app_name, engine in self._strategy_engines:
            database: Any = getattr(engine, "database", None)
            inner: Any = getattr(database, "db", None)
            close: Any = getattr(inner, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:   # noqa: BLE001
                    logger.warning("[autonomous] 关闭数据库连接失败：%s", exc)

    def _connect_gateways(self) -> None:
        """Connect every configured gateway."""
        self._active_session_day = None
        main_engine: MainEngine = self._require_engine()
        name: str
        setting: dict[str, object]
        for name, setting in self.config.gateways.items():
            try:
                gateway_class: Any = resolve_gateway_class(name)
            except AppResolutionError as exc:
                logger.error("[autonomous] %s", exc)
                if self.config.notification.notify_on_error:
                    self._notify(str(exc))
                continue
            main_engine.add_gateway(gateway_class, name)
            main_engine.connect(setting, name)
            self._gateway_last_event[name] = datetime.now()
            logger.info("[autonomous] 交易接口 %s 已连接", name)

    def _add_apps(self) -> None:
        """Load every configured app and collect strategy engines."""
        main_engine: MainEngine = self._require_engine()
        app_name: str
        for app_name in self.config.apps:
            try:
                app_class: type[BaseApp] = resolve_app_class(app_name)
            except AppResolutionError as exc:
                logger.warning("[autonomous] %s", exc)
                if self.config.notification.notify_on_error:
                    self._notify(str(exc))
                continue
            try:
                engine = main_engine.add_app(app_class)
            except Exception as exc:   # noqa: BLE001
                logger.warning("[autonomous] 应用模块 %s 加载失败：%s", app_name, exc)
                if self.config.notification.notify_on_error:
                    self._notify(f"应用模块 {app_name} 加载失败：{exc}")
                continue
            if hasattr(engine, "init_engine") or hasattr(engine, "init_all_strategies"):
                self._strategy_engines.append((app_name, engine))
            strategies_attr: Any = getattr(engine, "strategies", None)
            if isinstance(strategies_attr, dict):
                from .adapters.cta import CtaStatsAdapter

                self._stats_adapters.append(
                    CtaStatsAdapter(engine, self._require_event_engine())
                )
            logger.info("[autonomous] 应用模块 %s 已加载", app_name)

    def _wait_for_contracts(self) -> None:
        """Wait until contracts arrive or the timeout expires."""
        main_engine: MainEngine = self._require_engine()
        deadline: float = time.monotonic() + self.config.runtime.contract_wait_secs
        while time.monotonic() < deadline:
            if main_engine.get_all_contracts():
                return
            time.sleep(1)
        logger.warning("[autonomous] 等待合约超时，策略初始化可能不完整")

    def _start_strategy_engines(self) -> None:
        """Provision, initialize and start every strategy engine."""
        runtime: Any = self.config.runtime
        engine: Any
        app_name: str
        for app_name, engine in self._strategy_engines:
            if hasattr(engine, "init_engine"):
                try:
                    engine.init_engine()
                except Exception as exc:   # noqa: BLE001
                    logger.warning(
                        "[autonomous] 引擎 %s 初始化失败：%s", app_name, exc
                    )
                    if self.config.notification.notify_on_error:
                        self._notify(f"引擎 {app_name} 初始化失败：{exc}")
            self._provision_strategies(app_name, engine)
            if hasattr(engine, "init_all_strategies"):
                try:
                    engine.init_all_strategies()
                except Exception as exc:   # noqa: BLE001
                    logger.warning(
                        "[autonomous] 引擎 %s 策略初始化失败：%s", app_name, exc
                    )

        if self._strategy_engines and runtime.strategy_init_wait_secs > 0:
            time.sleep(runtime.strategy_init_wait_secs)

        for _app_name, engine in self._strategy_engines:
            if hasattr(engine, "start_all_strategies"):
                engine.start_all_strategies()

    def _provision_strategies(self, app_name: str, engine: Any) -> None:
        """Auto-create strategies listed in app_settings through the engine API.

        Engines exposing ``add_strategy(class_name, strategy_name, vt_symbol,
        setting)`` receive one call per configured entry, so the whole
        portfolio is declared in a single config file. Entries already known
        to the engine are skipped, keeping restarts idempotent.
        """
        settings: dict[str, object] = self.config.app_settings.get(app_name, {})
        specs: object = settings.get("strategies")
        if not isinstance(specs, list):
            return

        add_strategy: Any = getattr(engine, "add_strategy", None)
        if not callable(add_strategy):
            logger.warning(
                "[autonomous] %s 引擎没有 add_strategy 接口，app_settings.strategies 已忽略",
                app_name,
            )
            return

        existing: Any = getattr(engine, "strategies", None)
        spec: object
        for spec in specs:
            if not isinstance(spec, dict):
                logger.warning("[autonomous] 策略配置条目不是字典：%r", spec)
                continue

            class_obj: object = spec.get("class_name")
            symbol_obj: object = spec.get("vt_symbol")
            if not isinstance(class_obj, str) or not isinstance(symbol_obj, str):
                logger.warning(
                    "[autonomous] 策略配置缺少 class_name 或 vt_symbol：%r", spec
                )
                continue

            name_obj: object = spec.get("name")
            if not isinstance(name_obj, str) or not name_obj:
                name_obj = self._default_strategy_name(class_obj, symbol_obj)

            setting: dict[str, object] = {}
            setting_obj: object = spec.get("setting")
            if isinstance(setting_obj, dict):
                setting = {str(key): value for key, value in setting_obj.items()}

            if isinstance(existing, dict) and name_obj in existing:
                continue

            try:
                add_strategy(class_obj, name_obj, symbol_obj, setting)
                logger.info(
                    "[autonomous] 自动装配策略 %s -> %s", name_obj, symbol_obj
                )
            except Exception as exc:   # noqa: BLE001
                logger.warning("[autonomous] 装配策略 %s 失败：%s", name_obj, exc)

    @staticmethod
    def _default_strategy_name(class_name: str, vt_symbol: str) -> str:
        """Derive a strategy name from its class and vt_symbol."""
        symbol: str = vt_symbol.split(".")[0]
        return f"{class_name}_{symbol}"

    def _stop_strategy_engines(self) -> None:
        """Stop every strategy engine."""
        engine: Any
        for _app_name, engine in self._strategy_engines:
            if hasattr(engine, "stop_all_strategies"):
                engine.stop_all_strategies()

    def _publish_strategy_snapshots(self) -> None:
        """Publish per-strategy snapshots for health and allocation."""
        adapter: Any
        for adapter in self._stats_adapters:
            publish: Any = getattr(adapter, "publish_snapshots", None)
            if callable(publish):
                try:
                    publish()
                except Exception as exc:   # noqa: BLE001
                    logger.warning("[autonomous] 策略快照发布失败：%s", exc)

    def _require_event_engine(self) -> EventEngine:
        """Return the live event engine or raise."""
        if self.event_engine is None:
            raise RuntimeError("EventEngine 尚未创建")
        return self.event_engine

    def _pause_strategy(self, strategy_name: str, reason: str) -> None:
        """Pause one strategy through its engine."""
        logger.warning("[autonomous] 策略 %s 健康熔断：%s", strategy_name, reason)
        if self.config.notification.notify_on_breach:
            self._notify(f"策略 {strategy_name} 健康熔断：{reason}")
        self._invoke_strategy_method(strategy_name, "stop_strategy")
        self._write_state("strategy_halted")

    def _resume_strategy(self, strategy_name: str) -> None:
        """Resume one strategy through its engine."""
        logger.info("[autonomous] 策略 %s 冷却结束，恢复运行", strategy_name)
        self._invoke_strategy_method(strategy_name, "start_strategy")
        self._write_state("strategy_resumed")

    def _invoke_strategy_method(self, strategy_name: str, method_name: str) -> None:
        """Call a per-strategy method on engines that provide it."""
        engine: Any
        for _app_name, engine in self._strategy_engines:
            method: Any = getattr(engine, method_name, None)
            if callable(method):
                try:
                    method(strategy_name)
                except Exception as exc:   # noqa: BLE001
                    logger.warning(
                        "[autonomous] 调用 %s(%s) 失败：%s",
                        method_name, strategy_name, exc,
                    )

    # ---------------------------------------------------------------- events

    def _process_trade_event(self, event: Event) -> None:
        """Record trades by strategy reference into the ledger."""
        trade: TradeData = event.data
        strategy_name: str = "unknown"

        # Authoritative route: strategy engines keep vt_orderid -> strategy.
        engine: Any
        for _app_name, engine in self._strategy_engines:
            mapping: Any = getattr(engine, "orderid_strategy_map", None)
            if isinstance(mapping, dict) and trade.vt_orderid in mapping:
                strategy: Any = mapping[trade.vt_orderid]
                name: Any = getattr(strategy, "strategy_name", None)
                if isinstance(name, str):
                    strategy_name = name
                    break

        if strategy_name == "unknown":
            main_engine: MainEngine = self._require_engine()
            order: OrderData | None = main_engine.get_order(trade.vt_orderid)
            if order is not None and order.reference:
                strategy_name = self._strip_app_reference(order.reference)

        now: datetime = trade.datetime or datetime.now()
        self.ledger.record(strategy_name, now)
        if strategy_name not in self.health.snapshot():
            self.health.register_strategy(strategy_name)

    def _strip_app_reference(self, reference: str) -> str:
        """Strip the app_name_ prefix strategy engines put on order references."""
        app_name: str
        for app_name in self.config.apps:
            prefix: str = f"{app_name}_"
            if reference.startswith(prefix):
                return reference[len(prefix):]
        return reference

    def _process_order_event(self, event: Event) -> None:
        """Track rejected orders as strategy errors."""
        order: OrderData = event.data
        if order.status == Status.REJECTED:
            strategy_name: str = self._strip_app_reference(
                order.reference or "unknown"
            )
            self.health.record_error(strategy_name)

    def _process_log_event(self, event: Event) -> None:
        """Count error-level log entries against the global bucket."""
        log: LogData = event.data
        if log.level >= 40:   # ERROR and above
            self.health.record_error("__global__")

    def _process_snapshot_event(self, event: Event) -> None:
        """Feed strategy snapshots into health and allocation."""
        snapshot: StrategySnapshot = event.data
        self.health.register_strategy(snapshot.strategy_name)
        self.allocator.register_strategy(snapshot.strategy_name)
        if snapshot.equity is not None:
            self.health.record_equity(snapshot.strategy_name, snapshot.equity)
        if snapshot.trades is not None:
            health = self.health.get_health(snapshot.strategy_name)
            if health is not None and health.trades < snapshot.trades:
                self.health.record_trade(
                    snapshot.strategy_name,
                    snapshot.realized_pnl or 0.0,
                )
        if snapshot.error:
            self.health.record_error(snapshot.strategy_name)
        if snapshot.equity is not None:
            self.allocator.record_equity(snapshot.strategy_name, snapshot.equity)
            self._record_pnl_delta(snapshot)

    def _record_pnl_delta(self, snapshot: StrategySnapshot) -> None:
        """Record realized pnl changes as single health trades."""
        if snapshot.realized_pnl is None:
            return

        name: str = snapshot.strategy_name
        last: float | None = self._last_strategy_pnl.get(name)
        self._last_strategy_pnl[name] = snapshot.realized_pnl
        if last is None:
            # First observation only seeds the baseline.
            return

        delta: float = snapshot.realized_pnl - last
        if abs(delta) < 1e-9:
            return
        self.health.record_trade(name, delta)

    def _monitor_gateway_events(self, event: Event) -> None:
        """Track last event time per gateway for the watchdog."""
        data: object = event.data
        gateway_name: str | None = getattr(data, "gateway_name", None)
        if isinstance(gateway_name, str) and gateway_name:
            self._gateway_last_event[gateway_name] = datetime.now()

    # -------------------------------------------------------------- watchdog

    def _check_watchdog(self, now: datetime) -> None:
        """Re-alert or reconnect gateways that stopped sending events."""
        stall_limit: float = self.config.runtime.watchdog_stall_secs
        gateway_name: str
        for gateway_name in self.config.gateways:
            last: datetime | None = self._gateway_last_event.get(gateway_name)
            if last is None:
                continue
            age: float = (now - last).total_seconds()
            if age <= stall_limit:
                continue

            if not self.config.runtime.auto_reconnect:
                self._notify(
                    f"交易接口 {gateway_name} 已 {age:.0f} 秒无事件，请检查连接"
                )
                continue

            last_attempt: datetime | None = self._last_reconnect_attempt.get(gateway_name)
            if last_attempt is not None and (now - last_attempt).total_seconds() < stall_limit:
                continue

            logger.warning(
                "[autonomous] 交易接口 %s 事件停滞 %.0f 秒，尝试重连", gateway_name, age
            )
            self._notify(
                f"交易接口 {gateway_name} 事件停滞 {age:.0f} 秒，尝试重连"
            )
            self._last_reconnect_attempt[gateway_name] = now
            main_engine: MainEngine = self._require_engine()
            try:
                main_engine.connect(self.config.gateways[gateway_name], gateway_name)
            except Exception as exc:   # noqa: BLE001
                logger.error("[autonomous] 重连 %s 失败：%s", gateway_name, exc)

    # ------------------------------------------------------------ lifecycle

    def _heartbeat(self, now: datetime) -> None:
        """Write state and optionally notify on the heartbeat interval."""
        interval: int = self.config.notification.heartbeat_secs
        if self._heartbeat_at is None:
            self._heartbeat_at = now
            return
        if (now - self._heartbeat_at).total_seconds() < interval:
            return
        self._heartbeat_at = now
        self._write_state("heartbeat")
        session: Session | None = self.calendar.current_session(now)
        if self.config.notification.heartbeat_secs > 0 and session is not None:
            self._notify(
                f"心跳：时段 {session.name}，状态 {self._status}，"
                 f"当日成交 {self.ledger.total()} 笔"
            )

    def _shutdown_child(self) -> None:
        """Tear down the child process engines."""
        main_engine: MainEngine | None = self.main_engine
        if main_engine is not None:
            if self.config.runtime.stop_strategies_on_exit:
                self._stop_strategy_engines()
            self._write_state("stopping")
            self._close_engine_databases()
            main_engine.close()
        event_engine: EventEngine | None = self.event_engine
        if event_engine is not None:
            event_engine.stop()
        self._status = "stopped"
        self._write_state("stopped")

    # ---------------------------------------------------------------- state

    def _write_parent_state(self, status: str) -> None:
        """Write parent-level state only when the status actually changes."""
        if self._last_parent_state == status:
            return
        self._last_parent_state = status
        if status == "restart_limit":
            now: datetime = datetime.now()
            last: datetime | None = self._last_restart_limit_notice
            if last is not None:
                cooldown: float = max(
                    self.config.runtime.restart_backoff_secs,
                    self.config.notification.heartbeat_secs,
                )
                if (now - last).total_seconds() < cooldown:
                    return
            self._last_restart_limit_notice = now
            self._notify(
                f"交易子进程在同一交易时段内重启 {self.restarts_in_session} 次，"
                "已达上限，等待下一个交易时段"
            )
        self._write_state(status, parent=True)

    def _handle_restart_limit(self, runtime: Any) -> None:
        """Pause spawning after too many restarts, then recover by cooldown.

        Without recovery a burst of early crashes (bad config, feed outage at
        session start) would idle the supervisor for the rest of the trading
        day and silently miss every session.
        """
        now: datetime = datetime.now()
        if self._restart_limit_at is None:
            self._restart_limit_at = now
            self._write_parent_state("restart_limit")
            logger.warning(
                "[autonomous] 同一交易时段内重启达到 %d 次上限，进入冷却",
                runtime.max_restarts_per_session,
            )
            return

        cooldown: float = max(float(runtime.restart_backoff_secs), 30.0)
        if (now - self._restart_limit_at).total_seconds() < cooldown:
            return

        self.restarts_in_session = 0
        self._restart_limit_at = None
        logger.info("[autonomous] 重启冷却结束，重置计数并重试拉起子进程")
        self._notify("交易子进程重启冷却结束，已重置重启计数并重试")

    def _state_payload(self, status: str, parent: bool = False) -> dict[str, object]:
        """Assemble the runtime state payload."""
        now: datetime = datetime.now()
        session: Session | None = self.calendar.current_session(now)
        session_day: date | None = self.calendar.session_day(now)

        payload: dict[str, object] = {
             "pid": os.getpid(),
            "parent": parent,
            "mode": self.config.mode,
            "status": status,
            "session": session.name if session else None,
            "session_day": session_day.isoformat() if session_day else None,
            "calendar_profile": str(self.config.calendar.get("profile", "cn_futures")),
            "started_at": self.started_at.isoformat(),
            "updated_at": now.isoformat(),
            "uptime_secs": round((now - self.started_at).total_seconds(), 1),
            "next_open": self.calendar.next_open(now).isoformat(),
            "restarts_in_session": self.restarts_in_session,
        }

        if self.guard is not None:
            payload["guard"] = self.guard.snapshot()
        payload["health"] = self.health.snapshot()
        payload["portfolio"] = self.allocator.snapshot()
        payload["ledger"] = dict(self.ledger.trades)
        payload["strategy_engines"] = len(self._strategy_engines)
        payload["gateway_last_event"] = {
            name: value.isoformat() for name, value in self._gateway_last_event.items()
        }
        return payload

    def _write_state(self, status: str, parent: bool = False) -> None:
        """Persist the runtime state file."""
        self._status = status
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, object] = self._state_payload(status, parent)
            atomic_write_json(self.state_path, payload)
        except OSError as exc:
            logger.warning("[autonomous] 状态文件写入失败：%s", exc)

    def _notify(self, message: str) -> None:
        """Send a notification through the main engine when available."""
        logger.info("[autonomous] %s", message)
        main_engine: MainEngine | None = self.main_engine
        if main_engine is not None:
            try:
                email_server: object = SETTINGS.get("email.server")
                if email_server:
                    main_engine.send_notification(
                        message, subject="VeighNa Autonomous Trading"
                    )
                    self._send_webhook(message)
                    return

                # 未配置邮件时 send_notification 仍会拉起 SMTP 线程，且
                # EmailEngine.close() 会 join 它，拖慢时段切换；此时仅走
                # 已绑定的微信通道。
                wechat: Any = main_engine.get_engine("wechat")
                if (
                    wechat is not None
                    and getattr(wechat, "creds", None)
                    and getattr(wechat, "user_id", None)
                ):
                    wechat.send_wechat(
                        f"VeighNa Autonomous Trading\n{message}"
                    )
                    self._send_webhook(message)
                    return
            except Exception as exc:   # noqa: BLE001
                logger.warning("[autonomous] 通知发送失败：%s", exc)
        # Webhook is independent of the main engine, so the parent supervisor
        # can push restarts, watchdog alerts and session changes even when
        # neither email nor wechat is configured.
        self._send_webhook(message)

    def _send_webhook(self, message: str) -> None:
        """POST the message to the configured webhook endpoint, if any."""
        webhook_url: str = self.config.notification.webhook_url
        if not webhook_url:
            return
        try:
            import requests

            response = requests.post(
                webhook_url,
                json={
                    "source": "vnpy.autonomous",
                    "mode": self.config.mode,
                    "message": message,
                },
                timeout=5,
            )
            if response.status_code >= 400:
                logger.warning(
                    "[autonomous] webhook 返回 %s：%s",
                    response.status_code,
                    response.text[:200],
                )
        except Exception as exc:   # noqa: BLE001
            logger.warning("[autonomous] webhook 发送失败：%s", exc)

    def _require_engine(self) -> MainEngine:
        """Return the live main engine or raise."""
        if self.main_engine is None:
            raise RuntimeError("MainEngine 尚未创建")
        return self.main_engine

    # ------------------------------------------------------------------- run

    def run(self, role: str = "parent") -> None:
        """Run in the requested role: parent supervisor or child trader."""
        if role == "child":
            self.run_child()
        else:
            self.run_parent()


def read_state(path: str | Path) -> dict[str, object]:
    """Read a runtime state file, returning an empty dict when missing."""
    state_path: Path = Path(path)
    for attempt in range(5):
        try:
            with open(state_path, encoding="utf-8") as file:
                payload: object = json.load(file)
            break
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            # The writer swaps the file atomically; a reader can hit the
            # brief window on Windows, so retry before giving up.
            if attempt == 4:
                return {}
            time.sleep(0.01 * (attempt + 1))
    if isinstance(payload, dict):
        return {str(k): v for k, v in payload.items()}
    return {}
