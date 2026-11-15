"""Inspect what is actually in the alpha lab's store.

The store holds datasets under ``alpha_lab/dataset``, daily frames under
``alpha_lab/daily`` and a contract table. Three questions recur while
working on the pipeline and each one used to be a throwaway script, which is
how the same reading logic ends up written four times and drifting apart:

* what does a cached dataset contain, and did a rebuild change it
  (``fingerprint``);
* how many daily frames are there, how many rows, and what date range do they
  cover (``scan-daily``);
* what are a dataset's segment periods and does it carry the label and the
  factors (``inventory``).

Only ``--dataset-dir`` and ``--daily-dir`` are configurable; everything else
is a question about the store this repository already writes.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
import pickle
from pathlib import Path

import polars as pl


DEFAULT_DATASET_DIR: str = "D:/Codex/work/vnpy-alpha-cache/alpha_lab/dataset"
DEFAULT_DAILY_DIR: str = "D:/Codex/work/vnpy-alpha-cache/alpha_lab/daily"


def fingerprint(frame: pl.DataFrame | None) -> str | None:
    """Stable fingerprint of a frame, independent of row order."""
    if frame is None:
        return None
    columns = frame.columns
    if "datetime" in columns and "vt_symbol" in columns:
        frame = frame.sort(["datetime", "vt_symbol"])
    digest = hashlib.md5()
    digest.update(str(columns).encode())
    digest.update(f"|rows={frame.height}".encode())
    buffer = io.BytesIO()
    frame.select([c for c in columns if c != "datetime"]).write_parquet(buffer)
    digest.update(buffer.getvalue())
    return digest.hexdigest()


def command_fingerprint(dataset_dir: Path, names: list[str]) -> int:
    """Fingerprint every frame of every named dataset."""
    for name in names:
        path = dataset_dir / name
        if not path.exists():
            print(f"== {name}: missing")
            continue
        dataset = pickle.loads(path.read_bytes())
        print(f"== {name}")
        for attribute in ["raw_df", "result_df", "learn_df", "infer_df"]:
            frame = getattr(dataset, attribute, None)
            shape = (frame.height, len(frame.columns)) if frame is not None else None
            print(
                f"   {attribute:10s} shape={shape} "
                f"fp={fingerprint(frame)}"
            )
        del dataset
        gc.collect()
    print("DONE")
    return 0


def command_inventory(dataset_dir: Path, names: list[str]) -> int:
    """Report each dataset's segment periods and whether it carries labels."""
    attributes = [
        "label_expression",
        "train_period",
        "valid_period",
        "test_period",
        "filters",
        "instruments",
    ]
    for name in names:
        path = dataset_dir / name
        if not path.exists():
            print(f"== {name}: missing")
            continue
        dataset = pickle.loads(path.read_bytes())
        print(f"== {name}")
        for attribute in attributes:
            value = str(getattr(dataset, attribute, "<absent>"))
            print(f"    {attribute} = {value[:120]}")
        result = dataset.result_df
        print("    result cols[0:6]:", result.columns[:6])
        has_label = "label" in result.columns
        factor_count = sum(
            1 for column in result.columns
            if column.startswith("f_") or column.isupper()
        )
        print(f"    has label: {has_label} | factor-looking cols: {factor_count}")
        learn = getattr(dataset, "learn_df", None)
        print("    learn rows", learn.shape if learn is not None else None)
        del dataset
        gc.collect()
    return 0


def command_scan_daily(daily_dir: Path) -> int:
    """Count the daily frames, and read only the date column of each."""
    files = sorted(daily_dir.glob("*.parquet"))
    print(f"parquet_files= {len(files)}")
    for path in files[:3]:
        frame = pl.read_parquet(path)
        print(path.name, frame.shape, frame.columns[:8])
    rows = 0
    earliest = None
    latest = None
    for path in files:
        frame = pl.read_parquet(path, columns=["datetime"])
        rows += frame.height
        low = frame["datetime"].min()
        high = frame["datetime"].max()
        if earliest is None or low < earliest:
            earliest = low
        if latest is None or high > latest:
            latest = high
        del frame
    print(f"total_rows= {rows}")
    print(f"date_range= {earliest} {latest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", default=DEFAULT_DATASET_DIR)
    parser.add_argument("--daily-dir", default=DEFAULT_DAILY_DIR)
    parser.add_argument(
        "--datasets",
        nargs="*",
        default=["market800.pkl", "market800oos.pkl", "m800clean.pkl", "m800norm.pkl"],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_fingerprint = subparsers.add_parser(
        "fingerprint", help="fingerprint each frame of each named dataset"
    )
    parser_fingerprint.add_argument("--datasets", nargs="*", default=None)

    parser_inventory = subparsers.add_parser(
        "inventory", help="report segment periods and whether labels are present"
    )
    parser_inventory.add_argument("--datasets", nargs="*", default=None)

    subparsers.add_parser("scan-daily", help="count the daily frames and their range")

    args = parser.parse_args()

    dataset_dir = Path(args.dataset_dir)
    daily_dir = Path(args.daily_dir)

    if args.command == "fingerprint":
        names = getattr(args, "datasets", None) or args.datasets
        return command_fingerprint(dataset_dir, names)
    if args.command == "inventory":
        names = getattr(args, "datasets", None) or args.datasets
        return command_inventory(dataset_dir, names)
    if args.command == "scan-daily":
        return command_scan_daily(daily_dir)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
