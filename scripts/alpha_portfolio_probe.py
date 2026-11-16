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

import numpy as np
import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
BARS: str = "D:/Codex/work/vnpy-alpha-cache/stock_bars"
SIGNAL: str = "D:/Codex/work/vnpy-alpha-cache/sweeps/market3000_rolling900_250.parquet"


def market_beta(paired: pl.DataFrame) -> dict:
    """Split a book's forward return into the market's part and the rest.

    A long-only book of the strongest names is, to first order, a leveraged
    claim on the market's own direction that day. Regressing the book's mean
    forward return on the cross-section's mean forward return gives back the
    part of the book that the market paid for (the slope) and the part it
    did not (the intercept).

    This matters because a rank IC is unchanged by a shift while a book is
    not. On 2026 data the ranking ordered every quintile as well as it ever
    had while the book lost money, and the only thing that had moved was the
    market. Without this split the two are indistinguishable.

    ``paired`` carries one row per session with ``book`` and ``market``.
    """
    if paired.height < 3:
        return {}
    markets = paired["market"].to_numpy()
    books = paired["book"].to_numpy()
    slope = float(np.polyfit(markets, books, 1)[0])
    alpha = float(books.mean() - slope * markets.mean())
    if not markets.std() or not books.std():
        corr = 0.0
    else:
        corr = float(np.corrcoef(markets, books)[0, 1])
    return {
        "days": paired.height,
        "book": float(books.mean()),
        "market": float(markets.mean()),
        "alpha": alpha,
        "beta": slope,
        "corr": corr,
    }


