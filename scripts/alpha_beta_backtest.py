"""Backtest the beta question with the real engine, costs included.

The offline probe established that the top-30 book's beta against the
cross-section is 1.58 while a random 30-name book is 1.00, and that the
band inside the ranking carries less of that leverage. Neither number
survives the real book: the engine charges A-share commissions, rounds to
board lots, caps turnover and rebalances on a clock, and the result is the
only one the gate reads.

This runner exists so that sweep is cheap. The signal is read from the
rolling cache and the bars from the lab's daily store, so nothing is
refitted and nothing is recomputed; only the strategy changes between
runs.

Usage::

    python scripts/alpha_beta_backtest.py --band 0.0 0.10
    python scripts/alpha_beta_backtest.py --sweep
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import polars as pl

from vnpy.alpha import AlphaLab
from vnpy.alpha.strategy import BacktestingEngine
from vnpy.alpha.strategy.strategies.market_neutral_strategy import (
    MarketNeutralStrategy,
)
from vnpy.alpha.strategy.strategies.rank_band_strategy import RankBandStrategy
from vnpy.trader.constant import Interval


DEFAULT_LAB: str = "D:/Codex/work/vnpy-alpha-cache/alpha_lab"
DEFAULT_SIGNAL: str = (
    "D:/Codex/work/vnpy-alpha-cache/sweeps/"
    "market3000_size_rolling900_250_s42.parquet"
)


def build_engine(
    lab: AlphaLab,
    signal: pl.DataFrame,
    vt_symbols: list[str],
    start: str,
    end: str,
    capital: float,
    band_low: float,
    band_high: float,
    strategy: str,
    buffer_ratio: float,
    hedge_ratio: float,
    hedge_count: int,
    rebalance_days: int,
    min_days: int,
    max_turnover_ratio: float,
    open_rate: float,
    close_rate: float,
) -> BacktestingEngine:
    """Set up one engine run with the band strategy."""
    engine = BacktestingEngine(lab)
    engine.set_parameters(
        vt_symbols=vt_symbols,
        interval=Interval.DAILY,
        start=datetime.strptime(start, "%Y-%m-%d"),
        end=datetime.strptime(end, "%Y-%m-%d"),
        capital=int(capital),
    )
    if strategy == "neutral":
        strategy_class = MarketNeutralStrategy
        strategy_setting = {
            "band_low": band_low,
            "band_high": band_high,
            "buffer_ratio": buffer_ratio,
            "hedge_ratio": hedge_ratio,
            "hedge_count": hedge_count,
            "rebalance_days": rebalance_days,
            "min_days": min_days,
            "max_turnover_ratio": max_turnover_ratio,
            "open_rate": open_rate,
            "close_rate": close_rate,
        }
    else:
        strategy_class = RankBandStrategy
        strategy_setting = {
            "band_low": band_low,
            "band_high": band_high,
            "buffer_ratio": buffer_ratio,
            "rebalance_days": rebalance_days,
            "min_days": min_days,
            "max_turnover_ratio": max_turnover_ratio,
            "open_rate": open_rate,
            "close_rate": close_rate,
        }
    engine.add_strategy(
        strategy_class,
        strategy_setting,
        signal,
    )
    engine.load_data()
    engine.run_backtesting()
    engine.calculate_result()
    return engine


def summarise(engine: BacktestingEngine, capital: float) -> dict:
    """Read the engine's own numbers into the shape the gate reads."""
    stats = engine.calculate_statistics()
    total_days: int = int(stats.get("total_days", 0))
    return {
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
            float(stats["total_turnover"]) / capital, 4
        ),
        "commission_fraction": round(
            float(stats["total_commission"]) / capital, 6
        ),
        "trades": int(stats["total_trade_count"]),
    }


