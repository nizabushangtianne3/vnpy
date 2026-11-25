"""Blend cached cross-sectional signals without refitting a model.

Two signals can agree on the same universe but live on different scales. A
plain average would let the wider score dominate the book, so this tool first
puts every member on the same cross-sectional scale and then applies optional
weights. The output keeps the runner's expected ``datetime`` / ``vt_symbol`` /
``signal`` columns, so it can be fed straight into the engine.

Only sessions and symbols covered by every member are kept. That makes the
blended universe stable: a missing member cannot silently turn into a ranking
made from a smaller universe than the one the engine was sized against.
"""

from __future__ import annotations

import argparse
from functools import reduce
from pathlib import Path

import polars as pl


BLEND_METHODS = ("rank", "zscore")


def load_member(path: Path) -> pl.DataFrame:
    """Load one cached signal and keep only the blend key columns."""
    frame = pl.read_parquet(path).select(["datetime", "vt_symbol", "signal"])
    if frame.is_empty():
        raise ValueError(f"signal cache is empty: {path}")
    return frame.rename({"signal": f"signal_{path.stem}"})


def blend_members(
    members: list[pl.DataFrame],
    weights: list[float],
    method: str,
) -> pl.DataFrame:
    """Blend aligned signals into one runner-compatible signal frame."""
    joined = members[0]
    for member in members[1:]:
        joined = joined.join(member, on=["datetime", "vt_symbol"], how="inner")

    score_columns = [
        column
        for column in joined.columns
        if column not in {"datetime", "vt_symbol", "signal"}
    ]
    ordered_columns = ["datetime", "vt_symbol", *score_columns]
    joined = joined.select(ordered_columns)
    if method == "rank":
        columns = [
            pl.col(column).rank(method="average").over("datetime").alias(column)
            for column in score_columns
        ]
    else:
        columns = [
            (
                (pl.col(column) - pl.col(column).mean().over("datetime"))
                / pl.col(column).std().over("datetime")
            ).alias(column)
            for column in score_columns
        ]
    joined = joined.with_columns(columns)
    weighted_scores = [
        pl.col(column) * weight
        for column, weight in zip(score_columns, weights, strict=True)
    ]
    return joined.with_columns(
        reduce(lambda left, right: left + right, weighted_scores).alias("signal")
    ).select(["datetime", "vt_symbol", "signal"])


def main() -> int:
    """Read, blend and cache two or more signal parquets."""
    parser = argparse.ArgumentParser(description="Blend cached alpha signals")
    parser.add_argument("--signals", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--method", choices=BLEND_METHODS, default="rank")
    parser.add_argument(
        "--weights",
        nargs="+",
        type=float,
        default=None,
        help="one positive weight per signal; defaults to equal weights",
    )
    args = parser.parse_args()

    if len(args.signals) < 2:
        raise SystemExit("at least two signal caches are required")
    weights = args.weights if args.weights is not None else [1.0] * len(args.signals)
    if len(weights) != len(args.signals):
        raise SystemExit("the number of weights must match the number of signals")
    if any(weight <= 0 for weight in weights):
        raise SystemExit("blend weights must be positive")

    paths = [Path(path) for path in args.signals]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SystemExit(f"missing signal caches: {', '.join(missing)}")

    members = [load_member(path) for path in paths]
    blended = blend_members(members, weights, args.method)
    if blended.is_empty():
        raise SystemExit("signals have no overlapping datetime and symbol keys")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    blended.write_parquet(out)
    print(
        f"写 {args.method} 组合信号到 {out}：rows={blended.height}, "
        f"sessions={blended['datetime'].n_unique()}, "
        f"symbols={blended['vt_symbol'].n_unique()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
