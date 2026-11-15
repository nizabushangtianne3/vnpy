"""Does the signal hold together inside a size stratum?

The universe scan already measured the market-sized cross-section end to end,
and its answer was that the gate's ICIR of 0.50 is not reachable by changing
how the same 158 price-and-volume factors are fitted: fourteen ways of moving
it stayed between 0.36 and 0.49, and the closest one bought the ratio with
the level. The findings file records that conclusion along with the reason it
gives: the ratio behaves like a property of ranking 3021 names rather than a
property of the fit, and the gate that has not been tried is a different
information set.

A size stratum is the cheapest version of a different information set that
this data can actually supply. The cached bars carry no market cap, no
valuation and no fundamentals, but turnover is volume times price, and a
trailing mean of it is a direct proxy for how heavily the name trades. A
cap-weighted name and a penny name do not answer the same 158 factors in the
same way, so the honest question is not whether the whole market clears the
gate but whether the strata the signal applies to do.

The strata are cut on the as-of rank of trailing turnover, so a name's
stratum on session T is built only from sessions before T, and each stratum
is scored on the same sessions the full market is scored on. Everything else
- the rolling signal, the label, the daily IC computation - is imported from
the universe scan rather than reimplemented, so a number printed here and a
number printed there are the same measurement.

The trap this script exists to avoid: a narrower cross-section shrinks the
IC's own dispersion, so an ICIR gain inside a stratum can be an artefact of
having fewer names rather than a better signal. Every stratum is therefore
reported next to its own control, which is the same names scored on the same
sessions with the signal replaced by a random draw. A stratum only counts if
it beats its control, not only if it beats the full market.
"""

from __future__ import annotations

import argparse
import importlib.util
import random
from pathlib import Path

import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]


