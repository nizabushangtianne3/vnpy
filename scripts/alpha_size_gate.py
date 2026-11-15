"""Does the size information set move the gate?

The findings file records the state of the search: fifteen directions moved
ICIR on the market-sized cross-section and none of them crossed 0.50, and
the one direction that was not a variation of the same 158 price-and-volume
factors was a different information set. It also records why that was a
data problem: the repository's kline request asked for seven columns and the
host had two more it was willing to answer.

This script measures that information set. It runs the same rolling fit on
the same sessions with the same seeds as the baseline, and the only thing
that changes is whether the model can see the size of every name.

Two controls keep the result honest. The first is the baseline itself,
loaded from the same cache the universe scan used, so the comparison is
between two numbers measured the same way rather than between a number and
an earlier reading. The second is a random-signal control on the same
sessions and the same names, which says what the ICIR of a signal with no
information looks like on this cross-section.

The reading is per year as well, because a factor that only works in one
year is a regime and not a signal. A size factor that clears the gate in
one year of three is not the answer either.
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


def load_gate_script():
    """Import alpha_gate without running its CLI."""
    spec = importlib.util.spec_from_file_location(
        "alpha_gate", str(REPO / "scripts" / "alpha_gate.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def score(
    scan,
    joined: pl.DataFrame,
    dates: list[str],
    label: str,
) -> dict:
    """Score a joined frame on the given sessions and print the reading."""
    part: pl.DataFrame = joined.filter(pl.col("datetime").is_in(dates))
    stats: dict = scan.stats(scan.daily_ic(part))

    # The control: the same names, the same sessions, a random signal.
    rng = random.Random(42)
    noise = pl.Series("signal", [rng.random() for _ in range(part.height)])
    control_stats: dict = scan.stats(scan.daily_ic(part.with_columns(noise)))

    print(
        f"{label}: ic={stats['ic_mean']:.4f} icir={stats['icir']:.3f} "
        f"pos={stats['pos']:.3f} sessions={stats['sessions']}"
    )
    print(
        f"  control random signal: ic={control_stats['ic_mean']:.4f} "
        f"icir={control_stats['icir']:.3f} pos={control_stats['pos']:.3f}"
    )
    return stats


def yearly(scan, joined: pl.DataFrame, dates: list[str], label: str) -> None:
    """Print the same reading split by year."""
    part: pl.DataFrame = joined.filter(pl.col("datetime").is_in(dates))
    frame: pl.DataFrame = scan.daily_ic_frame(part).with_columns(
        pl.col("datetime").str.slice(0, 4).alias("year")
    )
    print(f"IC by year for {label}:")
    for row in frame.group_by("year").agg(
        pl.col("ic").mean().alias("ic"),
        pl.col("ic").std().alias("ic_std"),
        pl.len().alias("sessions"),
    ).sort("year").iter_rows(named=True):
        ic_mean: float = float(row["ic"])
        ic_std: float = float(row["ic_std"])
        icir: float = ic_mean / ic_std if ic_std else 0.0
        print(
            f"  {row['year']}: ic={ic_mean:.4f} icir={icir:.3f} "
            f"sessions={row['sessions']}"
        )


def build_signal(
    gate,
    dataset,
    seeds: list[int],
    rolling: tuple[int, int],
    cache_path: Path,
    force: bool,
) -> pl.DataFrame:
    """Read or fit the rolling signal for one dataset."""
    use_cache: bool = False
    if not force and cache_path.exists():
        meta = pl.read_parquet_metadata(cache_path)
        stored = dict(meta) if hasattr(meta, "items") else {}
        expected = str(getattr(dataset, "label_expression", ""))
        if str(stored.get("label_expression", "")) == expected:
            use_cache = True
    if use_cache:
        print(f"loaded cached rolling signal {cache_path.name}")
        return pl.read_parquet(cache_path)

    signal: pl.DataFrame = gate.predict_rolling(dataset, seeds, rolling[0], rolling[1])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    signal.write_parquet(
        cache_path, metadata={"label_expression": str(dataset.label_expression)}
    )
    print(f"cached rolling signal to {cache_path}")
    return signal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-dataset", default="market3000")
    parser.add_argument("--size-dataset", default="market3000_size")
    parser.add_argument("--lab", default="D:/Codex/work/vnpy-alpha-cache/alpha_lab")
    parser.add_argument("--rolling", nargs=2, type=int, default=[900, 250])
    parser.add_argument("--seeds", type=int, nargs="+", default=[42])
    parser.add_argument(
        "--signal-dir",
        default="D:/Codex/work/vnpy-alpha-cache/sweeps",
        help="where the rolling signals are cached",
    )
    parser.add_argument("--force-signal", action="store_true")
    parser.add_argument(
        "--split", choices=["valid", "test", "all"], default="test",
    )
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab, Segment

    scan = load_scan()
    gate = load_gate_script()
    lab = AlphaLab(args.lab)

    baseline = lab.load_dataset(args.baseline_dataset)
    sized = lab.load_dataset(args.size_dataset)
    if baseline is None:
        print(f"baseline dataset {args.baseline_dataset} missing")
        return 2
    if sized is None:
        print(f"size dataset {args.size_dataset} missing; build it first")
        return 2

    bounds: dict = scan.period_bounds(baseline)
    signal_dir: Path = Path(args.signal_dir)
    rolling = (args.rolling[0], args.rolling[1])

    readings: dict[str, dict] = {}
    frames: dict[str, pl.DataFrame] = {}

    for name, dataset in ((args.baseline_dataset, baseline), (args.size_dataset, sized)):
        cache_path = signal_dir / f"{name}_rolling{rolling[0]}_{rolling[1]}_s42.parquet"
        signal = build_signal(gate, dataset, args.seeds, rolling, cache_path, args.force_signal)

        signal = signal.with_columns(
            pl.col("vt_symbol").str.split(".").list.first().alias("code")
        )
        signal = signal.with_columns(scan.as_date_text(pl.col("datetime")).alias("datetime"))
        label = dataset.result_df.select(["datetime", "vt_symbol", "label"])
        label = label.with_columns(scan.as_date_text(pl.col("datetime")).alias("datetime"))
        frames[name] = signal.join(label, on=["datetime", "vt_symbol"], how="inner")

    valid_from, valid_to = bounds[Segment.VALID]
    test_from, test_to = bounds[Segment.TEST]
    if args.split == "valid":
        dates = sorted(
            d for d in frames[args.baseline_dataset]["datetime"].unique().to_list()
            if valid_from <= d <= valid_to
        )
    elif args.split == "test":
        dates = sorted(
            d for d in frames[args.baseline_dataset]["datetime"].unique().to_list()
            if test_from <= d <= test_to
        )
    else:
        dates = sorted(frames[args.baseline_dataset]["datetime"].unique().to_list())
    if not dates:
        print(f"no out-of-sample sessions in split={args.split}")
        return 2
    print(f"scoring {len(dates)} sessions from {dates[0]} to {dates[-1]}")
    print()

    for name in (args.baseline_dataset, args.size_dataset):
        readings[name] = score(scan, frames[name], dates, name)
        yearly(scan, frames[name], dates, name)
        print()

    baseline_name: str = args.baseline_dataset
    size_name: str = args.size_dataset
    delta_ic: float = readings[size_name]["ic_mean"] - readings[baseline_name]["ic_mean"]
    delta_icir: float = readings[size_name]["icir"] - readings[baseline_name]["icir"]
    print(
        f"size factors change: ic {delta_ic:+.4f} icir {delta_icir:+.3f}"
    )
    print(f"gate: icir >= 0.5, size dataset reads {readings[size_name]['icir']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
