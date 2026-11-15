"""Turn the float share count into a size information set for the model.

The research up to now has only had price and volume, so the size question
was answered by slicing the market on trailing turnover. The findings file
records that answer: the slices all read a lower ICIR than the whole market,
because slicing shrinks the cross-section rather than steadying the signal.

This module takes the other route. The float share count the kline host
already publishes is joined onto the bars as a real column, and the model
gets to see the size itself instead of the market being cut up around it.
That is the difference the findings file asks for: a size factor is a
feature, a size stratum is a filter, and only the first one can add
information the price factors do not already carry.

Every column here is built so it cannot look ahead. The float share count on
session T is the one the host published on session T, and the market cap is
that day's close times that day's count. The ratios use trailing windows
that end on the session being scored.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
FLOAT_SHARES: str = "D:/Codex/work/vnpy-alpha-cache/float_shares"


def load_float_shares(cache_dir: str = FLOAT_SHARES) -> pl.DataFrame:
    """Read every cached float share file into one frame.

    The per-symbol files carry the date and the share count, so this is a
    long frame keyed by code and date.
    """
    rows: list[dict] = []
    directory: Path = Path(cache_dir)
    if not directory.is_dir():
        return pl.DataFrame(
            schema={"code": pl.Utf8, "datetime": pl.Utf8, "float_shares": pl.Float64}
        )

    for path in sorted(directory.glob("*_float_shares.csv")):
        code: str = path.name.split("_")[0]
        with path.open(encoding="utf-8", newline="") as handle:
            import csv

            reader = csv.reader(handle)
            next(reader, None)
            for record in reader:
                if len(record) < 2:
                    continue
                try:
                    shares: float = float(record[1])
                except ValueError:
                    continue
                if shares <= 0:
                    continue
                rows.append(
                    {
                        "code": code,
                        "datetime": str(record[0])[:10],
                        "float_shares": shares,
                    }
                )

    if not rows:
        return pl.DataFrame(
            schema={"code": pl.Utf8, "datetime": pl.Utf8, "float_shares": pl.Float64}
        )
    return pl.DataFrame(rows).sort(["code", "datetime"])


def load_bars(bars_dir: str = "D:/Codex/work/vnpy-alpha-cache/stock_bars") -> pl.DataFrame:
    """Read the cached bars and attach the size columns.

    The bars carry volume but not turnover, so the amount column is volume
    times close, the same definition the universe scan uses.
    """
    shares: pl.DataFrame = load_float_shares()
    if shares.is_empty():
        raise RuntimeError("no float share files found; fetch them first")

    rows: list[dict] = []
    for path in sorted(Path(bars_dir).glob("*_*_101.csv")):
        code: str = path.stem.split("_")[1]
        with path.open(encoding="utf-8") as handle:
            lines: list[str] = handle.readlines()
        for line in lines[1:]:
            parts: list[str] = line.strip().split(",")
            if len(parts) < 7:
                continue
            dt, _open, close, _high, _low, volume, _symbol = parts[:7]
            try:
                close_value: float = float(close)
                volume_value: float = float(volume)
            except ValueError:
                continue
            rows.append(
                {
                    "code": code,
                    "datetime": str(dt)[:10],
                    "close": close_value,
                    "volume": volume_value,
                    "amount": volume_value * close_value,
                }
            )

    bars: pl.DataFrame = pl.DataFrame(rows).sort(["code", "datetime"])
    merged: pl.DataFrame = bars.join(shares, on=["code", "datetime"], how="left")
    return merged.with_columns(
        (pl.col("close") * pl.col("float_shares")).alias("float_cap")
    )


def add_size_features(
    frame: pl.DataFrame,
    windows: tuple[int, ...] = (5, 20, 60),
) -> pl.DataFrame:
    """Build the size factor columns on a long frame.

    The frame is one row per symbol per session, sorted by symbol and date.
    The rolling windows are trailing, so a factor on session T only sees
    sessions up to T.
    """
    out: pl.DataFrame = frame
    if "float_cap" not in out.columns:
        out = out.with_columns(
            (pl.col("close") * pl.col("float_shares")).alias("float_cap")
        )

    # The log of the market cap is the size itself, on a scale where a
    # doubling is a fixed step.
    out = out.with_columns(
        pl.col("float_cap").log().alias("log_float_cap"),
    )

    # How the size moved over each window, as a ratio to the size at the
    # start of the window. This is the share issuance and buyback signal,
    # which the price factors cannot see.
    for window in windows:
        out = out.with_columns(
            (
                pl.col("float_cap")
                / pl.col("float_cap").shift(window).over("code")
                - 1.0
            ).alias(f"cap_change_{window}"),
        )

    # The share count's own change, separated from the price change, so the
    # model can tell dilution from a falling price.
    for window in windows:
        out = out.with_columns(
            (
                pl.col("float_shares")
                / pl.col("float_shares").shift(window).over("code")
                - 1.0
            ).alias(f"share_change_{window}"),
        )

    # Turnover against size: how hard the name trades for its own weight.
    out = out.with_columns(
        (
            pl.col("amount")
            / pl.col("float_cap").clip(lower_bound=1.0)
        ).alias("turnover_of_cap"),
    )

    # The size rank inside the session, which is what a live filter could
    # act on, and the model's way of seeing "small" without carrying the
    # absolute number.
    out = out.with_columns(
        pl.col("float_cap")
        .rank(method="average")
        .over("datetime")
        .alias("cap_rank"),
    )
    session_count = out.group_by("datetime").agg(pl.len().alias("n_names"))
    out = out.join(session_count, on="datetime", how="left")
    out = out.with_columns(
        (pl.col("cap_rank") / pl.col("n_names")).alias("cap_pct"),
    )

    return out


def main() -> int:
    """Print a summary of the size information set that was built."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--float-shares", default=FLOAT_SHARES)
    parser.add_argument("--bars", default="D:/Codex/work/vnpy-alpha-cache/stock_bars")
    parser.add_argument(
        "--out",
        default="D:/Codex/work/vnpy-alpha-cache/sweeps/size_factors.parquet",
        help="where to write the joined frame",
    )
    args = parser.parse_args()

    frame: pl.DataFrame = load_bars(args.bars)
    print(f"bars rows={frame.height} names={frame['code'].n_unique()}")

    covered: int = frame.filter(pl.col("float_shares").is_not_null()).height
    print(f"float share coverage: {covered}/{frame.height} = {covered / max(frame.height, 1):.1%}")

    sized: pl.DataFrame = add_size_features(frame)
    target: Path = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    sized.write_parquet(target)
    print(f"wrote {target} rows={sized.height}")

    last: pl.DataFrame = (
        sized.filter(pl.col("datetime") == sized["datetime"].max())
        .select(["code", "close", "float_shares", "float_cap", "cap_pct"])
        .sort("float_cap", descending=True)
    )
    print("largest names on the last session:")
    for row in last.head(10).iter_rows(named=True):
        print(
            f"  {row['code']} close={row['close']:.2f} "
            f"shares={row['float_shares']:,.0f} cap={row['float_cap']:,.0f} "
            f"pct={row['cap_pct']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
