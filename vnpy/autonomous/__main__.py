"""Command line entry: ``python -m vnpy.autonomous``.

Subcommands:

* run      start the supervised autonomous trading process
* check    validate a config file and print the calendar summary
* sessions print the next session openings
"""

from __future__ import annotations

import argparse
import json
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

    status_parser: argparse.ArgumentParser = subparsers.add_parser(
        "status", help="查看自主交易运行状态与风控快照"
    )
    _add_config_argument(status_parser)
    status_parser.add_argument("--follow", action="store_true", help="持续刷新状态，每秒刷新一次")
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
    optimize_parser.add_argument("--size", type=float, default=10, help="合约乘数")
    optimize_parser.add_argument("--capital", type=float, default=1000000.0, help="初始资金")
    optimize_parser.add_argument("--bars", type=int, default=300, help="合成K线数量")
    optimize_parser.add_argument("--seed", type=int, default=7, help="随机种子")
    optimize_parser.add_argument("--train-bars", type=int, default=0, help="walk-forward 训练段长度，设置后启用样本外验证")
    optimize_parser.add_argument("--test-bars", type=int, default=0, help="walk-forward 测试段长度，设置后启用样本外验证")
    optimize_parser.add_argument("--output", default="", help="把回测/优化结果写入 JSON 文件")
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
    backtest_parser.add_argument("--size", type=float, default=10, help="合约乘数")
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


def _pick_best_params(rows: list[dict]) -> dict:
    """Choose the parameter set to carry into the out-of-sample window.

    Ranking by Sharpe alone can lift a combination that lost money on the
    training window: a curve with few trades and a smooth drift scores a
    respectable Sharpe even while its total pnl is negative, and the whole
    fold then validates a losing configuration. Profitable combinations rank
    first; only when every combination loses does Sharpe decide, so the fold
    still reports something for an operator to review.

    Profit factor breaks ties inside the profitable pool. Two combinations
    with the same pnl can differ by an order of magnitude in how repeatable
    that pnl is: one big win against many small losses is a lottery ticket,
    while many modest wins is an edge that survives a different window, and
    only the repeatable half of that pair is worth carrying out of sample.

    Sharpe is the first term in the profitable pool because it is what
    transfers out of sample. Real-data walk-forward showed the pnl-first
    rule selecting raw, unprotected parameters every fold, while the
    Sharpe-first rule landed on a trailing stop and cut out-of-sample
    drawdown by more than half at the same number of profitable folds.
    Profit-based pay is not what carries forward; risk-adjusted pay is.
    """
    if not rows:
        raise ValueError("没有可用的训练段回测结果")

    profitable: list[dict] = [row for row in rows if row["total_pnl"] > 0]
    pool: list[dict] = profitable or rows
    pool.sort(
        key=lambda item: (item["sharpe"], item.get("profit_factor", 0.0)),
        reverse=True,
    )
    params: dict = pool[0]["params"]
    return params


