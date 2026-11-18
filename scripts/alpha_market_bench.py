"""Answer, without a sweep, whether the graded half'"'"'s loss is the market'"'"'s.

Every configuration of the book loses money on the graded half, and the loss
is 2026 in every one of them. That can mean two different things and they
lead to different work. If the whole market fell in 2026 and the book held
its selection, then the loss is beta and the honest response is to hedge it
or to stop calling the book neutral. If the market rose and the book still
lost, then the selection itself was paying, which is a much heavier finding
because it means the signal selects the wrong side.

The distinction is not something a return table can answer, because a return
table compares two books and both sides move. It needs a benchmark the book
is not built out of. Three are computed here, all from the same cached bars
the runner loads and none of them using the signal:

* equal weight, every stock, which is the selection'"'"'s own universe
* top decile by trailing turnover, which is where a liquid basket comes from
* the named ETF, which is the instrument the hedge leg actually trades

They are reported per year, so that a year in which the book loses while
every benchmark gains is separated from a year in which everything fell.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

DEFAULT_DAILY = "D:/Codex/work/vnpy-alpha-cache/alpha_lab/daily"


def load_close_frame(daily_dir: Path) -> pl.DataFrame:
    """Read one close per session per symbol out of the cached daily bars.

    The cached files are one symbol each and carry open, high, low, close
    and volume, so a close column is read from each and stacked. The file
    name is the vt symbol, so the parquet stem is already the key the
    runner uses and no exchange mapping has to be invented.
    """
    frames: list[pl.DataFrame] = []
    for path in sorted(daily_dir.glob("*.parquet")):
        frame: pl.DataFrame = pl.read_parquet(
            path, columns=["datetime", "close", "turnover"]
        )
        if frame.is_empty():
            continue
        frames.append(frame.with_columns(pl.lit(path.stem).alias("vt_symbol")))
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical").sort(["vt_symbol", "datetime"])


def session_returns(frame: pl.DataFrame, window: int) -> pl.DataFrame:
    """One close-to-close return per session, with a turnover-based liquidity rank.

    The return is one session, not ``window`` sessions, and that is the point.
    Compounding overlapping multi-session returns counts the same price move
    several times, so a year of them is not the year's return: on 510300 over
    2026, compounding 208 overlapping five-session returns reads -51.68 percent
    while the same year compounded session by session reads -15.71 percent and
    in non-overlapping five-session blocks reads -14.33 percent. The overlapping
    figure is roughly three times the truth, which is the kind of error that
    turns a market fall into a catastrophe.

    ``window`` is therefore used for one thing only, the liquidity rank, and it
    is measured on the trailing window's turnover taken strictly before the
    session it is used on, because a basket chosen on turnover that includes the
    session it buys is a basket chosen with knowledge of what it bought. The
    cached daily bars carry turnover directly, so it is read rather than
    approximated by price, which would rank the most expensive stocks rather
    than the most traded ones.
    """
    return (
        frame.sort(["vt_symbol", "datetime"])
        .with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"),
            pl.col("turnover")
            .rolling_mean(window_size=window, min_samples=window)
            .shift(1)
            .alias("turnover"),
        )
        .drop_nulls(["ret"])
    )


def benchmark_rows(frame: pl.DataFrame, label: str, top_decile: bool) -> list[dict]:
    """Per-session equal weight return of one universe, optionally the top decile."""
    if frame.is_empty():
        return []
    if top_decile:
        # quantile() returns an optional because the column may hold nulls, and
        # a null cutoff would drop every row rather than keep the liquid ones.
        cutoff: float | None = frame["turnover"].quantile(0.9)
        if cutoff is None:
            return []
        frame = frame.filter(pl.col("turnover") >= cutoff)
    return (
        frame.group_by("datetime")
        .agg(pl.col("ret").mean().alias("ret"), pl.len().alias("names"))
        .sort("datetime")
        .with_columns(pl.lit(label).alias("bench"))
        .select("datetime", "bench", "ret", "names")
        .to_dicts()
    )


def main() -> int:
    """Report the market benchmarks against the book'"'"'s own graded window."""
    parser = argparse.ArgumentParser(description="Market benchmark for the alpha book")
    parser.add_argument("--daily", default=DEFAULT_DAILY)
    parser.add_argument("--graded-start", default="2025-10-01")
    parser.add_argument("--graded-end", default="2026-11-13")
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    daily_dir: Path = Path(args.daily)
    frame: pl.DataFrame = load_close_frame(daily_dir)
    if frame.is_empty():
        print("no cached daily bars found")
        return 2

    print(
        f"symbols: {frame['vt_symbol'].n_unique()} "
        f"sessions: {frame['datetime'].n_unique()}"
    )

    stock: pl.DataFrame = frame.filter(pl.col("vt_symbol").str.contains("(SZSE|SSE)$"))
    stock = session_returns(stock, args.hold_days)

    rows: list[dict] = []
    rows.extend(benchmark_rows(stock, "equal_weight_all", top_decile=False))
    rows.extend(benchmark_rows(stock, "top_decile_turnover", top_decile=True))

    etf_path: Path = daily_dir / "510300.SSE.parquet"
    if etf_path.exists():
        etf: pl.DataFrame = session_returns(
            pl.read_parquet(etf_path).with_columns(
                pl.lit("510300.SSE").alias("vt_symbol")
            ),
            args.hold_days,
        )
        rows.extend(benchmark_rows(etf, "510300.SSE", top_decile=False))

    table: pl.DataFrame = pl.DataFrame(rows).filter(
        (pl.col("datetime") >= pl.lit(args.graded_start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(args.graded_end).str.to_datetime())
    )

    print(f"\n=== graded window {args.graded_start} to {args.graded_end} ===")
    summary: pl.DataFrame = (
        table.with_columns(pl.col("datetime").dt.year().alias("year"))
        .group_by("bench", "year")
        .agg(
            pl.len().alias("sessions"),
            pl.col("ret").log1p().sum().alias("log_ret"),
        )
        .sort(["bench", "year"])
        .with_columns(
            ((pl.col("log_ret").exp() - 1.0) * 100.0).round(3).alias("total_pct")
        )
        .drop("log_ret")
    )
    print(summary)

    if args.output:
        path: Path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(summary.write_json(), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
