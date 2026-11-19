"""Clear this round's task cache, keeping the files findings still cite.

The evidence directory already holds a copy of every log and json this round
produced, so the task cache only needs the two leg splits the findings note
references by path.
"""

import pathlib
import shutil

TASK_CACHE = pathlib.Path(r"D:\Codex\work\vnpy-task-cache")
KEEP = {"leg_fit_0.0.json", "leg_hold_0.0.json"}


def main() -> int:
    for path in sorted(TASK_CACHE.iterdir()):
        if path.name in KEEP:
            continue
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            print("removed:", path.name)
        except Exception as exc:
            # A locked file belongs to a live run; say so rather than forcing it.
            print("kept (in use):", path.name, type(exc).__name__)
    print("remaining:", sorted(p.name for p in TASK_CACHE.iterdir()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
