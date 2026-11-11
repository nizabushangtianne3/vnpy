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

    optimize_parser: argparse.ArgumentParser = subparsers.add_parser(
        "optimize", help="参数网格上重复回测并输出排序结果"
    )
    _add_config_argument(optimize_parser)
    optimize_parser.add_argument("--strategy", default="AutonomousTrendStrategy", help="策略类名")
    optimize_parser.add_argument("--strategy-file", default="", help="策略文件，默认发自 examples/autonomous/strategies")
    optimize_parser.add_argument("--vt-symbol", default="rb9999.SHFE", help="合约代码")
    optimize_parser.add_argument("--grid", required=True, help="参数网格，如 fast_window=5,10;slow_window=20,40")
    optimize_parser.add_argument("--top", type=int, default=10, help="输出前 N 个结果")
    optimize_parser.add_argument("--csv", default="", help="从CSV读取K线")
    optimize_parser.add_argument("--exchange", default="SHFE", help="交易所，仅 CSV 使用")
    optimize_parser.add_argument("--interval", default="MINUTE", help="K线周期，仅 CSV 使用")
    optimize_parser.add_argument("--start", default="", help="数据库起始时间")
    optimize_parser.add_argument("--end", default="", help="数据库结束时间")
    optimize_parser.add_argument("--slippage", type=float, default=0.5, help="滑点")
    optimize_parser.add_argument("--rate", type=float, default=0.0001, help="手续费率")
    optimize_parser.add_argument("--size", type=int, default=10, help="合约乘数")
    optimize_parser.add_argument("--capital", type=float, default=1000000.0, help="初始资金")
    optimize_parser.add_argument("--bars", type=int, default=300, help="合成K线数量")
    optimize_parser.add_argument("--seed", type=int, default=7, help="随机种子")
    backtest_parser: argparse.ArgumentParser = subparsers.add_parser(
        "backtest", help="离线回测一个策略并输出绩效"
    )
    _add_config_argument(backtest_parser)
    backtest_parser.add_argument("--strategy", default="AutonomousTrendStrategy", help="策略类名")
    backtest_parser.add_argument("--strategy-file", default="", help="策略文件，默认发自 examples/autonomous/strategies")
    backtest_parser.add_argument("--vt-symbol", default="rb9999.SHFE", help="合约代码")
    backtest_parser.add_argument("--bars", type=int, default=200, help="生成K线数量")
    backtest_parser.add_argument("--seed", type=int, default=7, help="随机种子")
    backtest_parser.add_argument("--csv", default="", help="从CSV读取K线（datetime,open,high,low,close[,volume]），给定后忽略合成数据")
    backtest_parser.add_argument("--exchange", default="SHFE", help="交易所名称，仅 --csv 使用")
    backtest_parser.add_argument("--interval", default="MINUTE", help="K线周期，仅 --csv 使用")
    backtest_parser.add_argument("--start", default="", help="起止时间，仅数据库数据源使用，如 2024-01-01")
    backtest_parser.add_argument("--end", default="", help="结束时间，仅数据库数据源使用")
    backtest_parser.add_argument("--slippage", type=float, default=0.5, help="滑点")
    backtest_parser.add_argument("--rate", type=float, default=0.0001, help="手续费率")
    backtest_parser.add_argument("--size", type=int, default=10, help="合约倍数")
    backtest_parser.add_argument("--capital", type=float, default=1000000.0, help="初始资金")
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