def _selected_train_row(train_rows: list[dict], best_config: dict) -> dict:
    """Return the train metrics of the configuration this fold selected.

    Train rows are produced in grid order, so ``train_rows[0]`` describes
    whichever combination was listed first rather than the one carried out of
    sample. Reporting it would pair an in-sample number with an out-of-sample
    number from two different parameter sets, and the degradation a reader is
    trying to judge becomes meaningless.
    """
    for row in train_rows:
        if row["params"] == best_config:
            return row
    return train_rows[0]


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

    walk_forward: bool = args.train_bars > 0 and args.test_bars > 0
    if (args.train_bars > 0) != (args.test_bars > 0):
        print("错误：--train-bars 与 --test-bars 必须同时设置")
        return 2
    if walk_forward and args.train_bars < 30:
        print("错误：--train-bars 至少 30 根才能完成策略预热")
        return 2
    if walk_forward and args.test_bars < 10:
        print("错误：--test-bars 至少 10 根才有统计意义")
        return 2

    def _run_one(config: dict[str, int | float], bars_slice: list) -> dict:
        strategy = strategy_class(
            cta_engine=None,
            strategy_name="optimize",
            vt_symbol=args.vt_symbol,
            setting=dict(config),
        )
        result = run_backtest(
            strategy,
            bars_slice,
            BacktestSettings(
                slippage=args.slippage,
                rate=args.rate,
                size=args.size,
                capital=args.capital,
            ),
        )
        return {
            "params": dict(config),
            "total_pnl": result.total_pnl,
            "sharpe": result.sharpe,
            "max_drawdown": result.max_drawdown,
            "trade_count": result.trade_count,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
        }

    print(f"参数组合数：{len(grid)}")
    rows: list[dict] = []
    if not walk_forward:
        for config in grid:
            rows.append(_run_one(config, bars))
        rows.sort(key=lambda item: item["sharpe"], reverse=True)
        header = (
            f"{'排名':>4} {'sharpe':>8} {'total_pnl':>12} {'profit_f':>9}"
            f" {'max_dd':>10} {'trades':>8} {'win_rate':>9}  params"
        )
        print(header)
        for rank, row in enumerate(rows[: max(args.top, 1)], start=1):
            params_text = ",".join(f"{k}={v}" for k, v in row["params"].items())
            print(
                f"{rank:>4} {row['sharpe']:>8.3f} {row['total_pnl']:>12.2f}"
                f" {row['profit_factor']:>9.3f} {row['max_drawdown']:>10.2f}"
                f" {row['trade_count']:>8} {row['win_rate']:>9.3f}  {params_text}"
            )
        best_params: dict = _pick_best_params(rows) if rows else {}
        best: dict | None = next(
            (row for row in rows if row["params"] == best_params), None
        )
    else:
        print(
            f"walk-forward 验证：训练 {args.train_bars} 根，"
            f"测试 {args.test_bars} 根，总共 {len(bars)} 根K线"
        )
        if len(bars) < args.train_bars + args.test_bars:
            print(
                "错误：K线不足以组成一个完整的 walk-forward 折叠，"
                f"需要至少 {args.train_bars + args.test_bars} 根，"
                f"当前只有 {len(bars)} 根"
            )
            return 2
        window_start: int = 0
        fold_results: list[dict] = []
        while window_start + args.train_bars + args.test_bars <= len(bars):
            train_bars_slice = bars[window_start : window_start + args.train_bars]
            test_bars_slice = bars[
                window_start + args.train_bars : window_start
                + args.train_bars
                + args.test_bars
            ]
            train_rows: list[dict] = []
            for config in grid:
                train_rows.append(_run_one(config, train_bars_slice))
            best_config: dict = _pick_best_params(train_rows)
            selected_train: dict = _selected_train_row(train_rows, best_config)
            out_of_sample: dict = _run_one(best_config, test_bars_slice)
            fold_results.append(
                {
                    "fold": len(fold_results) + 1,
                    "train_start": window_start,
                    "best_params": best_config,
                    "train_sharpe": selected_train["sharpe"],
                    "train_pnl": selected_train["total_pnl"],
                    "test_sharpe": out_of_sample["sharpe"],
                    "test_pnl": out_of_sample["total_pnl"],
                }
            )
            window_start += args.test_bars

        print(
            f"{'fold':>5} {'train_sharpe':>13} {'train_pnl':>12}"
            f" {'test_sharpe':>12} {'test_pnl':>12}  params"
        )
        for row in fold_results:
            params_text = ",".join(f"{k}={v}" for k, v in row["best_params"].items())
            print(
                f"{row['fold']:>5} {row['train_sharpe']:>13.3f}"
                f" {row['train_pnl']:>12.2f} {row['test_sharpe']:>12.3f}"
                f" {row['test_pnl']:>12.2f}  {params_text}"
            )

        aggregate_train: float = sum(r["train_pnl"] for r in fold_results)
        aggregate_test: float = sum(r["test_pnl"] for r in fold_results)
        aggregate_train_sharpe: float = (
            sum(r["train_sharpe"] for r in fold_results) / len(fold_results)
        )
        aggregate_test_sharpe: float = (
            sum(r["test_sharpe"] for r in fold_results) /len(fold_results)
        )

        def _write_walk_forward_output() -> None:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "data_bars": len(bars),
                        "train_bars": args.train_bars,
                        "test_bars": args.test_bars,
                        "folds": fold_results,
                        "aggregate_train_pnl": aggregate_train,
                        "aggregate_test_pnl": aggregate_test,
                        "aggregate_train_sharpe": aggregate_train_sharpe,
                        "aggregate_test_sharpe": aggregate_test_sharpe,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"结果已写入：{output}")

        print(
            f"\n样本内合计 pnl={aggregate_train:.2f}, avg_sharpe={aggregate_train_sharpe:.3f}"
        )
        print(
            f"样本外合计 pnl={aggregate_test:.2f}, avg_sharpe={aggregate_test_sharpe:.3f}"
        )
        if aggregate_test <= 0:
            print("结论：样本外未取得正期望，参数不稳定或策略无边际。")
            if args.output:
                _write_walk_forward_output()
            return 1
        if aggregate_test_sharpe < aggregate_train_sharpe * 0.5:
            print("结论：样本外表现显著劣于样本内，存在过拟合风险，不建议启用。")
            if args.output:
                _write_walk_forward_output()
            return 1
        print("结论：样本外表现与样本内一致，参数相对稳定。")
        if args.output:
            _write_walk_forward_output()
        return 0

    if args.output:
        output_path: Path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "data_bars": len(bars),
            "results": rows,
            "best": best,
        }
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入：{output_path}")

    if not rows:
        print("错误：没有可用的回测结果")
        return 2
    best = rows[0]
    if best["total_pnl"] <= 0:
        print("结论：最优参数组合仍未取得正期望。")
        return 1
    return 0



