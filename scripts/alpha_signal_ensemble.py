"""Do two different factor sets produce two different signals?

The gate is graded on the stability of the daily ranking, and the two factor
sets available here are built from different expressions: Alpha158 from
rolling price and volume statistics, Alpha101 from WorldQuant's
cross-sectional expressions. A model fitted on one of them makes different
mistakes from a model fitted on the other, and the question is whether the
mistakes cancel.

Ensembling only helps if the two signals disagree in a way that is noise for
both and signal for neither. The controls here answer that: if the
ensemble's ICIR beat both members, the combination is carrying information
neither had. If it sits between them, the members are saying the same thing
and the ensemble is only averaging away some of the estimate's variance,
which is a different and much smaller effect.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
LAB: str = "D:/Codex/work/vnpy-alpha-cache/alpha_lab"
SWEEPS: str = "D:/Codex/work/vnpy-alpha-cache/sweeps"


def daily_ic(joined: pl.DataFrame) -> pl.Series:
    pdf = joined.to_pandas()
    if pdf.empty:
        return pl.Series("ic", [], dtype=pl.Float64)

    def rank_ic(group) -> float:
        sig = group["signal"]
        lab = group["label"]
        if sig.nunique() < 2 or lab.nunique() < 2:
            return float("nan")
        return float(np.corrcoef(sig.rank(), lab.rank())[0, 1])

    ic = pdf.groupby("datetime").apply(rank_ic, include_groups=False).dropna()
    return pl.Series("ic", ic.astype(float).to_list())


def stats(ic: pl.Series) -> dict:
    if len(ic) == 0:
        return {"sessions": 0, "ic_mean": 0.0, "icir": 0.0, "pos": 0.0}
    return {
        "sessions": len(ic),
        "ic_mean": float(ic.mean()),
        "icir": float(ic.mean() / ic.std()) if ic.std() else 0.0,
        "pos": float((ic > 0).mean()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab", default=LAB)
    parser.add_argument("--sweeps", default=SWEEPS)
    parser.add_argument(
        "--members",
        nargs="+",
        default=["m800clean_rolling900_250", "m800a101_rolling900_250"],
        help="cached signal names under the sweeps directory",
    )
    parser.add_argument("--dataset", default="m800clean")
    parser.add_argument("--date-from", default="2024-09-26")
    parser.add_argument("--date-to", default="2026-11-13")
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab

    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print("dataset missing")
        return 2

    label = dataset.result_df.select(["datetime", "vt_symbol", "label"])
    label = label.with_columns(pl.col("datetime").cast(pl.Utf8).str.slice(0, 10))

    frames: list[pl.DataFrame] = []
    for name in args.members:
        path = Path(args.sweeps) / f"{name}.parquet"
        if not path.exists():
            print(f"missing signal cache: {path}")
            return 2
        frame = pl.read_parquet(path)
        frame = frame.with_columns(
            pl.col("datetime").cast(pl.Utf8).str.slice(0, 10).alias("datetime")
        )
        frame = frame.select(["datetime", "vt_symbol", "signal"])
        # The member's score is renamed so the join can keep both; the
        # scoring helper always looks for a column called "signal".
        frame = frame.rename({"signal": "signal_member"})
        frames.append(frame)
        print(f"loaded {name}: {frame.height} rows")

    joined = frames[0]
    for index, frame in enumerate(frames[1:], start=1):
        joined = joined.join(
            frame.rename({"signal_member": f"signal_{index}"}),
            on=["datetime", "vt_symbol"],
            how="inner",
        )
    joined = joined.join(label, on=["datetime", "vt_symbol"], how="inner")
    joined = joined.filter(
        (pl.col("datetime") >= args.date_from) & (pl.col("datetime") <= args.date_to)
    )
    print(f"joined rows: {joined.height}")

    columns = [f"signal_{i}" for i in range(len(frames))]
    # The first member's column is what the join left in place, so it keeps
    # the original name.
    columns[0] = "signal_member"
    for index, column in enumerate(columns):
        frame = joined.select(
            ["datetime", "label", pl.col(column).alias("signal")]
        )
        measured = stats(daily_ic(frame))
        print(
            f"member {index} ({args.members[index]}): "
            f"ic={measured['ic_mean']:.4f} icir={measured['icir']:.3f} "
            f"pos={measured['pos']:.3f}"
        )

    # The correlation between the members' daily rankings says whether they
    # are two views of one thing or two different things.
    if len(columns) == 2:
        first = joined.select(["datetime", "vt_symbol", columns[0]]).rename(
            {columns[0]: "a"}
        )
        second = joined.select(["datetime", "vt_symbol", columns[1]]).rename(
            {columns[1]: "b"}
        )
        pair = first.join(second, on=["datetime", "vt_symbol"], how="inner")
        pair = pair.with_columns(
            pl.col("a").rank().over("datetime").alias("ra"),
            pl.col("b").rank().over("datetime").alias("rb"),
        )
        daily_corr = pair.group_by("datetime").agg(
            pl.corr("ra", "rb").alias("corr")
        )
        print(
            f"daily rank correlation between members: "
            f"{float(daily_corr['corr'].mean()):.4f}"
        )

    # The ensemble is a rank average, so both members contribute on their own
    # scale and neither can dominate by having a wider spread.
    ranked = joined.with_columns(
        [
            pl.col(column).rank(method="average").over("datetime").alias(f"r{index}")
            for index, column in enumerate(columns)
        ]
    )
    ranked = ranked.with_columns(
        pl.mean_horizontal([f"r{index}" for index in range(len(columns))]).alias("signal")
    )
    measured = stats(daily_ic(ranked.select(["datetime", "signal", "label"])))
    print(
        f"ENSEMBLE rank average: ic={measured['ic_mean']:.4f} "
        f"icir={measured['icir']:.3f} pos={measured['pos']:.3f}"
    )

    # The same ensemble on the members' own scales, and a random member as a
    # control: an ensemble that beats a random half of itself is only
    # averaging noise.
    scaled = joined.with_columns(
        [
            (
                (pl.col(column) - pl.col(column).mean().over("datetime"))
                / pl.col(column).std().over("datetime")
            ).alias(f"z{index}")
            for index, column in enumerate(columns)
        ]
    )
    scaled = scaled.with_columns(
        pl.mean_horizontal([f"z{index}" for index in range(len(columns))]).alias("signal")
    )
    measured = stats(daily_ic(scaled.select(["datetime", "signal", "label"])))
    print(
        f"ENSEMBLE z-score average: ic={measured['ic_mean']:.4f} "
        f"icir={measured['icir']:.3f} pos={measured['pos']:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
