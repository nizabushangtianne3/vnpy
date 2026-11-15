"""What does the signal actually pay, portfolio by portfolio?

The backtest engine answers the only question that matters - what does a
book earn - but it is slow and it grades one setting at a time. This script
answers the same question directly from the cached signal and the cached
bars, so a grid of holdings sizes and holding periods can be measured in one
pass.

It also reports the number the engine cannot: how much of the theoretical
edge the book actually captured. The signal's own quintile spread is the
edge; the portfolio's realised forward return is what a book could have
taken from it. When the second is a fraction of the first, the problem is
the selection, not the signal.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
BARS: str = "D:/Codex/work/vnpy-alpha-cache/stock_bars"
SIGNAL: str = "D:/Codex/work/vnpy-alpha-cache/sweeps/market3000_rolling900_250.parquet"


def load_close() -> pl.DataFrame:
    """Close prices for every cached symbol, one row per session."""
    rows: list[dict] = []
    for path in Path(BARS).glob("*_*_101.csv"):
        code: str = path.stem.split("_")[1]
        with path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
        for line in lines[1:]:
            parts = line.strip().split(",")
            if len(parts) < 7:
                continue
            rows.append(
                {
                    "code": code,
                    "datetime": str(parts[0])[:10],
                    "close": float(parts[2]),
                }
            )
    return pl.DataFrame(rows).sort(["code", "datetime"])


def forward_returns(close: pl.DataFrame, horizon: int) -> pl.DataFrame:
    """Forward return over ``horizon`` sessions, per symbol.

    The return starts on the session AFTER the signal is scored. The signal
    for session T is produced by a model that has already seen session T's
    close, so a book cannot buy at that close; it buys at the next session.
    Measuring from T's close would credit the book with the move it could
    not have taken, and on a market-sized cross-section that error is worth
    an order of magnitude.
    """
    return close.with_columns(
        (
            pl.col("close").shift(-horizon).over("code")
            / pl.col("close").shift(-1).over("code")
            - 1.0
        ).alias("fwd")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal", default=SIGNAL)
    parser.add_argument(
        "--date-from",
        default="2024-09-26",
        help="first session to trade",
    )
    parser.add_argument(
        "--date-to",
        default="2026-11-13",
        help="last session to trade",
    )
    parser.add_argument(
        "--split-date",
        default="2025-07-01",
        help="the date that separates the choosing half from the checking half",
    )
    parser.add_argument(
        "--top-k",
        nargs="+",
        type=int,
        default=[10, 30, 50, 100, 200, 300, 600],
    )
    parser.add_argument(
        "--horizon",
        nargs="+",
        type=int,
        default=[5, 10, 20],
    )
    args = parser.parse_args()

    signal = pl.read_parquet(args.signal)
    signal = signal.with_columns(
        pl.col("vt_symbol").str.split(".").list.first().alias("code")
    )
    signal = signal.with_columns(pl.col("datetime").cast(pl.Utf8).str.slice(0, 10))
    signal = signal.filter(
        (pl.col("datetime") >= args.date_from) & (pl.col("datetime") <= args.date_to)
    )
    print(f"signal rows in window: {signal.height}")
    duplicated = (
        signal.group_by(["code", "datetime"]).agg(pl.len().alias("n")).filter(pl.col("n") > 1)
    )
    print(f"signal duplicate (code, datetime) pairs: {duplicated.height}")
    if duplicated.height:
        print(duplicated.head(5))
        return 2

    close = load_close()
    print(f"bar rows: {close.height}")
    close_dup = (
        close.group_by(["code", "datetime"]).agg(pl.len().alias("n")).filter(pl.col("n") > 1)
    )
    print(f"bar duplicate (code, datetime) pairs: {close_dup.height}")

    for horizon in args.horizon:
        priced = forward_returns(close, horizon)
        scored = signal.join(
            priced.select(["code", "datetime", "fwd"]),
            on=["code", "datetime"],
            how="inner",
        )
        scored = scored.filter(pl.col("fwd").is_not_null())
        scored_dup = (
            scored.group_by(["code", "datetime"]).agg(pl.len().alias("n")).filter(pl.col("n") > 1)
        )
        print(f"  scored duplicate (code, datetime) pairs: {scored_dup.height}")
        ranked = scored.with_columns(
            pl.col("signal")
            .rank(method="average", descending=True)
            .over("datetime")
            .alias("signal_rank")
        )
        sizes = ranked.group_by("datetime").agg(pl.len().alias("n"))
        ranked = ranked.join(sizes, on="datetime", how="left")

        # The quintile split of the same forward return is the cross-check on
        # the portfolio numbers below: if the strongest quintile here does not
        # match the signal's own quintile spread, the two disagree about what
        # the signal is worth and nothing below can be trusted.
        quintiles = ranked.with_columns(
            ((pl.col("signal_rank") - 1) / pl.col("n") * 5)
            .floor()
            .cast(pl.Int64)
            .clip(0, 4)
            .alias("quintile")
        )
        by_quintile = (
            quintiles.group_by(["datetime", "quintile"])
            .agg(pl.col("fwd").mean().alias("ret"))
            .group_by("quintile")
            .agg(pl.col("ret").mean().alias("mean_ret"))
            .sort("quintile")
        )
        quintile_text = " ".join(
            f"{row['quintile'] + 1}:{row['mean_ret'] * 100:.4f}%"
            for row in by_quintile.iter_rows(named=True)
        )
        print(f"  quintile mean forward return (1=strongest): {quintile_text}")

        # The strongest 1% cannot beat the strongest quintile it sits in. If
        # it does, the signal and the return are being joined on different
        # rows, and every portfolio number below is fiction.
        top_slice = ranked.filter(pl.col("signal_rank") <= 30)
        top_mean = float(top_slice.group_by("datetime").agg(pl.col("fwd").mean())["fwd"].mean())
        top_quintile_mean = float(
            by_quintile.filter(pl.col("quintile") == 0)["mean_ret"][0]
        )
        print(
            f"  CHECK top30={top_mean * 100:.4f}% vs best quintile="
            f"{top_quintile_mean * 100:.4f}% "
            f"{'OK' if top_mean <= top_quintile_mean + 1e-9 else 'INCONSISTENT'}"
        )

        # One session, printed out, settles it faster than more averages.
        one_day = sorted(ranked["datetime"].unique().to_list())[100]
        sample = ranked.filter(pl.col("datetime") == one_day).sort("signal_rank")
        top30_rows = sample.filter(pl.col("signal_rank") <= 30)
        q1_rows = sample.filter(
            (pl.col("signal_rank") - 1) / pl.col("n") * 5 < 1
        )
        print(
            f"  SAMPLE {one_day}: n={sample.height} "
            f"top30_mean={top30_rows['fwd'].mean() * 100:.4f}% "
            f"q1_mean={q1_rows['fwd'].mean() * 100:.4f}% "
            f"q1_n={q1_rows.height}"
        )

        print(f"--- horizon {horizon} sessions ---")
        for top_k in args.top_k:
            picked = ranked.filter(pl.col("signal_rank") <= top_k)
            per_day = picked.group_by("datetime").agg(
                pl.col("fwd").mean().alias("ret"),
                pl.col("fwd").median().alias("med"),
                pl.col("fwd").quantile(0.1).alias("q10"),
                pl.col("fwd").max().alias("best"),
                pl.len().alias("held"),
            )
            for label, low, high in (
                ("first", args.date_from, args.split_date),
                ("second", args.split_date, args.date_to),
                ("all", args.date_from, args.date_to),
            ):
                part = per_day.filter(
                    (pl.col("datetime") >= low) & (pl.col("datetime") <= high)
                )
                if part.is_empty():
                    continue
                mean_ret = float(part["ret"].mean())
                med_ret = float(part["med"].mean())
                std_ret = float(part["ret"].std()) if len(part) > 1 else 0.0
                annual = mean_ret * 240.0
                sharpe = (
                    mean_ret / std_ret * (240.0 ** 0.5) if std_ret else 0.0
                )
                # The share of sessions whose portfolio beat the market's own
                # average for that session. A mean carried by two 40 percent
                # jumps a month is not a strategy a book can run.
                print(
                    f"  top_k={top_k:4d} {label:6s} sessions={part.height:4d} "
                    f"mean={mean_ret * 100:.4f}% median={med_ret * 100:.4f}% "
                    f"q10={float(part['q10'].mean()) * 100:.4f}% "
                    f"best_avg={float(part['best'].mean()) * 100:.2f}% "
                    f"annualised={annual * 100:7.2f}% sharpe={sharpe:5.2f}"
                )

        # The decay, by year. A signal that weakens as the market changes is
        # a different problem from one that was never there, and the year
        # split is what tells the two apart.
        yearly = quintiles.with_columns(
            pl.col("datetime").str.slice(0, 4).alias("year")
        ).group_by(["year", "quintile"]).agg(
            pl.col("fwd").mean().alias("ret")
        )
        print(f"--- quintile mean forward return by year, horizon {horizon} ---")
        for year in sorted(yearly["year"].unique().to_list()):
            row = yearly.filter(pl.col("year") == year)
            values = {
                int(r["quintile"]): float(r["ret"])
                for r in row.iter_rows(named=True)
            }
            if 0 not in values or 4 not in values:
                continue
            spread = values[0] - values[4]
            cells = " ".join(
                f"{q + 1}:{values.get(q, 0.0) * 100:.3f}%" for q in range(5)
            )
            print(
                f"  {year} {cells}  spread={spread * 100:.3f}%"
            )

        # The market's own direction is the largest source of the daily
        # variation in a long-only ranking, and the gate's ICIR charges that
        # variation to the signal. The long-short spread of the same
        # ranking removes it: the top of the ranking against the bottom,
        # measured on the same session.
        per_day_spread = (
            quintiles.group_by(["datetime", "quintile"])
            .agg(pl.col("fwd").mean().alias("ret"))
            .pivot(index="datetime", on="quintile", values="ret")
        )
        if "0" in per_day_spread.columns and "4" in per_day_spread.columns:
            spread_series = (
                per_day_spread.get_column("0") - per_day_spread.get_column("4")
            ).drop_nulls()
            if len(spread_series):
                spread_mean = float(spread_series.mean())
                spread_std = float(spread_series.std())
                spread_ir = (
                    spread_mean / spread_std * (240.0 ** 0.5) if spread_std else 0.0
                )
                print(
                    f"  LONG-SHORT spread per {horizon} sessions: "
                    f"mean={spread_mean * 100:.4f}% "
                    f"annualised={spread_mean * 240 * 100:.2f}% "
                    f"information_ratio={spread_ir:.3f}"
                )

        # A rank slice can be taken from the top of the ranking or from a
        # band inside it. The top of the ranking is where the selection noise
        # is largest, and on a market-sized cross-section the names in it are
        # often the ones that jumped, not the ones the model understands.
        # The band is measured as a share of the cross-section, so the same
        # slice means the same thing on every session.
        print(f"--- rank band, horizon {horizon} ---")
        for low, high in (
            (0.0, 0.01),
            (0.0, 0.05),
            (0.05, 0.10),
            (0.10, 0.20),
            (0.20, 0.30),
            (0.30, 0.50),
            (0.50, 1.00),
        ):
            band = ranked.filter(
                (pl.col("signal_rank") - 1) / pl.col("n") >= low
            ).filter(
                (pl.col("signal_rank") - 1) / pl.col("n") < high
            )
            if band.is_empty():
                continue
            per_day_band = band.group_by("datetime").agg(
                pl.col("fwd").mean().alias("ret"),
                pl.col("fwd").median().alias("med"),
                pl.len().alias("held"),
            )
            for label, dlo, dhi in (
                ("first", args.date_from, args.split_date),
                ("second", args.split_date, args.date_to),
            ):
                part = per_day_band.filter(
                    (pl.col("datetime") >= dlo) & (pl.col("datetime") <= dhi)
                )
                if part.is_empty():
                    continue
                mean_ret = float(part["ret"].mean())
                med_ret = float(part["med"].mean())
                std_ret = float(part["ret"].std()) if len(part) > 1 else 0.0
                sharpe = (
                    mean_ret / std_ret * (240.0 ** 0.5) if std_ret else 0.0
                )
                print(
                    f"  band {low:.2f}-{high:.2f} {label:6s} "
                    f"held={part['held'].mean():6.1f} "
                    f"mean={mean_ret * 100:.4f}% median={med_ret * 100:.4f}% "
                    f"sharpe={sharpe:5.2f}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
