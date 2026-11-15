"""A controlled read on whether the size factors carry information.

The full size dataset needs a float share count for every name in the
market, and the host that publishes them refuses bursts, so the fetch takes
hours. Waiting for all of it before finding out whether the direction is
worth anything is the wrong order of operations.

This pilot answers the cheaper question first, on the names that have
already landed. It takes the names the fetch has covered, builds a dataset
over exactly those names twice - once with the 158 price factors alone and
once with the size factors as well - and fits the same rolling model on
both over the same sessions with the same seed.

The two datasets are the same size, the same names, the same sessions and
the same model, so a difference in the reading is a difference in the
information and nothing else. That is a narrower question than the gate
asks, and a narrower question is the right one to answer first: the point
is to find out whether the size factors are worth the rest of the fetch,
not to declare the gate cleared on a tenth of the market.

What this cannot do is clear the gate. A subset has a narrower cross
section, and a narrower cross section reads a different ICIR for reasons
that have nothing to do with the factors. The numbers are therefore
printed next to the baseline measured on the same subset, and the gate is
not judged here.
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


def load_size_module():
    """Import alpha_size_dataset without running its CLI."""
    spec = importlib.util.spec_from_file_location(
        "alpha_size_dataset", str(REPO / "scripts" / "alpha_size_dataset.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def covered_codes(cache_dir: str) -> list[str]:
    """The names whose float share history has already landed."""
    directory: Path = Path(cache_dir)
    if not directory.is_dir():
        return []
    return sorted(path.name.split("_")[0] for path in directory.glob("*_float_shares.csv"))


def subset_dataset(dataset, codes: list[str]):
    """A copy of the dataset restricted to the given names."""
    import copy

    wanted: set[str] = set(codes)
    new_dataset = copy.deepcopy(dataset)
    new_dataset.df = new_dataset.df.filter(
        pl.col("vt_symbol").str.split(".").list.first().is_in(wanted)
    )
    new_dataset.prepare_data(max_workers=1)
    return new_dataset


def size_dataset(dataset, size_mod, codes: list[str]):
    """A copy of the dataset with the size factors added."""
    import copy

    wanted: set[str] = set(codes)
    frame = dataset.df.filter(
        pl.col("vt_symbol").str.split(".").list.first().is_in(wanted)
    )
    sized = size_mod.attach_size(frame)

    columns: list[str] = size_mod.size_feature_columns()
    size_frame = sized.select(["datetime", "vt_symbol"] + columns)
    joined = dataset.df.join(size_frame, on=["datetime", "vt_symbol"], how="left")
    for name in columns:
        joined = joined.with_columns(
            pl.col(name).fill_null(pl.col(name).median().over("datetime"))
        )

    new_dataset = copy.deepcopy(dataset)
    new_dataset.df = joined
    new_dataset.prepare_data(max_workers=1)
    return new_dataset


def read_out(
    scan,
    gate,
    dataset,
    seeds: list[int],
    rolling: tuple[int, int],
) -> tuple[pl.DataFrame, list[str]]:
    """Fit the rolling model and return the scored frame with its sessions."""
    signal = gate.predict_rolling(dataset, seeds, rolling[0], rolling[1])
    signal = signal.with_columns(
        pl.col("vt_symbol").str.split(".").list.first().alias("code")
    )
    signal = signal.with_columns(scan.as_date_text(pl.col("datetime")).alias("datetime"))
    label = dataset.result_df.select(["datetime", "vt_symbol", "label"])
    label = label.with_columns(scan.as_date_text(pl.col("datetime")).alias("datetime"))
    joined = signal.join(label, on=["datetime", "vt_symbol"], how="inner")
    dates = sorted(joined["datetime"].unique().to_list())
    return joined, dates


def report(scan, joined: pl.DataFrame, dates: list[str], label: str) -> dict:
    """Print the reading and its control."""
    part = joined.filter(pl.col("datetime").is_in(dates))
    stats = scan.stats(scan.daily_ic(part))

    rng = random.Random(42)
    noise = pl.Series("signal", [rng.random() for _ in range(part.height)])
    control = scan.stats(scan.daily_ic(part.with_columns(noise)))

    print(
        f"{label}: ic={stats['ic_mean']:.4f} icir={stats['icir']:.3f} "
        f"pos={stats['pos']:.3f} sessions={stats['sessions']}"
    )
    print(
        f"  control random signal: ic={control['ic_mean']:.4f} "
        f"icir={control['icir']:.3f} pos={control['pos']:.3f}"
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="market3000")
    parser.add_argument("--lab", default="D:/Codex/work/vnpy-alpha-cache/alpha_lab")
    parser.add_argument(
        "--float-shares",
        default="D:/Codex/work/vnpy-alpha-cache/float_shares",
    )
    parser.add_argument("--rolling", nargs=2, type=int, default=[900, 250])
    parser.add_argument("--seeds", type=int, nargs="+", default=[42])
    parser.add_argument(
        "--min-names",
        type=int,
        default=100,
        help="do not run on fewer names than this",
    )
    args = parser.parse_args()

    from vnpy.alpha import AlphaLab

    scan = load_scan()
    gate = load_gate_script()
    size_mod = load_size_module()

    codes: list[str] = covered_codes(args.float_shares)
    print(f"float share coverage: {len(codes)} names")
    if len(codes) < args.min_names:
        print(f"not enough names yet, need {args.min_names}")
        return 2

    lab = AlphaLab(args.lab)
    dataset = lab.load_dataset(args.dataset)
    if dataset is None:
        print("dataset missing")
        return 2

    rolling = (args.rolling[0], args.rolling[1])

    # The machine has sixteen gigabytes and the market frame is three and a
    # a half million rows, so the two datasets are built and scored one
    # after the other rather than side by side. Holding both at once is what
    # runs the machine out of memory.
    print("building the price-only dataset on the covered names")
    plain = subset_dataset(dataset, codes)
    print(f"  rows={plain.result_df.height} names={plain.result_df['vt_symbol'].n_unique()}")
    print("fitting the price-only model")
    plain_frame, dates = read_out(scan, gate, plain, args.seeds, rolling)
    del plain

    print("building the size-aware dataset on the same names")
    sized = size_dataset(dataset, size_mod, codes)
    print(f"  rows={sized.result_df.height} names={sized.result_df['vt_symbol'].n_unique()}")
    print("fitting the size-aware model")
    size_frame, size_dates = read_out(scan, gate, sized, args.seeds, rolling)
    del sized

    common = sorted(set(dates) & set(size_dates))
    if not common:
        print("the two runs share no sessions")
        return 2
    print(f"comparing on {len(common)} shared sessions from {common[0]} to {common[-1]}")
    print()

    plain_stats = report(scan, plain_frame, common, "price factors only")
    size_stats = report(scan, size_frame, common, "price and size factors")
    print()
    print(
        f"size factors change: ic {size_stats['ic_mean'] - plain_stats['ic_mean']:+.4f} "
        f"icir {size_stats['icir'] - plain_stats['icir']:+.3f}"
    )
    print(
        "this is a subset reading and does not judge the gate, which is asked "
        "on the whole market"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
