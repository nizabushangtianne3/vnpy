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
from typing import cast

import polars as pl

from vnpy.alpha import AlphaLab
from vnpy.alpha.strategy import AlphaStrategy, BacktestingEngine
from vnpy.autonomous.stocks import _fetch_window
from vnpy.alpha.strategy.strategies.market_neutral_strategy import (
    MarketNeutralStrategy,
)
from vnpy.alpha.strategy.strategies.rank_band_strategy import RankBandStrategy
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData


DEFAULT_LAB: str = "D:/Codex/work/vnpy-alpha-cache/alpha_lab"
DEFAULT_SIGNAL: str = (
    "D:/Codex/work/vnpy-alpha-cache/sweeps/"
    "market3000_size_rolling900_250_s42.parquet"
)


def load_hedge_bars(hedge_symbol: str, start: str, end: str) -> list:
    """Load unadjusted daily bars for the hedge instrument.

    The strategy's own bars are already forward-adjusted A-share bars, but
    an ETF or index-future leg has to be priced on the exact instrument the
    broker can trade, so its bars are fetched separately from the public
    kline host rather than reused from the signal's price factors.
    """
    symbol, _, market = hedge_symbol.partition(".")
    if market not in ("SSE", "SZSE"):
        raise ValueError(f"对冲标的上交所或深交所之外的代码：{hedge_symbol}")
    secid = f"{('1' if market == 'SSE' else '0')}.{symbol}"
    began = datetime.strptime(start, "%Y-%m-%d").strftime("%Y%m%d")
    finished = datetime.strptime(end, "%Y-%m-%d").strftime("%Y%m%d")
    bars = _fetch_window(secid, "101", began, finished, fqt="0")
    if not bars:
        raise ValueError(f"无法加载对冲标的行情：{hedge_symbol}")
    # The kline parser leaves the instrument blank and lets the caller name
    # it, so an unnamed hedge leg would land in the lab under a vt_symbol
    # the strategy never asks for.
    market_exchange = Exchange.SSE if market == "SSE" else Exchange.SZSE
    for bar in bars:
        # vt_symbol is a snapshot taken when the dataclass was built, so
        # rewriting symbol alone leaves the leg answering to a blank name.
        bar.symbol = symbol
        bar.exchange = market_exchange
        bar.vt_symbol = f"{symbol}.{market_exchange.value}"
    return bars


def stock_rates(
    commission_rate: float,
    transfer_fee_rate: float,
    stamp_tax_rate: float,
) -> tuple[float, float]:
    """The buy-side and sell-side cost of one A-share round trip.

    The stamp tax is charged on the transfer of a share, so it lands on the
    sell side only, and the transfer fee is charged on both sides. The
    sell side is therefore the buy side plus the stamp tax, and the fee
    the engine charges has to carry both halves or the hedge's cost is
    understated by the side that pays it.
    """
    buy: float = commission_rate + transfer_fee_rate
    sell: float = buy + stamp_tax_rate
    return buy, sell


def etf_rates(
    commission_rate: float,
    transfer_fee_rate: float,
) -> tuple[float, float]:
    """The buy-side and sell-side cost of an ETF round trip.

    An ETF share is not a stock and not a depositary receipt, and the stamp
    tax is levied on the transfer of a stock, so an ETF leg pays no stamp
    tax on the sell side. It still pays the transfer fee, which the clearing
    house charges on every transfer including fund shares.
    """
    side: float = commission_rate + transfer_fee_rate
    return side, side


