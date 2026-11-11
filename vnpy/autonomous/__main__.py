"""Command line entry: ``python -m vnpy.autonomous``.

Subcommands:

* run      start the supervised autonomous trading process
* check    validate a config file and print the calendar summary
* sessions print the next session openings
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .calendar import PROFILE_NAMES
from .config import default_config_path, load_config, validate_config
from .runner import APP_REGISTRY, GATEWAY_REGISTRY, AutonomousRunner


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    """Add the shared --config option."""
    parser.add_argument(
        "--config",
        "-c",
        default=str(default_config_path()),
        help="配置文件路径，默认为 .vntrader/autonomous.json",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="python -m vnpy.autonomous",
        description="VeighNa 自主交易运行器",
    )
    subparsers: argparse._SubParsersAction = parser.add_subparsers(dest="command")

    run_parser: argparse.ArgumentParser = subparsers.add_parser("run", help="启动自主交易进程")
    _add_config_argument(run_parser)
    run_parser.add_argument(
        "--role",
        choices=["parent", "child"],
        default="parent",
        help="运行角色，默认 parent（守护进程）",
    )

    check_parser: argparse.ArgumentParser = subparsers.add_parser("check", help="校验配置与日历")
    _add_config_argument(check_parser)

    sessions_parser: argparse.ArgumentParser = subparsers.add_parser(
        "sessions", help="打印后续交易时段"
    )
    _add_config_argument(sessions_parser)
    sessions_parser.add_argument("--count", type=int, default=5, help="展示时段数量")

    return parser


def _load_runner(config_path: str) -> AutonomousRunner:
    """Load config and build a runner."""
    config = load_config(config_path)
    errors: list[str] = validate_config(config)
    if errors:
        raise ValueError("配置校验失败：" + "；".join(errors))
    return AutonomousRunner(config, config_path=Path(config_path))


def command_run(args: argparse.Namespace) -> int:
    """Run the autonomous process in the requested role."""
    runner: AutonomousRunner = _load_runner(args.config)
    print(f"启动自主交易进程：role={args.role}，mode={runner.config.mode}")
    runner.run(role=args.role)
    return 0


def command_check(args: argparse.Namespace) -> int:
    """Validate the config file and print a calendar summary."""
    runner: AutonomousRunner = _load_runner(args.config)
    now: datetime = datetime.now()
    print("配置校验通过")
    print(f"交易日历：profile={runner.config.calendar.get('profile', 'cn_futures')}")
    print(f"当前是否在交易时段：{runner.calendar.is_open(now)}")
    print(f"下一个开盘时间：{runner.calendar.next_open(now)}")
    print(f"配置的交易接口：{list(runner.config.gateways)}")
    print(f"配置的应用模块：{runner.config.apps}")
    print(f"风控参数：{runner.config.risk.to_dict()}")
    return 0


def command_sessions(args: argparse.Namespace) -> int:
    """Print the next session openings."""
    runner: AutonomousRunner = _load_runner(args.config)
    print(f"支持的日历模板：{', '.join(PROFILE_NAMES)}")
    print(f"内置接口：{', '.join(GATEWAY_REGISTRY)}")
    print(f"内置模块：{', '.join(APP_REGISTRY)}")
    current: datetime = datetime.now()
    for _ in range(max(args.count, 1)):
        next_open: datetime = runner.calendar.next_open(current)
        print(f"开盘时间：{next_open}")
        current = next_open
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    from .runner import ensure_utf8_output

    ensure_utf8_output()
    parser: argparse.ArgumentParser = build_parser()
    args: argparse.Namespace = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "run": command_run,
        "check": command_check,
        "sessions": command_sessions,
    }
    handler = handlers[args.command]
    try:
        return int(handler(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
