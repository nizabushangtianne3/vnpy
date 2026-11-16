"""Build a dataset that also knows the size of every name.

The 158 factors are all price and volume, so the model has never been told
how big a company is. The findings file calls that the information set that
was never tried, and it is right: a large bank and a small chip designer do
not answer the same momentum factor the same way.

This module builds the size-aware dataset. It takes the bars the lab
already cached, joins the float share count the kline host publishes for
every session, and adds the size factors as extra columns on the dataset's
own frame. The model then sees the size directly instead of the market
being sliced around it.

The market cap is computed from the turnover and the volume rather than
from the close, because the lab normalises every price by the first close
it sees. The normalised close is the right input for a return factor and
the wrong input for a market cap: 0.99 means something different for a
100-yuan stock and a 5-yuan one. ``turnover / volume`` is the session's
average traded price in yuan, so the cap comes out in yuan too.

The factors are all trailing, so a factor on session T only sees sessions
up to T, and the size on session T is the share count the host published on
session T.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
FLOAT_SHARES: str = "D:/Codex/work/vnpy-alpha-cache/float_shares"


def load_float_shares(cache_dir: str = FLOAT_SHARES) -> pl.DataFrame:
    """Read every cached float share file into one frame."""
    import csv

    rows: list[dict] = []
    directory: Path = Path(cache_dir)
    if not directory.is_dir():
        return pl.DataFrame(
            schema={"code": pl.Utf8, "datetime": pl.Utf8, "float_shares": pl.Float64}
        )

    for path in sorted(directory.glob("*_float_shares.csv")):
        code: str = path.name.split("_")[0]
        with path.open(encoding="utf-8", newline="") as handle:
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


def symbol_to_code(vt_symbol: str) -> str:
    """The six-digit code behind a vt_symbol."""
    return vt_symbol.split(".")[0]


def attach_size(
    df: pl.DataFrame,
    cache_dir: str = FLOAT_SHARES,
    windows: tuple[int, ...] = (5, 20, 60),
) -> pl.DataFrame:
    """Join the float share count onto a bar frame and build the size factors.

    The frame is expected to carry ``datetime``, ``vt_symbol``, ``turnover``
    and ``volume``. The size columns are appended to it.
    """
    shares: pl.DataFrame = load_float_shares(cache_dir)
    if shares.is_empty():
        raise RuntimeError(f"no float share files under {cache_dir}")

    out: pl.DataFrame = df.with_columns(
        pl.col("vt_symbol").map_elements(symbol_to_code, return_dtype=pl.Utf8).alias("code")
    )
    out = out.with_columns(
        pl.col("datetime").cast(pl.Utf8).str.slice(0, 10).alias("date_text")
    )

    joined: pl.DataFrame = out.join(
        shares.rename({"datetime": "date_text"}),
        on=["code", "date_text"],
        how="left",
    )

    covered: int = joined.filter(pl.col("float_shares").is_not_null()).height
    print(f"float share coverage: {covered}/{joined.height} = {covered / max(joined.height, 1):.1%}")

    # The average traded price in yuan, then the market cap in yuan.
    # A suspended session has no volume, so the cap falls back to the
    # previous session's close times the share count.
    joined = joined.with_columns(
        pl.when(pl.col("volume") > 0)
        .then(pl.col("turnover") / pl.col("volume"))
        .otherwise(None)
        .alias("avg_price")
    )
    joined = joined.with_columns(
        pl.col("avg_price")
        .forward_fill()
        .over("vt_symbol")
        .alias("price_filled")
    )
    joined = joined.with_columns(
        (pl.col("price_filled") * pl.col("float_shares")).alias("float_cap")
    )

    # The size factors themselves.
    joined = joined.with_columns(
        pl.col("float_cap").log().alias("size_log_cap"),
    )

    for window in windows:
        joined = joined.with_columns(
            (
                pl.col("float_cap")
                / pl.col("float_cap").shift(window).over("vt_symbol")
                - 1.0
            ).alias(f"size_cap_change_{window}")
        )
        joined = joined.with_columns(
            (
                pl.col("float_shares")
                / pl.col("float_shares").shift(window).over("vt_symbol")
                - 1.0
            ).alias(f"size_share_change_{window}")
        )

    # The size rank inside the session: the model's way of seeing "small"
    # without carrying the absolute number, and the one version of the idea
    # a live filter could act on.
    joined = joined.with_columns(
        pl.col("float_cap").rank(method="average").over("datetime").alias("size_cap_rank")
    )
    counts = joined.group_by("datetime").agg(pl.len().alias("size_names"))
    joined = joined.join(counts, on="datetime", how="left")
    joined = joined.with_columns(
        (pl.col("size_cap_rank") / pl.col("size_names")).alias("size_cap_pct")
    )

    # The size relative to the market's own size, so the factor means the
    # same thing on a day when the whole market re-rated.
    joined = joined.with_columns(
        pl.col("float_cap").log().mean().over("datetime").alias("size_market_log_cap")
    )
    joined = joined.with_columns(
        (pl.col("size_log_cap") - pl.col("size_market_log_cap")).alias("size_relative")
    )

    # How hard the name trades for its own size.
    joined = joined.with_columns(
        (pl.col("turnover") / pl.col("float_cap").clip(lower_bound=1.0)).alias("size_turnover")
    )

    # The share count's own move, separated from the price's. A change in
    # the market cap is the sum of the two, and the model cannot tell them
    # apart on its own: a company that issued stock and one whose price rose
    # by the same percentage look identical in the cap column. The share
    # count is the one that is information the price factors cannot see,
    # because the price factors are all functions of the price.
    for window in windows:
        joined = joined.with_columns(
            pl.col("float_shares")
            .shift(window)
            .over("vt_symbol")
            .alias(f"size_shares_prev_{window}")
        )
        joined = joined.with_columns(
            (
                pl.col("float_shares")
                / pl.col(f"size_shares_prev_{window}")
                - 1.0
            ).alias(f"size_issue_{window}")
        )
        joined = joined.with_columns(
            (
                pl.col("price_filled")
                / pl.col("price_filled").shift(window).over("vt_symbol")
                - 1.0
            ).alias(f"size_price_move_{window}")
        )

    return joined


def size_feature_columns(windows: tuple[int, ...] = (5, 20, 60)) -> list[str]:
    """The names of the columns the model should read as size factors."""
    columns: list[str] = ["size_log_cap", "size_cap_pct", "size_relative", "size_turnover"]
    for window in windows:
        columns.append(f"size_cap_change_{window}")
        columns.append(f"size_share_change_{window}")
        columns.append(f"size_issue_{window}")
        columns.append(f"size_price_move_{window}")
    return columns


def main() -> int:
    """Build the size-aware dataset and report what it covers."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--float-shares", default=FLOAT_SHARES)
    parser.add_argument("--dataset", default="market3000")
    parser.add_argument("--name", default="market3000_size")
    parser.add_argument("--lab", default="D:/Codex/work/vnpy-alpha-cache/alpha_lab")
    parser.add_argument(
        "--build",
        action="store_true",
        help="save the size-aware dataset to the lab",
    )
    parser.add_argument(
        "--out",
        default="D:/Codex/work/vnpy-alpha-cache/sweeps/size_dataset_probe.parquet",
    )
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab

    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print("dataset missing")
        return 2

    sized: pl.DataFrame = attach_size(dataset.df, args.float_shares)

    if args.build:
        build_size_dataset(dataset, sized, lab, args.name)
        print(f"saved dataset {args.name} to {args.lab}")
        return 0

    target: Path = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    sized.write_parquet(target)
    print(f"wrote {target} rows={sized.height}")

    columns: list[str] = size_feature_columns()
    print(f"size factor columns: {columns}")
    for name in columns:
        values: pl.Series = sized[name]
        finite: int = values.is_finite().sum()
        print(f"  {name}: finite={finite}/{values.len()}")
    return 0