def exposure(engine: BacktestingEngine, capital: float) -> dict:
    """The real exposures the engine's cash book cannot see by itself.

    The engine's own equity figure subtracts a short position's market value
    as if selling the position released cash the account may spend, which
    is true for a closed trade and false for a security the book still owes.
    A neutral book therefore has to be audited on gross, long and short
    exposure separately, because a hedge sized off one definition can look
    profitable while the account is silently leveraged.
    """
    long_value: float = 0.0
    short_value: float = 0.0
    for vt_symbol, position in engine.strategy.pos_data.items():
        if not position:
            continue
        bar = engine.bars.get(vt_symbol)
        price = bar.close_price if bar else 0.0
        if position > 0:
            long_value += price * position
        else:
            short_value += price * abs(position)
    trades = engine.get_all_trades()
    hedge_trades = [
        trade
        for trade in trades
        if trade.vt_symbol == getattr(engine.strategy, "hedge_symbol", "")
    ]
    return {
        "long_x_capital": round(long_value / capital, 4),
        "short_x_capital": round(short_value / capital, 4),
        "net_x_capital": round((long_value - short_value) / capital, 4),
        "gross_x_capital": round((long_value + short_value) / capital, 4),
        "hedge_trade_count": len(hedge_trades),
    }


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
    hedge_symbol: str,
    rebalance_days: int,
    min_days: int,
    max_turnover_ratio: float,
    buy_rate: float,
    sell_rate: float,
    borrow_rate: float,
    signal_lag: int = 0,
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
        strategy_class: type[AlphaStrategy] = MarketNeutralStrategy
        strategy_setting = {
            "band_low": band_low,
            "band_high": band_high,
            "buffer_ratio": buffer_ratio,
            "hedge_ratio": hedge_ratio,
            "hedge_count": hedge_count,
            "hedge_symbol": hedge_symbol,
            "rebalance_days": rebalance_days,
            "min_days": min_days,
            "max_turnover_ratio": max_turnover_ratio,
            "open_rate": buy_rate,
            "close_rate": sell_rate,
            "signal_lag": signal_lag,
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
            "open_rate": buy_rate,
            "close_rate": sell_rate,
            "signal_lag": signal_lag,
        }
    engine.add_strategy(
        strategy_class,
        strategy_setting,
        signal,
    )
    if borrow_rate > 0:
        # The lending fee is charged on every symbol the book can go short
        # in, which is either the named hedge instrument or the liquid
        # basket. It is charged uniformly on that set rather than on the
        # names the engine can already see, because a fee only bites on the
        # days a position is actually short and the daily result layer
        # applies it per symbol against the sign of the position.
        shortable: list[str] = [hedge_symbol] if hedge_symbol else list(vt_symbols)
        engine.set_borrow_rates({vt_symbol: borrow_rate for vt_symbol in shortable})
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
        "borrow_fee_fraction": round(
            float(stats.get("total_borrow_fee", 0.0)) / capital, 6
        ),
        "trades": int(stats["total_trade_count"]),
    }