def neutral_top_slice(scored: pl.DataFrame, top_k: int) -> pl.DataFrame:
    """The top slice paid relative to the session's own mean, not absolute.

    Subtracting the cross-section's own mean forward return from every name
    leaves a holding whose outcome does not depend on the market's direction,
    which is the part a signal can be asked to produce. The absolute version
    of the same book is what a long-only strategy earns and it is mostly beta.
    """
    relative = scored.with_columns(
        (pl.col("fwd") - pl.col("fwd").mean().over("datetime")).alias("rel")
    )
    return (
        relative.filter(pl.col("signal_rank") <= top_k)
        .group_by("datetime")
        .agg(pl.col("rel").mean().alias("rel"))
    )


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
        # Keep the quintile on the ranked frame too, so the alignment check
        # below can ask of any row which quintile it sits in.
        ranked = ranked.join(
            quintiles.select(["datetime", "code", "quintile"]),
            on=["datetime", "code"],
            how="left",
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

        # What actually proves the join is aligned is a subset relation, not
        # an inequality of averages. The top 30 names of a session are the 30
        # strongest of that session, so every one of them has to sit inside
        # the session's own strongest quintile. A row outside it means the
        # signal and the return were joined on different rows.
        #
        # The earlier check compared the top-30 mean against the cross-day
        # average of the quintile means. Those are two different averages of
        # two different populations: a 30-name subset of a 579-name quintile
        # is free to out-earn the quintile it lives in, because the forward
        # returns are right-skewed and the top of the ranking is where the
        # skew sits. On the flagged session the subset earned 3.59% against
        # the quintile's 1.63% while every row of the subset was inside the
        # quintile, so the old reading called a correct join inconsistent.
        top_slice = ranked.filter(pl.col("signal_rank") <= 30)
        top_mean = float(top_slice.group_by("datetime").agg(pl.col("fwd").mean())["fwd"].mean())
        top_slice_size = 30
        # A session whose scored cross-section holds fewer than five times
        # the book cannot hold the subset relation: with fewer names than
        # the quintile boundary needs, a rank inside the top slice can sit
        # outside the strongest quintile at the same time. On 2026-11-06
        # only 114 names had a forward price, so the boundary was 22 and
        # ranks 24 to 30 were inside the slice and outside the quintile.
        # That is a property of the session, not a broken join.
        thick = ranked.filter(pl.col("n") >= top_slice_size * 5)
        outside = int(
            thick.filter(
                (pl.col("signal_rank") <= top_slice_size) & (pl.col("quintile") != 0)
            ).height
        )
        top_quintile_mean = float(
            by_quintile.filter(pl.col("quintile") == 0)["mean_ret"][0]
        )
        print(
            f"  CHECK top30={top_mean * 100:.4f}% vs best quintile="
            f"{top_quintile_mean * 100:.4f}% "
            f"top30_outside_quintile0={outside} "
            f"{'OK' if outside == 0 else 'MISALIGNED JOIN'}"
        )

        # A session where the scored cross-section is smaller than the book
        # it is choosing from cannot hold the subset relation: with fewer
        # names than five per quintile of the top slice, ranks past the
        # quintile boundary are inside the top slice and outside the quintile
        # at the same time. That is a property of the session, not a broken
        # join, so it is reported rather than counted against the check.
        thin = ranked.filter(pl.col("n") < top_slice_size * 5)
        if thin.height:
            thin_days = sorted(thin["datetime"].unique().to_list())
            print(
                f"  note {thin.height} rows on {len(thin_days)} sessions had "
                f"fewer than {top_slice_size * 5} scored names "
                f"({thin_days[0]} to {thin_days[-1]}), so the top-slice "
                "subset check does not apply to them"
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

        # The book's outcome and the market's outcome are not separable by
        # looking at the book alone. A long-only ranking of 30 names is
        # largely a leveraged claim on the market's own direction, so a
        # year in which the book loses money can be a year in which the
        # ranking was as good as it ever was. This splits the two: the
        # book's mean forward return is regressed on the cross-section's
        # own mean forward return, and what is left after the market is
        # charged to the signal is the alpha.
        print(f"--- market beta attribution, horizon {horizon} ---")
        book = ranked.filter(pl.col("signal_rank") <= 30)
        book_daily = book.group_by("datetime").agg(
            pl.col("fwd").mean().alias("book")
        )
        market_daily = ranked.group_by("datetime").agg(
            pl.col("fwd").mean().alias("market")
        )
        paired = book_daily.join(market_daily, on="datetime", how="inner")
        attribution = market_beta(paired)
        if attribution:
            print(
                f"  all days={attribution['days']} "
                f"book={attribution['book'] * 100:.4f}% "
                f"market={attribution['market'] * 100:.4f}% "
                f"alpha={attribution['alpha'] * 100:.4f}% "
                f"beta={attribution['beta']:.2f} "
                f"corr={attribution['corr']:.2f}"
            )
            for year in sorted(
                paired["datetime"].str.slice(0, 4).unique().to_list()
            ):
                part = paired.filter(
                    pl.col("datetime").str.slice(0, 4) == year
                )
                year_attribution = market_beta(part)
                if not year_attribution:
                    continue
                print(
                    f"  {year} days={year_attribution['days']} "
                    f"book={year_attribution['book'] * 100:.4f}% "
                    f"market={year_attribution['market'] * 100:.4f}% "
                    f"alpha={year_attribution['alpha'] * 100:.4f}% "
                    f"beta={year_attribution['beta']:.2f} "
                    f"corr={year_attribution['corr']:.2f}"
                )

        # The market-neutral version of the same book. Subtracting the
        # session's own mean forward return from every name leaves a
        # holding that is paid for selecting names rather than for the
        # market's direction, which is the only part a signal can be
        # asked to produce.
        print(f"--- market-neutral top-30, horizon {horizon} ---")
        neutral_daily = neutral_top_slice(ranked, 30)
        for label, low, high in (
            ("all", args.date_from, args.date_to),
        ):
            part = neutral_daily.filter(
                (pl.col("datetime") >= low) & (pl.col("datetime") <= high)
            )
            if part.is_empty():
                continue
            mean_rel = float(part["rel"].mean())
            std_rel = float(part["rel"].std()) if len(part) > 1 else 0.0
            sharpe_rel = (
                mean_rel / std_rel * (240.0 ** 0.5) if std_rel else 0.0
            )
            print(
                f"  {label} sessions={part.height} "
                f"rel_mean={mean_rel * 100:.4f}% "
                f"annualised={mean_rel * 240 * 100:.2f}% "
                f"sharpe={sharpe_rel:.2f}"
            )
            for year in sorted(
                part["datetime"].str.slice(0, 4).unique().to_list()
            ):
                slot = part.filter(pl.col("datetime").str.slice(0, 4) == year)
                if slot.height < 3:
                    continue
                slot_mean = float(slot["rel"].mean())
                slot_std = float(slot["rel"].std()) if slot.height > 1 else 0.0
                slot_ir = (
                    slot_mean / slot_std * (240.0 ** 0.5) if slot_std else 0.0
                )
                print(
                    f"    {year} days={slot.height} "
                    f"rel_mean={slot_mean * 100:.4f}% "
                    f"sharpe={slot_ir:.2f}"
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