def load_scan():
    """Import alpha_universe_scan without running its CLI."""
    spec = importlib.util.spec_from_file_location(
        "alpha_universe_scan", str(REPO / "scripts" / "alpha_universe_scan.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def size_buckets(frame: pl.DataFrame, quantiles: int) -> pl.DataFrame:
    """Attach each session's as-of size bucket to every name.

    The trailing turnover column was built by the scan with a one-session
    shift, so the value on session T uses only sessions before T. Buckets are
    cut on that day's own cross-section, which is the only thing a live
    filter could know. The rank runs descending, so bucket 0 is the largest
    stratum by turnover.
    """
    ranked = frame.with_columns(
        pl.col("trailing_amount")
        .rank(method="average", descending=True)
        .over("datetime")
        .alias("size_rank")
    )
    counts = ranked.group_by("datetime").agg(pl.len().alias("n"))
    ranked = ranked.join(counts, on="datetime", how="left")
    return (
        ranked.with_columns(
            ((pl.col("size_rank") - 1) / pl.col("n") * quantiles)
            .floor()
            .cast(pl.Int64)
            .clip(0, quantiles - 1)
            .alias("size_bucket")
        )
        .select(["code", "datetime", "trailing_amount", "size_bucket"])
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="market3000")
    parser.add_argument("--lab", default="D:/Codex/work/vnpy-alpha-cache/alpha_lab")
    parser.add_argument("--rolling", nargs=2, type=int, default=[900, 250])
    parser.add_argument("--quantiles", type=int, default=3)
    parser.add_argument("--liquidity-window", type=int, default=60)
    parser.add_argument(
        "--signal-cache",
        default="D:/Codex/work/vnpy-alpha-cache/sweeps/market3000_rolling900_250.parquet",
        help="where to cache the rolling signal so a scan does not refit it",
    )
    parser.add_argument(
        "--force-signal",
        action="store_true",
        help="refit and rewrite the cached rolling signal",
    )
    parser.add_argument(
        "--neutralise",
        choices=["none", "demean", "zscore"],
        default="none",
        help="remove a common component from the signal before scoring",
    )
    parser.add_argument(
        "--seeds", type=int, nargs="+", default=[42],
        help="booster seeds to average into the signal",
    )
    parser.add_argument(
        "--hold-days", type=int, default=0,
        help="relabel the dataset to this horizon before fitting; 0 keeps the cached one",
    )
    parser.add_argument(
        "--split", choices=["valid", "test", "all"], default="test",
        help="which part of the out-of-sample window to score",
    )
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab, Segment

    scan = load_scan()
    gate = scan.load_gate()
    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print("dataset missing")
        return 2

    bounds = scan.period_bounds(dataset)
    cache_path = scan.signal_cache_path(
        args.signal_cache,
        args.dataset,
        args.rolling[0],
        args.rolling[1],
        args.hold_days,
        args.seeds,
    )
    cache_key = cache_path.stem
    use_cache = False
    if not args.force_signal and cache_path.exists():
        # The label lives in the file's custom metadata, not in its schema.
        # DataFrame.schema only carries the column types, so reading it here
        # returned no label at all and every run refitted.
        meta = pl.read_parquet_metadata(cache_path)
        expected_label = (
            f"ts_delay(close, -{args.hold_days}) / ts_delay(close, -1) - 1"
            if args.hold_days
            else str(getattr(dataset, "label_expression", ""))
        )
        if str(meta.get("label_expression", "")) == expected_label:
            use_cache = True
    if use_cache:
        signal = pl.read_parquet(cache_path)
        print(f"loaded cached rolling signal rows={signal.height}")
    else:
        if args.hold_days:
            gate.relabel(dataset, args.hold_days)
            print(f"relabelled to hold_days={args.hold_days}")
        signal = gate.predict_rolling(
            dataset, args.seeds, args.rolling[0], args.rolling[1]
        )
        target = cache_path.with_name(f"{cache_key}.parquet")
        target.parent.mkdir(parents=True, exist_ok=True)
        signal.write_parquet(
            target, metadata={"label_expression": str(dataset.label_expression)}
        )
        print(f"cached rolling signal to {target}")
    print(f"rolling signal rows={signal.height}")

    signal = signal.with_columns(
        pl.col("vt_symbol").str.split(".").list.first().alias("code")
    )
    signal = signal.with_columns(
        scan.as_date_text(pl.col("datetime")).alias("datetime")
    )
    label = dataset.result_df.select(["datetime", "vt_symbol", "label"])
    label = label.with_columns(scan.as_date_text(pl.col("datetime")).alias("datetime"))
    joined = signal.join(label, on=["datetime", "vt_symbol"], how="inner")

    valid_from, valid_to = bounds[Segment.VALID]
    test_from, test_to = bounds[Segment.TEST]
    if args.split == "valid":
        dates = sorted(
            d for d in joined["datetime"].unique().to_list()
            if valid_from <= d <= valid_to
        )
    elif args.split == "test":
        dates = sorted(
            d for d in joined["datetime"].unique().to_list()
            if test_from <= d <= test_to
        )
    else:
        dates = sorted(joined["datetime"].unique().to_list())
    if not dates:
        print(f"no out-of-sample sessions in split={args.split}")
        return 2
    print(f"scoring {len(dates)} sessions from {dates[0]} to {dates[-1]}")

    # The scan's liquidity buckets are reused, not rebuilt: they are the same
    # as-of trailing turnover, and the cache is already on disk. Only the cut
    # across that column is new.
    turnover = scan.liquidity_buckets(
        args.liquidity_window,
        args.quantiles,
        "D:/Codex/work/vnpy-alpha-cache/sweeps",
    )
    joined = joined.join(turnover, on=["code", "datetime"], how="inner")
    joined = joined.filter(pl.col("datetime").is_in(dates))
    print(f"joined to as-of turnover: rows={joined.height}")

    strata = size_buckets(joined, args.quantiles)
    joined = joined.join(
        strata.select(["code", "datetime", "size_bucket"]),
        on=["code", "datetime"],
        how="inner",
    )
    joined = scan.neutralise(joined, args.neutralise)
    if args.neutralise != "none":
        print(f"signal neutralised: mode={args.neutralise}")

    full = scan.stats(scan.daily_ic(joined))
    print(
        f"full market ic={full['ic_mean']:.4f} icir={full['icir']:.3f} "
        f"pos={full['pos']:.3f} sessions={full['sessions']}"
    )

    names = ["largest", "middle", "smallest"]
    for bucket in range(args.quantiles):
        part = joined.filter(pl.col("size_bucket") == bucket)
        if part.is_empty():
            continue
        part_stats = scan.stats(scan.daily_ic(part))

        # The control: the same names, the same sessions, a random signal.
        rng = random.Random(42)
        noise = pl.Series("signal", [rng.random() for _ in range(part.height)])
        control = part.with_columns(noise)
        control_stats = scan.stats(scan.daily_ic(control))

        label_name = names[bucket] if bucket < len(names) else f"stratum {bucket + 1}"
        print(
            f"stratum {bucket + 1}/{args.quantiles} ({label_name} by turnover) "
            f"rows={part.height:8d} names/session={part.height / max(len(dates), 1):6.1f} "
            f"ic={part_stats['ic_mean']:.4f} icir={part_stats['icir']:.3f} "
            f"pos={part_stats['pos']:.3f}"
        )
        print(
            f"  control random signal: ic={control_stats['ic_mean']:.4f} "
            f"icir={control_stats['icir']:.3f} pos={control_stats['pos']:.3f}"
        )

    # The same split by year, because a stratum that only works in one year
    # is a regime, not a signal.
    print("IC by year and stratum:")
    for bucket in range(args.quantiles):
        part = joined.filter(pl.col("size_bucket") == bucket)
        if part.is_empty():
            continue
        part_ic = scan.daily_ic_frame(part).with_columns(
            pl.col("datetime").str.slice(0, 4).alias("year")
        )
        label_name = names[bucket] if bucket < len(names) else f"stratum {bucket + 1}"
        for row in part_ic.group_by("year").agg(
            pl.col("ic").mean().alias("ic"),
            pl.col("ic").std().alias("ic_std"),
            pl.len().alias("sessions"),
        ).sort("year").iter_rows(named=True):
            ic_mean = float(row["ic"])
            ic_std = float(row["ic_std"])
            icir = ic_mean / ic_std if ic_std else 0.0
            print(
                f"  {label_name:8s} {row['year']}: ic={ic_mean:.4f} "
                f"icir={icir:.3f} sessions={row['sessions']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
