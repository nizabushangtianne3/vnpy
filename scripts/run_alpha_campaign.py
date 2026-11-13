"""Run one cross-sectional alpha campaign end to end from cached real bars.

This is the shape a stock quant stack actually takes - Qlib, RQAlpha and this
repository's own ``vnpy.alpha`` all do the same thing: build cross-sectional
factors, fit a model to predict next-period returns, turn the predictions into
a ranking signal, and backtest a portfolio that trades that ranking. Running
a single-instrument CTA strategy across many stocks is a different and much
weaker claim, and the campaigns recorded in ``docs`` graded three such
 strategies ``retire`` on real A-share bars.

The label is the part that decides whether the result means anything, so its
construction is stated rather than left implicit: ``shift`` walks next-session
 returns back to the current row, which is what lets a model trained on row T
predict a return the strategy can only earn at T+1. Without that shift the
label would describe the future the model is being asked about, and the
backtest would grade foresight instead of an edge.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from functools import partial
from pathlib import Path

import polars as pl

from vnpy.alpha import Segment
from vnpy.alpha.dataset import process_cs_norm, process_drop_na
from vnpy.alpha.dataset.datasets.alpha_158 import Alpha158
from vnpy.alpha.model.models.lgb_model import LgbModel
from vnpy.alpha.strategy import BacktestingEngine
from vnpy.trader.constant import Interval


DEFAULT_LAB: str = ".vntrader/alpha_lab"
DEFAULT_BARS: str = ".vntrader/stock_bars"


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for one campaign."""
    parser = argparse.ArgumentParser(
        prog="alpha-campaign",
        description="Cross-sectional alpha campaign on cached A-share daily bars",
    )
    parser.add_argument("--lab", default=DEFAULT_LAB, help="AlphaLab store path")
    parser.add_argument("--bars", default=DEFAULT_BARS, help="cached bars directory")
    parser.add_argument("--start", default="2021-01-01", help="window start")
    parser.add_argument("--end", default="2026-11-13", help="window end")
    parser.add_argument("--hold-days", type=int, default=5, help="label horizon in sessions")
    parser.add_argument("--extended-days", type=int, default=100, help="warmup bars before start")
    parser.add_argument("--min-bars", type=int, default=120, help="minimum cached bars per symbol")
    parser.add_argument("--capital", type=float, default=100_000_000, help="backtest capital")
    parser.add_argument("--top-k", type=int, default=30, help="stocks held at once")
    parser.add_argument("--n-drop", type=int, default=3, help="stocks dropped per rebalance")
    parser.add_argument("--min-days", type=int, default=3, help="minimum holding days")
    parser.add_argument("--output", default="", help="write the summary to this JSON path")
    parser.add_argument("--engine-start", default="2024-01-01", help="backtest window start")
    parser.add_argument("--engine-end", default="2026-11-13", help="backtest window end")
    return parser


def label_expression(hold_days: int) -> str:
    """Build the label expression for a fixed holding horizon.

    ``ts_delay(close, n)`` walks the close back n sessions; a negative n walks
    it forward, which is what a label needs. The denominator is the close one
    session after the row was observed rather than today's close, so the
    horizon is measured from the first price the strategy could actually get,
    not from a close it could not trade at.
    """
    return f"ts_delay(close, -{hold_days}) / ts_delay(close, -1) - 1"


def main() -> int:
    """Run the campaign and print the summary."""
    args = build_parser().parse_args()

    from vnpy.autonomous.alpha_loader import build_lab

    lab, codes = build_lab(args.lab, args.bars, min_bars=args.min_bars)
    print(f"标的数量：{len(codes)}")

    index_symbol: str = "000300.SSE"
    symbols: list[str] = [
        f"{code}.{'SSE' if code.startswith('6') else 'BSE' if code.startswith('920') or code.startswith('4') or code.startswith('8') else 'SZSE'}"
        for code in codes
    ]

    df: pl.DataFrame | None = lab.load_bar_df(
        symbols, Interval.DAILY, args.start, args.end, args.extended_days
    )
    if df is None or df.is_empty():
        print("错误：未能加载任何行情数据")
        return 2
    print(f"数据规模：{df.height} 行 x {df.width} 列")

    dataset = Alpha158(
        df,
        train_period=("2021-01-01", "2022-12-31"),
        valid_period=("2023-01-01", "2023-12-31"),
        test_period=("2024-01-01", "2026-11-13"),
    )
    dataset.set_label(label_expression(args.hold_days))
    dataset.add_processor("learn", partial(process_drop_na, names=["label"]))
    dataset.add_processor("learn", partial(process_cs_norm, names=["label"], method="zscore"))

    filters = lab.load_component_filters(index_symbol, args.start, args.end)
    dataset.prepare_data(filters, max_workers=6)
    dataset.process_data()

    model = LgbModel(seed=42)
    model.fit(dataset)

    predicted = model.predict(dataset, Segment.TEST)
    # Order must match what ``predict`` sorted its input to, or every
    # prediction lands on the wrong symbol.
    infer = dataset.fetch_infer(Segment.TEST).sort(["datetime", "vt_symbol"])
    signal = infer.with_columns(pl.Series(predicted).alias("signal"))
    signal = signal.select("datetime", "vt_symbol", "signal")

    engine = BacktestingEngine(lab)
    engine.set_parameters(
        vt_symbols=symbols,
        interval=Interval.DAILY,
        start=datetime.strptime(args.engine_start, "%Y-%m-%d"),
        end=datetime.strptime(args.engine_end, "%Y-%m-%d"),
        capital=int(args.capital),
    )
    from vnpy.alpha.strategy.strategies.equity_demo_strategy import EquityDemoStrategy

    engine.add_strategy(
        EquityDemoStrategy,
        {"top_k": args.top_k, "n_drop": args.n_drop, "min_days": args.min_days},
        signal,
    )
    engine.load_data()
    engine.run_backtesting()
    engine.calculate_result()
    stats = engine.calculate_statistics()

    print(json.dumps(stats, ensure_ascii=False, indent=1, default=str))

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
            {"codes": len(codes), "stats": stats}, ensure_ascii=False, indent=1, default=str
        ), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
