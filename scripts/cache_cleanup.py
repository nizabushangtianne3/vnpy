"""Prune the research artifacts the alpha pipeline has already consumed.

The pilot, the campaign and every sweep write their numbers into
``.vntrader`` as JSON. Once a run has been read and its conclusion recorded,
those files stop earning their space: each one is a full ``stats`` dict plus
the raw engine record, and a sweep leaves a dozen of them behind. They are not
configuration and no step in the pipeline reads them back, so deleting them
changes nothing except the free space on the disk.

What survives is listed explicitly rather than inferred: ``vt_setting.json``
and ``wechat_setting.json`` are read at startup, ``autonomous.json`` is the
running configuration, and the fetch script is still the only local record of
how the bar cache was built. Everything else under ``.vntrader`` that matches
a research artifact pattern is removed.

Usage::

    python scripts/cache_cleanup.py            # report only
    python scripts/cache_cleanup.py --apply    # delete
"""

from __future__ import annotations

import argparse
from pathlib import Path


DEFAULT_ROOT: str = ".vntrader"

# Artifacts a run produces and nothing reads back.
ARTIFACT_SUFFIXES: tuple[str, ...] = (".json", ".log", ".txt")

# Files the runtime or the next fetch actually opens.
KEEP_NAMES: frozenset[str] = frozenset(
    {
        "vt_setting.json",
        "wechat_setting.json",
        "autonomous.json",
    }
)


def classify(path: Path) -> str:
    """Return ``keep`` or ``prune`` for one file under the research root."""
    if path.name in KEEP_NAMES:
        return "keep"
    if path.suffix in ARTIFACT_SUFFIXES:
        return "prune"
    return "keep"


def main() -> int:
    """Report the artifacts, and delete them when asked to."""
    parser = argparse.ArgumentParser(description="Prune consumed research artifacts")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="research artifacts directory")
    parser.add_argument("--apply", action="store_true", help="delete instead of reporting")
    args = parser.parse_args()

    root: Path = Path(args.root)
    if not root.is_dir():
        print(f"没有找到目录：{root}")
        return 2

    keep: list[Path] = []
    prune: list[Path] = []
    path: Path
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        (keep if classify(path) == "keep" else prune).append(path)

    for path in prune:
        print(f"{'删除' if args.apply else '可删除'}：{path.name}")
    for path in keep:
        print(f"保留：{path.name}")

    freed: int = sum(path.stat().st_size for path in prune)
    print(f"{len(prune)} 个文件，{freed / 1024:.1f} KiB")

    if not args.apply:
        print("加 --apply 执行删除")
        return 0

    for path in prune:
        path.unlink()
    print(f"已删除 {len(prune)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