def _read_state_file(config_path: str) -> Path:
    """Resolve the state file path the runner would have written."""
    from .config import load_config

    config = load_config(config_path)
    state_file: str = config.runtime.state_file or "autonomous_state.json"
    candidate: Path = Path(state_file)
    if candidate.is_absolute():
        return candidate
    return Path(config_path).parent / candidate


def _format_state_line(payload: dict) -> str:
    """One human-readable summary line for the state payload."""
    from datetime import datetime as _dt

    now: _dt = _dt.now()
    updated_at: str = str(payload.get("updated_at", ""))
    try:
        age: float = (now - _dt.fromisoformat(updated_at).replace(tzinfo=None)).total_seconds()
    except ValueError:
        age = -1.0
    age_text: str = f"{age:.0f}s" if age >= 0 else "unknown"
    guard: dict = payload.get("guard") or {}
    health: dict = payload.get("health") or {}
    # health is {strategy_name: {halted: bool, ...}}; the operator wants to
    # know whether any strategy is currently halted, not the per-strategy
    # breakdown (use the full state file for that).
    any_halted: bool = any(
        bool(h.get("halted")) for h in health.values() if isinstance(h, dict)
    )
    return (
        f"pid={payload.get('pid')} mode={payload.get('mode')} "
        f"status={payload.get('status')} session={payload.get('session')} "
        f"uptime={payload.get('uptime_secs')}s heartbeat_age={age_text} "
        f"restarts={payload.get('restarts_in_session')} "
        f"strategies={payload.get('strategy_engines')} "
        f"guard_armed={guard.get('armed')} health_halted={any_halted}"
        f" guard_breached={guard.get('breached', False)}"
    )


def command_status(args: argparse.Namespace) -> int:
    """Print the current runtime state from the state file."""
    import time

    from .runner import read_state

    config_path: str = str(args.config)
    state_path: Path = _read_state_file(config_path)

    if not args.follow:
        if not state_path.exists():
            print(f"错误：状态文件不存在 {state_path}")
            print("提示：先启动 python -m vnpy.autonomous run，才会生成状态文件。")
            return 2
        payload = read_state(str(state_path))
        if not payload:
            print(f"错误：状态文件读取失败 {state_path}")
            return 2
        print(f"状态文件：{state_path}")
        print(_format_state_line(payload))
        return 0

    print("按 Ctrl+C 退出。")
    try:
        while True:
            if state_path.exists():
                payload = read_state(str(state_path))
                if payload:
                    line: str = _format_state_line(payload)
                    stamp: str = datetime.now().strftime("%H:%M:%S")
                    print(f"[{stamp}] {line}", flush=True)
            else:
                print("状态文件尚未生成，等待中...", flush=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("已退出。")
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
        "status": command_status,
    }
    handler = handlers[args.command]
    try:
        return int(handler(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