def yearly(engine: BacktestingEngine) -> dict:
    """The gate reads one number over the whole window; a year can still hide."""
    df = engine.daily_df
    if df is None or df.is_empty():
        return {}
    out: dict = {}
    df = df.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 4).alias("year"))
    for year, part in df.group_by("year"):
        net = part["net_pnl"]
        total_days = len(part)
        balance = net.cum_sum()
        peak = balance.cum_max()
        drawdown = balance - peak
        std = float(net.std()) if total_days > 1 else 0.0
        mean = float(net.mean())
        key = year[0] if isinstance(year, tuple) else year
        out[str(key)] = {
            "sessions": int(total_days),
            "net_pnl": round(float(net.sum()), 2),
            "mean_pnl": round(mean, 2),
            "sharpe": round(mean / std * (240.0 ** 0.5), 4) if std else 0.0,
            "max_drawdown": round(float(drawdown.min()), 2),
        }
    return out


def main() -> int:
    """Run one band, or a sweep over several."""
    parser = argparse.ArgumentParser(description="Band strategy backtest")
    parser.add_argument("--lab", default=DEFAULT_LAB)
    parser.add_argument("--signal", default=DEFAULT_SIGNAL)
    parser.add_argument("--start", default="2024-09-26")
    parser.add_argument("--end", default="2026-11-13")
    parser.add_argument("--capital", type=float, default=100_000_000)
    parser.add_argument("--band-low", type=float, default=0.05)
    parser.add_argument("--band-high", type=float, default=0.15)
    parser.add_argument(
        "--buffer-ratio",
        type=float,
        default=0.10,
        help="share of the cross-section below the band that a held name may fall and stay",
    )
    parser.add_argument(
        "--strategy",
        default="band",
        choices=["band", "neutral"],
        help="band is long only; neutral adds a short basket against it",
    )
    parser.add_argument(
        "--hedge-ratio",
        type=float,
        default=1.0,
        help="short value per unit of long value",
    )
    parser.add_argument(
        "--hedge-count",
        type=int,
        default=30,
        help="names in the short basket",
    )
    parser.add_argument("--rebalance-days", type=int, default=30)
    parser.add_argument("--min-days", type=int, default=5)
    parser.add_argument("--max-turnover-ratio", type=float, default=0.3)
    parser.add_argument("--open-rate", type=float, default=0.0005)
    parser.add_argument("--close-rate", type=float, default=0.0015)
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="run the bands the beta table says are worth measuring",
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    signal = pl.read_parquet(args.signal)
    print(f"signal rows: {signal.height}, symbols: {signal['vt_symbol'].n_unique()}")

    vt_symbols: list[str] = sorted(signal["vt_symbol"].unique().to_list())
    lab = AlphaLab(args.lab)
    # The engine charges the commission from the lab's contract settings, so
    # the cost sensitivity has to be written there and not only into the
    # strategy's own budget arithmetic.
    for vt_symbol in vt_symbols:
        if not (Path(args.lab) / "daily" / f"{vt_symbol}.parquet").exists():
            continue
        lab.add_contract_setting(
            vt_symbol=vt_symbol,
            long_rate=args.open_rate,
            short_rate=args.close_rate,
            size=1.0,
            pricetick=0.01,
        )

    if args.sweep:
        bands = [
            (0.00, 0.01),
            (0.00, 0.05),
            (0.05, 0.10),
            (0.05, 0.15),
            (0.10, 0.20),
            (0.10, 0.30),
            (0.20, 0.40),
        ]
    else:
        bands = [(args.band_low, args.band_high)]

    results: list[dict] = []
    for low, high in bands:
        print(f"=== band {low:.2f}-{high:.2f} ===")
        engine = build_engine(
            lab,
            signal,
            vt_symbols,
            args.start,
            args.end,
            args.capital,
            low,
            high,
            args.strategy,
            args.buffer_ratio,
            args.hedge_ratio,
            args.hedge_count,
            args.rebalance_days,
            args.min_days,
            args.max_turnover_ratio,
            args.open_rate,
            args.close_rate,
        )
        summary = summarise(engine, args.capital)
        by_year = yearly(engine)
        result = {
            "strategy": args.strategy,
            "band": [low, high],
            "hedge_ratio": args.hedge_ratio if args.strategy == "neutral" else None,
            "summary": summary,
            "by_year": by_year,
        }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=1))

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(results, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