def command_backtest(args: argparse.Namespace) -> int:
    """Drive a reporting strategy through bars and report its edge."""
    try:
        from .backtest import BacktestSettings, build_bar, run_backtest
    except ImportError as exc:
        print("错误：" + str(exc))
        return 3

    import importlib.util
    import math
    import random
    from datetime import datetime, timedelta

    if args.strategy_file:
        strategy_path: Path = Path(args.strategy_file)
    else:
        strategy_path = (
            Path(__file__).resolve().parents[2]
            / "examples"
            / "autonomous"
            / "strategies"
            / "autonomous_trend_strategy.py"
        )

    spec = importlib.util.spec_from_file_location("backtest_strategy", strategy_path)
    if spec is None or spec.loader is None:
        print(f"错误：无法加载策略文件 {strategy_path}")
        return 2
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        strategy_class: type = getattr(module, args.strategy)
    except AttributeError:
        print(f"错误：{args.strategy} 不在 {strategy_path} 中")
        return 2

    # Prefer real data when the caller supplies it. A synthetic series only
    # proves the plumbing works, so the CSV and database paths are the ones
    # that can support a real promotion decision.
    from .backtest import (
        load_bars_from_csv,
        load_bars_from_database,
        parse_exchange,
        parse_interval,
    )

    if args.csv:
        bars = load_bars_from_csv(
            args.csv,
            args.vt_symbol.split(".")[0],
            exchange=parse_exchange(args.exchange),
            interval=parse_interval(args.interval),
        )
        print(f"数据来源：CSV {args.csv}，共 {len(bars)} 根K线")
    elif args.start or args.end:
        start = datetime.fromisoformat(args.start) if args.start else datetime(2024, 1, 1)
        end = (
            datetime.fromisoformat(args.end) if args.end else datetime.now()
        )
        bars = load_bars_from_database(
            args.vt_symbol.split(".")[0],
            parse_exchange(args.exchange),
            parse_interval(args.interval),
            start,
            end,
        )
        print(f"数据来源：数据库 {args.vt_symbol}，共 {len(bars)} 根K线")
    else:
        import math
        import random

        random.seed(args.seed)
        base: datetime = datetime(2024, 1, 2, 9, 0)
        price: float = 3500.0
        bars = []
        for index in range(max(args.bars, 60)):
            close: float = (
                price
                + 4.0 * math.sin(index / 45.0)
                + 0.4
                + (random.random() - 0.5) * 5.0
            )
            open_price: float = price
            bars.append(
                build_bar(
                    base + timedelta(minutes=index),
                    open_price,
                    max(open_price, close) + random.random() * 4.0,
                    min(open_price, close) - random.random() * 4.0,
                    close,
                )
            )
            price = close
        print(f"数据来源：合成行情（{len(bars)} 根，seed={args.seed}）")

    strategy = strategy_class(
        cta_engine=None,
        strategy_name="backtest",
        vt_symbol=args.vt_symbol,
        setting={},
    )
    result = run_backtest(
        strategy,
        bars,
        BacktestSettings(
            slippage=args.slippage,
            rate=args.rate,
            size=args.size,
            capital=args.capital,
        ),
    )

    print("回测结果：")
    for key, value in result.summary().items():
        print(f"  {key:16} {value}")

    if result.total_pnl <= 0:
        print("结论：该策略在此数据上未取得正期望。")
        return 1
    return 0


def _parse_grid(spec: str) -> list[dict[str, int | float]]:
    """Expand ``fast_window=5,10;slow_window=20,40`` into a list of settings."""
    settings_list: list[dict[str, int | float]] = [{}]
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"网格片段缺少等号：{chunk}")
        name, raw_values = chunk.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"网格片段缺少参数名：{chunk}")
        values: list[int | float] = []
        for token in raw_values.split(","):
            token = token.strip()
            if not token:
                continue
            numeric: int | float = (
                int(token) if token.lstrip("-").isdigit() else float(token)
            )
            values.append(numeric)
        if not values:
            raise ValueError(f"参数 {name} 没有可用取值")
        settings_list = [
            {**current, name: value}
            for current in settings_list
            for value in values
        ]
    if not settings_list:
        raise ValueError("参数网格为空")
    return settings_list