def build_size_dataset(
    dataset,
    sized: pl.DataFrame,
    lab,
    name: str,
) -> None:
    """Write a dataset that carries the size factors.

    The size columns are appended to the dataset's own frame and the factors
    are then recalculated, so the model reads them the same way it reads the
    158 price factors: as columns of ``learn_df`` and ``infer_df``.

    The price columns are left exactly as they were. The lab normalises them
    by the first close it sees, and the size factors are built from turnover
    and volume, which are absolute, so nothing about the normalisation has
    to change.
    """
    columns: list[str] = size_feature_columns()

    augmented: object = augment_dataset(dataset, sized, columns)
    lab.save_dataset(name, augmented)
    print(f"dataset {name}: rows={augmented.result_df.height} factors={len(columns)}")


def augment_dataset(dataset, sized: pl.DataFrame, columns: list[str]):
    """Return a copy of the dataset with the size columns on every frame.

    The size columns are joined onto the frames the dataset already carries
    rather than recalculated through ``prepare_data``. Two reasons make that
    the right call:

    * The 158 price factors do not depend on the size columns, so
      recomputing them would produce the same numbers at the cost of seven
      minutes on a market-sized frame. Joining keeps them byte for byte
      identical, which is also what makes the comparison against the
      baseline fair.
    * A frame that was rebuilt could differ from the baseline in ways that
      have nothing to do with the factors being tested.

    A name the host had no share count for gets a missing value, and the
    missing value is written as NaN rather than as a number. That is the
    convention the 158 price factors already use, because ``prepare_data``
    fills their nulls with NaN, and the booster reads a NaN as a missing
    value rather than as a zero. Filling the gap with a median would put a
    number where the honest answer is "this name had no size on that day",
    and the size rank would then be a rank of invented values.
    """
    import copy

    size_frame: pl.DataFrame = sized.select(["datetime", "vt_symbol"] + columns)

    new_dataset = copy.deepcopy(dataset)
    for frame_name in ("df", "result_df", "raw_df", "infer_df", "learn_df"):
        frame: pl.DataFrame = getattr(new_dataset, frame_name)
        if "datetime" not in frame.columns or "vt_symbol" not in frame.columns:
            continue
        joined: pl.DataFrame = frame.drop(
            [c for c in columns if c in frame.columns]
        ).join(size_frame, on=["datetime", "vt_symbol"], how="left")
        for column in columns:
            joined = joined.with_columns(
                pl.col(column).fill_null(float("nan"))
            )
        # ``LgbModel`` reads its features as ``df.columns[2:-1]``, so the
        # label has to stay the last column of every frame it scores.
        # Appending the size columns after it tells the model the label is
        # a feature, and that reads as a near-perfect cross-sectional IC
        # because it is the answer it is being graded against. Reordering
        # the frame after the join keeps the label last.
        if "label" in frame.columns:
            head: list[str] = [
                c for c in joined.columns if c not in columns and c != "label"
            ]
            joined = joined.select(head + columns + ["label"])
        setattr(new_dataset, frame_name, joined)

    return new_dataset


if __name__ == "__main__":
    raise SystemExit(main())
