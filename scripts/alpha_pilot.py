"""Run the cross-sectional pipeline on a small real sample, end to end.

The full campaign spends most of its time computing 158 factors per bar per
symbol, so a change to the label, the horizon or the strategy is slow to
 validate. This script exists to make that iteration cheap: it takes a symbol
 sample, runs the identical pipeline, and prints the numbers. It shares the
 code with the full campaign rather than re-implementing it, so a passing
pilot and a passing campaign are the same test at different sizes.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from functools import partial
from pathlib import Path

import polars as pl

from vnpy.alpha import AlphaDataset, AlphaLab, Segment
from vnpy.alpha.dataset import process_cs_norm, process_drop_na
from vnpy.alpha.dataset.datasets.alpha_158 import Alpha158
from vnpy.alpha.model.models.lgb_model import LgbModel
from vnpy.alpha.strategy import BacktestingEngine
from vnpy.alpha.strategy.strategies.equity_demo_strategy import EquityDemoStrategy
from vnpy.trader.constant import Interval


DEFAULT_BARS: str = os.environ.get(
    "VNPY_BARS_DIR",
    "D:/Codex/work/vnpy-alpha-cache/stock_bars",
)
DEFAULT_LAB: str = os.environ.get(
    "VNPY_ALPHA_LAB",
    "D:/Codex/work/vnpy-alpha-cache/alpha_lab",
)


def label_expression(hold_days: int) -> str:
    """Label: the return from the next tradeable session to the horizon."""
    return f"ts_delay(close, -{hold_days}) / ts_delay(close, -1) - 1"


def main() -> int:
    """Run the pilot campaign."""
    parser = argparse.ArgumentParser(description="Alpha pilot on a symbol sample")
    parser.add_argument("--codes", default="", help="comma separated symbols, or a file holding them")
    parser.add_argument("--bars", default=DEFAULT_BARS, help="directory holding cached CSV bars")
    parser.add_argument("--lab", default=DEFAULT_LAB, help="AlphaLab store path")
    parser.add_argument("--sample", type=int, default=300, help="random sample size when --codes is empty")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-11-13")
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--extended-days", type=int, default=100)
    parser.add_argument("--capital", type=float, default=100_000_000)
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument("--n-drop", type=int, default=3)
    parser.add_argument("--min-days", type=int, default=5)
    parser.add_argument("--strategy", default="gapped", choices=["demo", "gapped"])
    parser.add_argument("--rebalance-days", type=int, default=5)
    parser.add_argument("--max-turnover-ratio", type=float, default=0.3)
    parser.add_argument("--output", default="")
    parser.add_argument("--reuse-dataset", default="", help="cached dataset name to reuse instead of recomputing")
    parser.add_argument("--dataset-name", default="pilot", help="name to cache the dataset under")
    parser.add_argument("--train-end", nargs=2, default=["2021-01-01", "2022-12-31"])
    parser.add_argument("--valid-end", nargs=2, default=["2023-01-01", "2023-12-31"])
    parser.add_argument("--engine-start", default="2024-01-01")
    parser.add_argument("--engine-end", default="2026-11-13")
    args = parser.parse_args()

    from vnpy.autonomous.alpha_loader import load_universe_codes, read_cached_bars

    if args.codes:
        text: str = args.codes
        if Path(text).is_file():
            text = Path(text).read_text(encoding="utf-8")
        codes = [c.strip() for c in text.split(",") if c.strip()]
    else:
        import random

        universe = load_universe_codes(args.bars)
        random.seed(42)
        codes = sorted(random.sample(universe, min(args.sample, len(universe))))

    print(f"标的数量：{len(codes)}")

    series: dict[str, list] = {}
    for code in codes:
        bars = read_cached_bars(code, args.bars)
        if len(bars) >= 120:
            series[code] = bars
    codes = sorted(series)
    print(f"达到 120 根日线的标的：{len(codes)}")

    lab = AlphaLab(args.lab)
    vt_symbols: list[str] = []
    components: dict[str, list[str]] = {}
    for code in codes:
        from vnpy.autonomous.alpha_loader import _exchange_for

        exchange_value = _exchange_for(code).value
        vt_symbol = f"{code}.{exchange_value}"
        vt_symbols.append(vt_symbol)
        # The backtester prices an order off this table, so a symbol missing
        # from it raises instead of filling. Writing it for every symbol held
        # is what keeps the order path executable rather than theoretical.
        lab.add_contract_setting(
            vt_symbol=vt_symbol,
            long_rate=0.0005,
            short_rate=0.0015,
            size=1.0,
            pricetick=0.01,
        )
        for bar in series[code]:
            components.setdefault(bar.datetime.date().isoformat(), []).append(vt_symbol)

    for bars in series.values():
        lab.save_bar_data(bars)
    lab.save_component_data("000300.SSE", components)

    df: pl.DataFrame | None = lab.load_bar_df(
        vt_symbols, Interval.DAILY, args.start, args.end, args.extended_days
    )
    if df is None or df.is_empty():
        print("错误：未能加载任何行情数据")
        return 2
    print(f"数据规模：{df.height} 行 x {df.width} 列")

    # Only the symbols that made it into the sample may appear in the filters.
    # A filter naming a symbol the store never loaded removes nothing but
    # hides the real gap, and a filter that omits a held symbol silently drops
    # the row, so the two must describe the same universe.
    filters = {
        vt_symbol: spans
        for vt_symbol, spans in lab.load_component_filters(
            "000300.SSE", args.start, args.end
        ).items()
        if vt_symbol in set(vt_symbols)
    }
    print(f"参与过滤的标的：{len(filters)}")

    # The factor computation is by far the expensive step, and sweeping a
    # rebalance clock re-runs it for every setting. Caching the prepared
    # dataset under a name means later runs of the same window skip it, which
    # is the difference between a sweep that finishes and one that does not.
    dataset: AlphaDataset | None = None
    if args.reuse_dataset:
        dataset = lab.load_dataset(args.reuse_dataset)
        if dataset is not None:
            print(f"复用数据集：{args.reuse_dataset}")
    if dataset is None:
        dataset = Alpha158(
            df,
            train_period=(args.train_end[0], args.train_end[1]),
            valid_period=(args.valid_end[0], args.valid_end[1]),
            # The test segment is where the model is scored and where the
            # book trades, and it has to be the engine window rather than the
            # load window: passing the load window here makes the train
            # period a subset of test, so the model is scored on sessions it
            # fitted on and every number that follows is in-sample.
            test_period=(args.engine_start, args.engine_end),
        )
        dataset.set_label(label_expression(args.hold_days))
        dataset.add_processor("learn", partial(process_drop_na, names=["label"]))
        dataset.add_processor("learn", partial(process_cs_norm, names=["label"], method="zscore"))
        # A pickled copy of the frame goes to every worker, so a pool over a
        # market-sized sample stalls on the serialisation. One worker pays it
        # once and finishes.
        dataset.prepare_data(filters, max_workers=1)
        dataset.process_data()
        lab.save_dataset(args.dataset_name, dataset)
        print(f"数据集已缓存：{args.dataset_name}")

    model = LgbModel(seed=42)
    model.fit(dataset)

    predicted = model.predict(dataset, Segment.TEST)
    # ``predict`` sorts the inference frame by datetime and vt_symbol before
    # building its input matrix, and the frame ``fetch_infer`` already returns
    # happens to carry the same order, so this sort is a no-op today. It is kept
    # because it states the dependency rather than relying on the order being
    # preserved by a coincidence: dropping it changed nothing on 294 symbols
    # (1.1216 percent, 507 trades either way), but an upstream change that
    # stopped sorting would silently transpose the signal.
    infer = dataset.fetch_infer(Segment.TEST).sort(["datetime", "vt_symbol"])
    signal = infer.with_columns(pl.Series(predicted).alias("signal"))
    signal = signal.select("datetime", "vt_symbol", "signal")

    engine = BacktestingEngine(lab)
    engine.set_parameters(
        vt_symbols=vt_symbols,
        interval=Interval.DAILY,
        start=datetime.strptime(args.engine_start, "%Y-%m-%d"),
        end=datetime.strptime(args.engine_end, "%Y-%m-%d"),
        capital=int(args.capital),
    )
    if args.strategy == "demo":
        strategy_class = EquityDemoStrategy
        strategy_setting = {"top_k": args.top_k, "n_drop": args.n_drop, "min_days": args.min_days}
    else:
        from vnpy.alpha.strategy.strategies.gapped_rebalance_strategy import (
            GappedRebalanceStrategy,
        )

        strategy_class = GappedRebalanceStrategy
        strategy_setting = {
            "top_k": args.top_k,
            "rebalance_days": args.rebalance_days,
            "min_days": args.min_days,
            "max_turnover_ratio": args.max_turnover_ratio,
        }

    engine.add_strategy(strategy_class, strategy_setting, signal)
    engine.load_data()
    engine.run_backtesting()
    engine.calculate_result()
    stats = engine.calculate_statistics()

    # ``annual_return`` in the engine is a straight linear extrapolation:
    # total_return / total_days * annual_days, with total_return already a
    # percentage. It is not annualized compounding, so a 693-session window
    # that returned 12.98 percent reads as 4.50 percent, and a shorter window
    # extrapolates hard. The summary recomputes the same figure with the same
    # formula rather than reporting a value that looks misleading next to the
    # total return it came from.
    total_days: int = int(str(stats.get("total_days", 0)))
    summary: dict = {
        "start_date": stats.get("start_date"),
        "end_date": stats.get("end_date"),
        "sessions": total_days,
        "total_return_pct": round(float(stats["total_return"]), 4),
        "annual_return_pct": round(
            float(stats["total_return"]) / total_days * 240.0, 4
        )
        if total_days
        else None,
        "sharpe": round(float(stats["sharpe_ratio"]), 4),
        "max_drawdown_pct": round(float(stats["max_ddpercent"]), 4),
        "turnover_x_capital": round(
            float(stats["total_turnover"]) / float(stats["capital"]), 4
        ),
        "commission_fraction": round(
            float(stats["total_commission"]) / float(stats["capital"]), 6
        ),
        "trades": stats["total_trade_count"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"codes": len(codes), "stats": stats, "summary": summary},
                ensure_ascii=False,
                indent=1,
                default=str,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
