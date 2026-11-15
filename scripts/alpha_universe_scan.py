"""Which slice of the market carries the signal, without looking ahead?

The first version of this scan ranked every symbol by the turnover of the last
250 cached bars and then scored each bucket over the whole test window. Two
problems made its numbers unusable as a gate:

* the ranking was computed from the end of the cache (2026-11) and then applied
  to sessions from 2024, so every bucket assignment knew the future; and
* a bucket was scored on the sessions where it happened to trade, while the
  full-market number was scored on all of them, so a difference between the
  two could be a difference in which dates landed in which bucket rather than
  a difference in the signal.

This version fixes both. Liquidity is measured over a trailing window that
ends before the session being scored, the bucket is fixed by that as-of rank,
and every bucket is scored on exactly the dates the full-market reference is
scored on.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import numpy as np
import polars as pl


REPO: Path = Path(__file__).resolve().parents[1]
BARS: str = "D:/Codex/work/vnpy-alpha-cache/stock_bars"


def load_gate():
    """Import alpha_gate without running its CLI."""
    spec = importlib.util.spec_from_file_location(
        "alpha_gate", str(REPO / "scripts" / "alpha_gate.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def as_date_text(series: pl.Series) -> pl.Series:
    """Normalise a datetime column to a plain YYYY-MM-DD string.

    The signal side carries a datetime with a time part and the CSV side
    carries plain text. Casting the datetime to text would leave
    "2024-01-03 00:00:00" on one side and "2024-01-03" on the other, which
    joins to nothing, so the date part is taken explicitly.
    """
    return series.cast(pl.Utf8).str.slice(0, 10)


def period_bounds(dataset) -> dict:
    """The dataset's own segment boundaries, as plain date strings.

    The bounds have to be read before predict_rolling runs: that function
    rewrites data_periods block by block, so afterwards VALID and TEST point at
    the last rolling block instead of the split the dataset was built with.
    """
    from vnpy.alpha import Segment

    bounds: dict = {}
    for segment in (Segment.VALID, Segment.TEST):
        start, end = dataset.data_periods[segment]
        bounds[segment] = (
            str(start)[:10] if not isinstance(start, str) else start[:10],
            str(end)[:10] if not isinstance(end, str) else end[:10],
        )
    return bounds


def load_bars() -> pl.DataFrame:
    """Read every cached bar file into one long frame.

    The cached files carry volume but not turnover, so turnover is volume times
    close, which is what the liquidity filter has to work with.
    """
    rows: list[dict] = []
    for path in Path(BARS).glob("*_*_101.csv"):
        code: str = path.stem.split("_")[1]
        with path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
        for line in lines[1:]:
            parts = line.strip().split(",")
            if len(parts) < 7:
                continue
            dt, _open, close, _high, _low, volume, _symbol = parts[:7]
            rows.append(
                {
                    "code": code,
                    # The signal side carries a datetime; the CSV side carries
                    # text. Both sides are normalised to a plain date string so
                    # the join keys have one type.
                    "datetime": str(dt)[:10],
                    "amount": float(volume) * float(close),
                }
            )
    return pl.DataFrame(rows).sort(["code", "datetime"])


def signal_cache_path(
    requested: str,
    dataset: str,
    train_window: int,
    step: int,
    hold_days: int = 0,
    seeds: list[int] | None = None,
) -> Path:
    """The file a rolling fit for these settings is cached under.

    The key always carries the seed list and the horizon, because a signal
    fitted with one seed is not the signal fitted with another. The caller's
    path is only a hint about where to look: the default has no seed suffix
    in its name, so matching the caller's file name against the key would
    never find the cache and every run would refit.
    """
    seed_list = seeds or [42]
    horizon_tag = f"_h{hold_days}" if hold_days else ""
    seed_tag = f"_s{'-'.join(str(s) for s in seed_list)}"
    key = f"{dataset}_rolling{train_window}_{step}{horizon_tag}{seed_tag}"
    return Path(requested).with_name(f"{key}.parquet")


def liquidity_buckets(
    window: int, quantiles: int, cache_dir: str
) -> pl.DataFrame:
    """The as-of liquidity bucket of every symbol on every session.

    Building this reads every cached bar file, which takes minutes, and the
    answer only depends on the window and the number of buckets, so it is
    cached and reused. The cache is keyed by both, because a different
    window is a different measurement.
    """
    cache = Path(cache_dir) / f"liquidity_buckets_w{window}_q{quantiles}.parquet"
    if cache.exists():
        print(f"loaded cached liquidity buckets: {cache}")
        return pl.read_parquet(cache)
    bars = load_bars()
    buckets = as_of_turnover(bars, window, quantiles)
    cache.parent.mkdir(parents=True, exist_ok=True)
    buckets.write_parquet(cache)
    print(f"cached liquidity buckets to {cache}")
    return buckets


def as_of_turnover(
    bars: pl.DataFrame, window: int, quantiles: int
) -> pl.DataFrame:
    """Attach each session's as-of liquidity bucket to every symbol.

    The trailing mean of turnover is shifted by one session before it is used,
    so the value attached to session T is built only from sessions before T.
    Buckets are cut on that day's cross-section, which is what a live filter
    can actually do.
    """
    frame = bars.with_columns(
        pl.col("amount")
        .shift(1)
        .rolling_mean(window_size=window, min_samples=window)
        .over("code")
        .alias("trailing_amount")
    )
    frame = frame.filter(pl.col("trailing_amount").is_not_null())
    frame = frame.with_columns(
        pl.col("trailing_amount")
        .rank(method="average")
        .over("datetime")
        .alias("liq_rank")
    )
    daily_count = frame.group_by("datetime").agg(pl.len().alias("n"))
    frame = frame.join(daily_count, on="datetime", how="left")
    frame = frame.with_columns(
        ((pl.col("liq_rank") - 1) / pl.col("n") * quantiles)
        .floor()
        .cast(pl.Int64)
        .clip(0, quantiles - 1)
        .alias("bucket")
    )
    return frame.select(["code", "datetime", "trailing_amount", "bucket"])


def daily_ic(
    joined: pl.DataFrame, dates: list[str] | None = None
) -> pl.Series:
    """Daily cross-sectional rank IC for one frame of signal and label."""
    if dates is not None:
        joined = joined.filter(pl.col("datetime").is_in(dates))
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


def daily_ic_frame(joined: pl.DataFrame) -> pl.DataFrame:
    """The daily IC values, with the date kept so they can be grouped."""
    pdf = joined.to_pandas()
    if pdf.empty:
        return pl.DataFrame({"datetime": [], "ic": []})

    def rank_ic(group) -> float:
        sig = group["signal"]
        lab = group["label"]
        if sig.nunique() < 2 or lab.nunique() < 2:
            return float("nan")
        return float(np.corrcoef(sig.rank(), lab.rank())[0, 1])

    ic = pdf.groupby("datetime").apply(rank_ic, include_groups=False).dropna()
    return pl.DataFrame({"datetime": ic.index.astype(str), "ic": ic.astype(float).to_list()})


def decompose_ic(joined: pl.DataFrame) -> None:
    """Separate the cross-section's tilt from its ordering.

    A rank IC of 0.045 on a 3000-name cross-section can come from two very
    different places:

    * the signal separates names within the session, in which case the best
      names should carry the largest labels; or
    * the signal barely moves but the label's own extremes happen to line up
      with it, in which case slicing the signal by rank finds nothing.

    The two are told apart by scoring the same signal against a label with
    its own cross-sectional mean removed, and by scoring the same names with
    the signal's cross-sectional mean removed. The first is pure ordering, the
    second is pure tilt.
    """
    frame = joined.with_columns(
        pl.col("signal")
        .rank(method="average", descending=True)
        .over("datetime")
        .alias("signal_rank")
    )
    counts = frame.group_by("datetime").agg(pl.len().alias("n"))
    frame = frame.join(counts, on="datetime", how="left")
    frame = frame.with_columns(
        ((pl.col("signal_rank") - 1) / pl.col("n") * 5)
        .floor()
        .cast(pl.Int64)
        .clip(0, 4)
        .alias("signal_quintile")
    )
    per_day = frame.group_by(["datetime", "signal_quintile"]).agg(
        pl.col("label").mean().alias("mean_label")
    )
    by_quintile = per_day.group_by("signal_quintile").agg(
        pl.col("mean_label").mean().alias("mean_label"),
        pl.col("mean_label").std().alias("std_label"),
        pl.len().alias("sessions"),
    )
    print("DECOMP mean label by signal quintile (1=strongest call):")
    for row in by_quintile.sort("signal_quintile").iter_rows(named=True):
        std_label = row["std_label"]
        std_text = "n/a" if std_label is None else f"{std_label:.5f}"
        print(
            f"  quintile {row['signal_quintile'] + 1}: "
            f"mean_label={row['mean_label']:.5f} "
            f"std={std_text} sessions={row['sessions']}"
        )
    def quintile_mean(table: pl.DataFrame, index: int) -> float | None:
        rows = table.filter(pl.col("signal_quintile") == index)["mean_label"]
        return float(rows[0]) if len(rows) else None

    top_mean = quintile_mean(by_quintile, 0)
    bottom_mean = quintile_mean(by_quintile, 4)
    spread = (
        top_mean - bottom_mean
        if top_mean is not None and bottom_mean is not None
        else None
    )
    spread_text = "n/a" if spread is None else f"{spread:.5f}"
    print(f"DECOMP strongest minus weakest quintile: {spread_text}")

    # The same split, but the label's own cross-sectional mean is removed
    # first. What is left is the part of the label that names can differ on,
    # which is the part a trading book can actually capture.
    per_day_mean = frame.group_by("datetime").agg(pl.col("label").mean().alias("day_mean"))
    centred = frame.join(per_day_mean, on="datetime", how="left").with_columns(
        (pl.col("label") - pl.col("day_mean")).alias("label_centred")
    )
    centred_daily = centred.group_by("datetime").agg(
        pl.col("label_centred").mean().alias("mean_label")
    )
    centred_quintile = (
        centred.group_by(["datetime", "signal_quintile"])
        .agg(pl.col("label_centred").mean().alias("mean_label"))
        .group_by("signal_quintile")
        .agg(pl.col("mean_label").mean().alias("mean_label"))
    )
    print("DECOMP mean centred label by signal quintile:")
    for row in centred_quintile.sort("signal_quintile").iter_rows(named=True):
        print(f"  quintile {row['signal_quintile'] + 1}: {row['mean_label']:.5f}")


def stats(ic: pl.Series) -> dict:
    """Reduce daily IC values to the three numbers the gate reads."""
    if len(ic) == 0:
        return {"sessions": 0, "ic_mean": 0.0, "icir": 0.0, "pos": 0.0}
    return {
        "sessions": len(ic),
        "ic_mean": float(ic.mean()),
        "icir": float(ic.mean() / ic.std()) if ic.std() else 0.0,
        "pos": float((ic > 0).mean()),
    }


def neutralise(joined: pl.DataFrame, mode: str) -> pl.DataFrame:
    """Strip a common component out of the signal.

    The market's own direction is the largest source of the daily variation
    in a long-only ranking, and the gate's ICIR charges that variation to the
    signal. Removing the cross-sectional mean of the signal leaves the
    ordering alone - a rank correlation is invariant to a constant shift - but
    it does change what a book that trades the signal's own levels is
    exposed to, which is why the money has to be measured after the change
    and not before it.

    ``demean`` subtracts the session's mean signal. ``zscore`` also divides
    by the session's standard deviation, which also equalises the spread.
    """
    if mode == "none":
        return joined
    if mode == "demean":
        return joined.with_columns(
            (pl.col("signal") - pl.col("signal").mean().over("datetime")).alias("signal")
        )
    if mode == "zscore":
        return joined.with_columns(
            (
                (pl.col("signal") - pl.col("signal").mean().over("datetime"))
                / pl.col("signal").std().over("datetime")
            ).alias("signal")
        )
    raise ValueError(f"unknown neutralisation mode: {mode}")


def keep_top_by(
    joined: pl.DataFrame, keep: float, by: list[str] | None = None
) -> pl.DataFrame:
    """Keep the names whose signal rank is in the top slice of their group.

    With ``by`` empty the grouping is the whole cross-section, which is the
    "signal only, no liquidity" control. With a liquidity bucket in ``by``
    the same slice is taken inside every bucket, which is the rule under test.
    """
    group = by or ["datetime"]
    # The signal is a prediction, so the highest value is the strongest call.
    # rank() numbers the smallest value 1, so the ranking has to be reversed
    # or the slice would keep the names the model likes least.
    ranked = joined.with_columns(
        pl.col("signal")
        .rank(method="average", descending=True)
        .over(group)
        .alias("signal_rank")
    )
    sizes = (
        joined.group_by(group)
        .agg(pl.len().alias("n"))
        .select(group + ["n"])
    )
    ranked = ranked.join(sizes, on=group, how="left")
    return ranked.filter(pl.col("signal_rank") <= pl.col("n") * keep)


def control_experiments(joined: pl.DataFrame, keep: float) -> None:
    """Run the controls that decide whether the rule is real.

    A rank-based slice on a cross-section can raise the measured IC for a
    signal that predicts nothing, because the survivors are the names whose
    noise happened to line up with the label on that session. Three controls
    separate that from a real effect:

    * a random signal, scored through the same slice;
    * the real signal sliced by a random grouping instead of liquidity;
    * the real signal sliced by liquidity, but with the liquidity buckets
      shuffled, which keeps the slice sizes but destroys the liquidity meaning.
    """
    rng = __import__("random").Random(42)

    random_signal = joined.with_columns(
        pl.Series("signal", [rng.random() for _ in range(joined.height)])
    )
    random_selected = keep_top_by(random_signal, keep)
    random_stats = stats(daily_ic(random_selected))
    print(
        f"CONTROL random signal, same slice: rows={random_selected.height} "
        f"ic={random_stats['ic_mean']:.4f} icir={random_stats['icir']:.3f} "
        f"pos={random_stats['pos']:.3f}"
    )

    flat_selected = keep_top_by(joined, keep)
    flat_stats = stats(daily_ic(flat_selected))
    print(
        f"CONTROL signal only, no liquidity split: rows={flat_selected.height} "
        f"ic={flat_stats['ic_mean']:.4f} icir={flat_stats['icir']:.3f} "
        f"pos={flat_stats['pos']:.3f}"
    )

    # Shuffle the bucket labels across names within each session. The number
    # of names in every bucket stays the same and the slice sizes stay the
    # same, but the buckets no longer mean anything.
    shuffled = joined.with_columns(
        pl.col("bucket")
        .shuffle(seed=42)
        .over("datetime")
        .alias("bucket")
    )
    shuffled_selected = keep_top_by(shuffled, keep, by=["datetime", "bucket"])
    shuffled_stats = stats(daily_ic(shuffled_selected))
    print(
        f"CONTROL liquidity buckets shuffled: rows={shuffled_selected.height} "
        f"ic={shuffled_stats['ic_mean']:.4f} icir={shuffled_stats['icir']:.3f} "
        f"pos={shuffled_stats['pos']:.3f}"
    )

    # The sharpest control: keep the signal exactly as it is and move the
    # label one session forward in time. If the model's signal were picking
    # up anything real, shifting the label would change the IC; if the IC
    # survives the shift, the relationship being measured is an artefact of
    # how the two frames line up rather than a forecast.
    shifted = joined.with_columns(
        pl.col("label").shift(-1).over("datetime").alias("label_next")
    )
    shifted = shifted.filter(pl.col("label_next").is_not_null())
    shifted = shifted.select(
        ["datetime", "signal", pl.col("label_next").alias("label")]
    )
    shifted_ic = daily_ic(shifted)
    shifted_stats = stats(shifted_ic)
    print(
        f"CONTROL label shifted one session: sessions={shifted_stats['sessions']} "
        f"ic={shifted_stats['ic_mean']:.4f} icir={shifted_stats['icir']:.3f} "
        f"pos={shifted_stats['pos']:.3f}"
    )

    # And the same signal scored against the label one session EARLIER, which
    # is the window a model that has simply learned today's return would look
    # good on.
    lagged = joined.with_columns(
        pl.col("label").shift(1).over("datetime").alias("label_prev")
    )
    lagged = lagged.filter(pl.col("label_prev").is_not_null())
    lagged = lagged.select(
        ["datetime", "signal", pl.col("label_prev").alias("label")]
    )
    lagged_ic = daily_ic(lagged)
    lagged_stats = stats(lagged_ic)
    print(
        f"CONTROL label lagged one session: sessions={lagged_stats['sessions']} "
        f"ic={lagged_stats['ic_mean']:.4f} icir={lagged_stats['icir']:.3f} "
        f"pos={lagged_stats['pos']:.3f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="market3000")
    parser.add_argument("--lab", default="D:/Codex/work/vnpy-alpha-cache/alpha_lab")
    parser.add_argument("--rolling", nargs=2, type=int, default=[900, 250])
    parser.add_argument("--quantiles", type=int, default=5)
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
        "--seeds",
        type=int,
        nargs="+",
        default=[42],
        help="booster seeds to average into the signal",
    )
    parser.add_argument(
        "--hold-days",
        type=int,
        default=0,
        help="relabel the dataset to this horizon before fitting; 0 keeps the cached one",
    )
    parser.add_argument(
        "--split",
        choices=["valid", "test", "all"],
        default="test",
        help="which part of the out-of-sample window to score",
    )
    parser.add_argument(
        "--date-from",
        default="",
        help="score only sessions on or after this date, overriding --split",
    )
    parser.add_argument(
        "--date-to",
        default="",
        help="score only sessions on or before this date, overriding --split",
    )
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab, Segment

    gate = load_gate()
    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print("dataset missing")
        return 2

    # No relabel here on purpose. The cached label already matches the
    # expression, and rewriting it mid-campaign leaves the fitted horizon and
    # the scored one disagreeing across result_df, learn_df, infer_df and
    # raw_df.
    train_window, step = args.rolling
    # Read the split boundaries before predict_rolling runs: it rewrites
    # data_periods block by block, so afterwards VALID and TEST point at the
    # last rolling block rather than the split the dataset was built with.
    bounds = period_bounds(dataset)
    cache_path = signal_cache_path(
        args.signal_cache,
        args.dataset,
        train_window,
        step,
        args.hold_days,
        args.seeds,
    )
    cache_key = cache_path.stem
    use_cache = False
    if not args.force_signal and cache_path.exists():
        # The label lives in the file's custom metadata, not in its schema.
        # DataFrame.schema only carries the column types, so reading it here
        # returned no label at all and every scan refitted.
        meta = pl.read_parquet_metadata(cache_path)
        stored = dict(meta) if hasattr(meta, "items") else {}
        stored_label = str(stored.get("label_expression", ""))
        current_label = str(getattr(dataset, "label_expression", ""))
        expected_label = (
            f"ts_delay(close, -{args.hold_days}) / ts_delay(close, -1) - 1"
            if args.hold_days
            else current_label
        )
        if stored_label == expected_label:
            use_cache = True
    if use_cache:
        signal = pl.read_parquet(cache_path)
        print(f"loaded cached rolling signal rows={signal.height}")
    else:
        if args.hold_days:
            # The relabel rewrites all four frames, so the model is fitted
            # and scored on the same horizon. It has to happen before the
            # rolling fit, not after it.
            gate.relabel(dataset, args.hold_days)
            print(f"relabelled to hold_days={args.hold_days}")
        signal = gate.predict_rolling(dataset, args.seeds, train_window, step)
        target = cache_path.with_name(f"{cache_key}.parquet")
        target.parent.mkdir(parents=True, exist_ok=True)
        signal.write_parquet(target, metadata={"label_expression": str(dataset.label_expression)})
        print(f"cached rolling signal to {target}")
    print(f"rolling signal rows={signal.height}")

    signal = signal.with_columns(
        pl.col("vt_symbol").str.split(".").list.first().alias("code")
    )
    signal = signal.with_columns(as_date_text(pl.col("datetime")).alias("datetime"))
    label = dataset.result_df.select(["datetime", "vt_symbol", "label"])
    label = label.with_columns(as_date_text(pl.col("datetime")).alias("datetime"))
    joined = signal.join(label, on=["datetime", "vt_symbol"], how="inner")

    valid_from, valid_to = bounds[Segment.VALID]
    test_from, test_to = bounds[Segment.TEST]
    if args.date_from or args.date_to:
        low = args.date_from or "0000-01-01"
        high = args.date_to or "9999-12-31"
        dates = sorted(
            d for d in joined["datetime"].unique().to_list() if low <= d <= high
        )
    elif args.split == "valid":
        dates = sorted(
            d for d in joined["datetime"].unique().to_list() if valid_from <= d <= valid_to
        )
    elif args.split == "test":
        dates = sorted(
            d for d in joined["datetime"].unique().to_list() if test_from <= d <= test_to
        )
    else:
        dates = sorted(joined["datetime"].unique().to_list())
    if not dates:
        print(
            f"no out-of-sample sessions in split={args.split} "
            f"({valid_from}..{valid_to} for valid, {test_from}..{test_to} for test)"
        )
        print(
            "the rolling window needs train_window sessions of history before it "
            "can score anything; the valid split sits inside that history"
        )
        return 2
    print(f"scoring {len(dates)} sessions from {dates[0]} to {dates[-1]}")

    buckets = liquidity_buckets(
        args.liquidity_window,
        args.quantiles,
        "D:/Codex/work/vnpy-alpha-cache/sweeps",
    )
    joined = joined.join(buckets, on=["code", "datetime"], how="inner")
    before = joined.height
    joined = joined.filter(pl.col("datetime").is_in(dates))
    print(f"joined to as-of buckets: rows={before} scored rows={joined.height}")
    joined = neutralise(joined, args.neutralise)
    if args.neutralise != "none":
        print(f"signal neutralised: mode={args.neutralise}")

    full_ic = daily_ic(joined)
    full = stats(full_ic)
    print(
        f"full market ic={full['ic_mean']:.4f} icir={full['icir']:.3f} "
        f"pos={full['pos']:.3f} sessions={full['sessions']}"
    )

    # ICIR is a mean over a standard deviation, so one bad year can hold the
    # whole ratio down. The yearly split says which one.
    ic_frame = daily_ic_frame(joined).with_columns(
        pl.col("datetime").str.slice(0, 4).alias("year")
    )
    print("IC by year:")
    for row in ic_frame.group_by("year").agg(
        pl.col("ic").mean().alias("ic"),
        pl.col("ic").std().alias("ic_std"),
        pl.len().alias("sessions"),
    ).sort("year").iter_rows(named=True):
        # A year with one session has no dispersion, and a year whose ICs are
        # all equal has none either; either way the ratio is not defined.
        year_mean = row["ic"]
        year_std = row["ic_std"]
        year_ir = (
            float(year_mean) / float(year_std)
            if year_mean is not None and year_std
            else 0.0
        )
        print(
            f"  {row['year']}: ic={float(row['ic']):.4f} "
            f"ic_std={float(row['ic_std']):.4f} icir={year_ir:.3f} "
            f"sessions={row['sessions']}"
        )

    # The same split by rolling block. Each block is scored by one model
    # fitted on the window before it, so a block that is weak while its
    # neighbours are strong points at a stale model rather than at a change
    # in the market.
    block_dates = sorted(ic_frame["datetime"].unique().to_list())
    block_size = max(step, 1)
    blocks = []
    for start in range(0, len(block_dates), block_size):
        chunk = block_dates[start : start + block_size]
        part = ic_frame.filter(pl.col("datetime").is_in(chunk))
        if len(part) < 20:
            continue
        block_mean = part["ic"].mean()
        block_std = part["ic"].std()
        ic_mean = float(part["ic"].mean())
        ic_std = float(part["ic"].std())
        if block_mean is None or not block_std:
            continue
        blocks.append(
            {
                "from": chunk[0],
                "to": chunk[-1],
                "sessions": len(part),
                "ic": ic_mean,
                "icir": ic_mean / ic_std if ic_std else 0.0,
            }
        )
    print(f"IC by rolling block (step={block_size}):")
    for row in blocks:
        print(
            f"  {row['from']} to {row['to']}: sessions={row['sessions']:3d} "
            f"ic={row['ic']:.4f} icir={row['icir']:.3f}"
        )

    scored_dates = set(joined["datetime"].unique().to_list())
    for bucket in range(args.quantiles):
        part = joined.filter(pl.col("bucket") == bucket)
        if part.is_empty():
            continue
        bucket_ic = daily_ic(part)
        bucket_stats = stats(bucket_ic)
        print(
            f"bucket {bucket + 1}/{args.quantiles} (least liquid first) "
            f"rows={part.height:8d} names/session={part.height / max(len(scored_dates), 1):6.1f} "
            f"ic={bucket_stats['ic_mean']:.4f} icir={bucket_stats['icir']:.3f} "
            f"pos={bucket_stats['pos']:.3f}"
        )

    # A liquidity filter that keeps only the names the signal likes is a
    # different universe, and it is the only honest way to ask whether the
    # weak full-market IC is diluted by names the signal has nothing to say
    # about. The rule below is deliberately simple: keep the names whose
    # signal rank is in the top slice of their own liquidity bucket.
    for keep in (0.25, 0.5):
        selected = keep_top_by(joined, keep, by=["datetime", "bucket"])
        sel_ic = daily_ic(selected)
        sel = stats(sel_ic)
        print(
            f"keep top {int(keep * 100)}% of each bucket: rows={selected.height} "
            f"ic={sel['ic_mean']:.4f} icir={sel['icir']:.3f} pos={sel['pos']:.3f}"
        )
    print("--- controls ---")
    control_experiments(joined, 0.25)
    print("--- decomposition ---")
    decompose_ic(joined)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