def command_optimize(args: argparse.Namespace) -> int:
    """Run the same strategy over a parameter grid and rank the results."""
    try:
        from .backtest import BacktestSettings, build_bar, run_backtest
    except ImportError as exc:
        print("错误：" + str(exc))
        return 3

    import importlib.util
    import math
    import random
    from datetime import datetime, timedelta

    if args.strategy_file:
        strategy_path: Path = Path(args.strategy_file)
    else:
        strategy_path = (
            Path(__file__).resolve().parents[2]
            / "examples"
            / "autonomous"
            / "strategies"
            / "autonomous_trend_strategy.py"
        )

    spec = importlib.util.spec_from_file_location("optimize_strategy", strategy_path)
    if spec is None or spec.loader is None:
        print(f"错误：无法加载策略文件 {strategy_path}")
        return 2
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        strategy_class: type = getattr(module, args.strategy)
    except AttributeError:
        print(f"错误：{args.strategy} 不在 {strategy_path} 中")
        return 2

    from .backtest import (
        load_bars_from_csv,
        load_bars_from_database,
        parse_exchange,
        parse_interval,
    )

    if args.csv:
        bars = load_bars_from_csv(
            args.csv,
            args.vt_symbol.split(".")[0],
            exchange=parse_exchange(args.exchange),
            interval=parse_interval(args.interval),
        )
        print(f"数据来源：CSV {args.csv}，共 {len(bars)} 根K线")
    elif args.start or args.end:
        start = datetime.fromisoformat(args.start) if args.start else datetime(2024, 1, 1)
        end = datetime.fromisoformat(args.end) if args.end else datetime.now()
        bars = load_bars_from_database(
            args.vt_symbol.split(".")[0],
            parse_exchange(args.exchange),
            parse_interval(args.interval),
            start,
            end,
        )
        print(f"数据来源：数据库 {args.vt_symbol}，共 {len(bars)} 根K线")
    else:
        random.seed(args.seed)
        base = datetime(2024, 1, 2, 9, 0)
        price = 3500.0
        bars = []
        for index in range(max(args.bars, 60)):
            close = (
                price
                + 4.0 * math.sin(index / 45.0)
                + 0.4
                + (random.random() - 0.5) * 5.0
            )
            open_price = price
            bars.append(
                build_bar(
                    base + timedelta(minutes=index),
                    open_price,
                    max(open_price, close) + random.random() * 4.0,
                    min(open_price, close) - random.random() * 4.0,
                    close,
                )
            )
            price = close
        print(f"数据来源：合成行情（{len(bars)} 根，seed={args.seed}）")

    try:
        grid = _parse_grid(args.grid)
    except ValueError as exc:
        print("错误：" + str(exc))
        return 2

    print(f"参数组合数：{len(grid)}")

    rows: list[dict] = []
    for settings_item in grid:
        strategy = strategy_class(
            cta_engine=None,
            strategy_name="optimize",
            vt_symbol=args.vt_symbol,
            setting=dict(settings_item),
        )
        result = run_backtest(
            strategy,
            bars,
            BacktestSettings(
                slippage=args.slippage,
                rate=args.rate,
                size=args.size,
                capital=args.capital,
            ),
        )
        rows.append(
            {
                "params": dict(settings_item),
                "total_pnl": result.total_pnl,
                "sharpe": result.sharpe,
                "max_drawdown": result.max_drawdown,
                "trade_count": result.trade_count,
                "win_rate": result.win_rate,
            }
        )

    rows.sort(key=lambda item: item["sharpe"], reverse=True)
    print(f"{'排名':>4} {'sharpe':>8} {'total_pnl':>12} {'max_dd':>10} {'trades':>8} {'win_rate':>9}  params")
    for rank, row in enumerate(rows[: max(args.top, 1)], start=1):
        params_text = ",".join(f"{k}={v}" for k, v in row["params"].items())
        print(
            f"{rank:>4} {row['sharpe']:>8.3f} {row['total_pnl']:>12.2f}"
            f" {row['max_drawdown']:>10.2f} {row['trade_count']:>8}"
            f" {row['win_rate']:>9.3f}  {params_text}"
        )

    if not rows:
        print("错误：没有可用的回测结果")
        return 2
    best = rows[0]
    if best["total_pnl"] <= 0:
        print("结论：最优参数组合仍未取得正期望。")
        return 1
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
        "backtest": command_backtest,
        "optimize": command_optimize,
    }
    handler = handlers[args.command]
    try:
        return int(handler(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