def leg_split(engine: BacktestingEngine, hedge_symbol: str) -> dict:
    """Split every session's net pnl into the hedge leg and everything else.

    A positive total on the graded half can come from the selection or from
    the hedge leg alone, and a book that is long a falling market and short a
    falling index is a different bet from a book whose selection paid. The
    engine keeps one ContractDailyResult per contract per day, so the split is
    read directly off them rather than inferred from the signals.
    """
    if not hedge_symbol:
        return {}
    rows: list[dict] = []
    for daily in engine.daily_results.values():
        hedge: float = 0.0
        rest: float = 0.0
        hedge_turnover: float = 0.0
        rest_turnover: float = 0.0
        for vt_symbol, cr in daily.contract_results.items():
            if vt_symbol == hedge_symbol:
                hedge += cr.net_pnl
                hedge_turnover += cr.turnover
            else:
                rest += cr.net_pnl
                rest_turnover += cr.turnover
        rows.append(
            {
                "date": daily.date.isoformat(),
                "hedge_net": round(hedge, 2),
                "rest_net": round(rest, 2),
                "total_net": round(hedge + rest, 2),
                "hedge_turnover": round(hedge_turnover, 2),
                "rest_turnover": round(rest_turnover, 2),
            }
        )
    if not rows:
        return {}
    hedge_net: float = sum(row["hedge_net"] for row in rows)
    rest_net: float = sum(row["rest_net"] for row in rows)
    return {
        "hedge_symbol": hedge_symbol,
        "sessions": len(rows),
        "hedge_net": round(hedge_net, 2),
        "rest_net": round(rest_net, 2),
        "total_net": round(hedge_net + rest_net, 2),
        "hedge_positive_sessions": sum(1 for row in rows if row["hedge_net"] > 0),
        "rest_positive_sessions": sum(1 for row in rows if row["rest_net"] > 0),
        "hedge_turnover": round(sum(row["hedge_turnover"] for row in rows), 2),
        "rest_turnover": round(sum(row["rest_turnover"] for row in rows), 2),
        "daily": rows,
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
        std = cast(float, net.std() or 0.0) if total_days > 1 else 0.0
        mean = cast(float, net.mean() or 0.0)
        key = year[0] if isinstance(year, tuple) else year
        out[str(key)] = {
            "sessions": int(total_days),
            "net_pnl": round(cast(float, net.sum()), 2),
            "mean_pnl": round(mean, 2),
            "sharpe": round(mean / std * (240.0 ** 0.5), 4) if std else 0.0,
            "max_drawdown": round(cast(float, drawdown.min()), 2),
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
    parser.add_argument(
        "--hedge-symbol",
        default="",
        help="single hedge instrument, e.g. 510300.SSE; empty uses the stock basket",
    )
    parser.add_argument("--rebalance-days", type=int, default=30)
    parser.add_argument("--min-days", type=int, default=5)
    parser.add_argument("--max-turnover-ratio", type=float, default=0.3)
    parser.add_argument(
        "--commission-rate",
        type=float,
        default=0.00025,
        help="broker commission on both sides",
    )
    parser.add_argument(
        "--transfer-fee-rate",
        type=float,
        default=0.00001,
        help="clearing house transfer fee on both sides",
    )
    parser.add_argument(
        "--stamp-tax-rate",
        type=float,
        default=0.0005,
        help="stamp tax on stock sells; ETFs pay none",
    )
    parser.add_argument(
        "--etf-commission-rate",
        type=float,
        default=0.00015,
        help="broker commission on an ETF leg, often discounted",
    )
    parser.add_argument(
        "--borrow-rate",
        type=float,
        default=0.0,
        help="annualised securities lending fee on the short leg",
    )
    parser.add_argument(
        "--open-rate",
        type=float,
        default=0.0,
        help="override the buy-side rate; 0 uses the modelled fees",
    )
    parser.add_argument(
        "--close-rate",
        type=float,
        default=0.0,
        help="override the sell-side rate; 0 uses the modelled fees",
    )
    parser.add_argument(
        "--signal-lag",
        type=int,
        default=0,
        help="sessions between the signal used and the bar traded",
    )
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
    if args.hedge_symbol and args.hedge_symbol not in vt_symbols:
        vt_symbols.append(args.hedge_symbol)
    lab = AlphaLab(args.lab)
    # A stock and an ETF pay different fees, and the difference is not a
    # detail: the stamp tax is charged on the transfer of a share, so an ETF
    # leg never pays it while a stock leg pays it on every sell.
    stock_buy, stock_sell = stock_rates(
        args.commission_rate,
        args.transfer_fee_rate,
        args.stamp_tax_rate,
    )
    if args.open_rate > 0 or args.close_rate > 0:
        stock_buy = args.open_rate if args.open_rate > 0 else stock_buy
        stock_sell = args.close_rate if args.close_rate > 0 else stock_sell
    etf_buy, etf_sell = etf_rates(
        args.etf_commission_rate,
        args.transfer_fee_rate,
    )
    if args.open_rate > 0:
        etf_buy = args.open_rate
    if args.close_rate > 0:
        etf_sell = args.close_rate

    # The engine charges the commission from the lab's contract settings, so
    # the cost sensitivity has to be written there and not only into the
    # strategy's own budget arithmetic.
    for vt_symbol in vt_symbols:
        if not (Path(args.lab) / "daily" / f"{vt_symbol}.parquet").exists():
            continue
        lab.add_contract_setting(
            vt_symbol=vt_symbol,
            long_rate=stock_buy,
            short_rate=stock_sell,
            size=1.0,
            pricetick=0.01,
        )
    if args.hedge_symbol:
        lab.add_contract_setting(
            vt_symbol=args.hedge_symbol,
            long_rate=etf_buy,
            short_rate=etf_sell,
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

    if args.hedge_symbol:
        print(f"加载对冲标的 {args.hedge_symbol}")
        hedge_bars: list[BarData] = load_hedge_bars(
            args.hedge_symbol, args.start, args.end
        )
        lab.save_bar_data(hedge_bars)
        print(f"hedge bars: {len(hedge_bars)}")

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
            args.hedge_symbol,
            args.rebalance_days,
            args.min_days,
            args.max_turnover_ratio,
            stock_buy,
            stock_sell,
            args.borrow_rate,
            args.signal_lag,
        )
        summary = summarise(engine, args.capital)
        by_year = yearly(engine)
        if args.strategy == "neutral":
            audit = exposure(engine, args.capital)
            summary.update(audit)
        result = {
            "strategy": args.strategy,
            "band": [low, high],
            "hedge_ratio": args.hedge_ratio if args.strategy == "neutral" else None,
            "summary": summary,
            "by_year": by_year,
            "legs": leg_split(engine, args.hedge_symbol),
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
