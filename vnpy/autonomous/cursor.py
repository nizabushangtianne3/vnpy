"""Local cursor for the derived tick feed.

The public history endpoint ignores the row-limit parameter and answers with
the whole trading session every time, and it rate limits a host that asks
too often. Both facts point the same way: fetch once per contract, keep the
bars on disk, and derive ticks only from what is new. This module holds that
cursor.

A cursor is deliberately dumb on purpose. It records nothing but the last
bar timestamp it has already emitted, so a restart cannot silently re-emit a
bar and double count a fill, and a crash cannot lose the bars already
fetched. Emitting a tick twice would book two fills for one bar, which is a
real money error, not a cosmetic one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CURSOR_FILENAME: str = "emfeed_cursor.json"


@dataclass
class FeedCursor:
    """Remembers the last bar timestamp already turned into a tick."""

    last_timestamp: str = ""
    bar_count: int = 0

    def seen(self, time_text: str) -> bool:
        """True when this bar has already been emitted.

        Compares on the minute prefix, because the same minute is printed as
        both "09:30" and "09:30:00" and a string compare would treat those as
        different bars and emit the minute twice.
        """
        if not self.last_timestamp:
            return False
        return time_text[:16] <= self.last_timestamp[:16]

    def advance(self, bar: object) -> None:
        """Record a bar as emitted, moving the cursor forward only."""
        time_text: str = getattr(bar, "time_text", "")
        if not time_text:
            return
        # "2026-11-12 21:01" and "2026-11-12 21:01:00" denote the same minute,
        # so compare on the minute prefix rather than the raw string.
        minute: str = time_text[:16]
        if minute > self.last_timestamp:
            self.last_timestamp = minute
        self.bar_count += 1


def cursor_path(directory: Path) -> Path:
    """Build the cursor file path inside a directory."""
    return directory / CURSOR_FILENAME


def load_cursor(directory: Path) -> FeedCursor:
    """Read a cursor from disk, or an empty one when absent or unreadable."""
    path: Path = cursor_path(directory)
    if not path.exists():
        return FeedCursor()
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return FeedCursor()
    if not isinstance(payload, dict):
        return FeedCursor()
    return FeedCursor(
        last_timestamp=str(payload.get("last_timestamp", "")),
        bar_count=int(payload.get("bar_count", 0) or 0),
    )


def save_cursor(directory: Path, cursor: FeedCursor) -> None:
    """Write the cursor atomically so a crash mid-write loses nothing."""
    directory.mkdir(parents=True, exist_ok=True)
    path: Path = cursor_path(directory)
    tmp: Path = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"last_timestamp": cursor.last_timestamp, "bar_count": cursor.bar_count},
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)
