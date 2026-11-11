"""Trading calendar for autonomous trading sessions.

Calendar semantics follow the Chinese futures market: a trading day is a
weekday that is not a public holiday, and the night session belongs to the
trading day it starts on even when it closes after midnight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from collections.abc import Callable, Iterable

from vnpy.trader.logger import logger


# Default day sessions for Chinese futures (SHFE/INE/DCE/CZCE).
CN_FUTURES_DAY_SESSIONS: list[tuple[str, str, str]] = [
    ("day_morning", "09:00", "11:30"),
    ("day_afternoon", "13:30", "15:00"),
]

# Default night session (most products close at 23:00).
CN_FUTURES_NIGHT_SESSIONS: list[tuple[str, str, str]] = [
    ("night", "21:00", "23:00"),
]

# Night session for precious metals and crude oil products (close 02:30).
CN_FUTURES_MIDNIGHT_NIGHT_SESSIONS: list[tuple[str, str, str]] = [
    ("night", "21:00", "02:30"),
]

# Official 2026 exchange closure days for Chinese futures, including the
# night-session preparatory days announced alongside each holiday block.
CN_FUTURES_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 2, 15),
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 2, 19),
        date(2026, 2, 20),
        date(2026, 2, 23),
        date(2026, 4, 4),
        date(2026, 4, 5),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 5, 2),
        date(2026, 5, 3),
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 6, 19),
        date(2026, 6, 20),
        date(2026, 6, 21),
        date(2026, 9, 25),
        date(2026, 9, 26),
        date(2026, 9, 27),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 3),
        date(2026, 10, 4),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    }
)

# Around the clock without day boundaries (crypto-like markets).
FULL_DAY_SESSIONS: list[tuple[str, str, str]] = [
    ("all_day", "00:00", "23:59"),
]


@dataclass(frozen=True)
class Session:
    """One trading session inside a trading day.

    When ``end`` is earlier than ``start`` the session crosses midnight: it
    opens on the trading day and closes on the next calendar day.
    """

    name: str
    start: time
    end: time

    @property
    def overnight(self) -> bool:
        """Whether the session closes after midnight."""
        return self.end < self.start

    def contains(self, value: time) -> bool:
        """Return whether the clock time falls inside this session."""
        if self.overnight:
            return value >= self.start or value <= self.end
        return self.start <= value <= self.end

    @classmethod
    def from_strings(cls, name: str, start: str, end: str) -> Session:
        """Build a session from \"HH:MM\" strings."""
        start_time: time = time.fromisoformat(start)
        end_time: time = time.fromisoformat(end)
        return cls(name=name, start=start_time, end=end_time)


@dataclass(frozen=True)
class CalendarProfile:
    """Named collection of sessions with day exceptions."""

    name: str
    sessions: tuple[Session, ...]
    holidays: frozenset[date] = frozenset()
    extra_trading_days: frozenset[date] = frozenset()
    skip_weekend: bool = False


class TradingCalendar:
    """Trading day and session awareness driven by configurable sessions."""

    def __init__(
        self,
        sessions: list[Session],
        holidays: set[date] | None = None,
        extra_trading_days: set[date] | None = None,
        skip_weekend: bool = False,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a calendar from sessions and day exceptions."""
        self.sessions: tuple[Session, ...] = tuple(sessions)
        self.holidays: set[date] = set(holidays or set())
        self.extra_trading_days: set[date] = set(extra_trading_days or set())
        self.skip_weekend: bool = skip_weekend
        self._now_func: Callable[[], datetime] = now_func or datetime.now

    @classmethod
    def from_profile(cls, profile: CalendarProfile) -> TradingCalendar:
        """Build a calendar from a named profile."""
        return cls(
            sessions=list(profile.sessions),
            holidays=set(profile.holidays),
            extra_trading_days=set(profile.extra_trading_days),
            skip_weekend=profile.skip_weekend,
        )

    @classmethod
    def cn_futures(
        cls,
        holidays: set[date] | None = None,
        night_end: str = "23:00",
    ) -> TradingCalendar:
        """Chinese futures profile with a configurable night session end."""
        sessions: list[Session] = [
            Session.from_strings(name, start, end)
            for name, start, end in CN_FUTURES_DAY_SESSIONS
        ]
        night: Session = Session.from_strings("night", "21:00", night_end)
        sessions.append(night)
        return cls(sessions=sessions, holidays=holidays)

    @classmethod
    def cn_futures_midnight(
        cls,
        holidays: set[date] | None = None,
    ) -> TradingCalendar:
        """Chinese futures profile whose night session closes at 02:30."""
        sessions: list[Session] = [
            Session.from_strings(name, start, end)
            for name, start, end in (
                CN_FUTURES_DAY_SESSIONS + CN_FUTURES_MIDNIGHT_NIGHT_SESSIONS
            )
        ]
        return cls(sessions=sessions, holidays=holidays)

    @classmethod
    def full_day(cls) -> TradingCalendar:
        """Around-the-clock profile for markets without a day boundary."""
        sessions: list[Session] = [
            Session.from_strings(name, start, end)
            for name, start, end in FULL_DAY_SESSIONS
        ]
        return cls(sessions=sessions, skip_weekend=True)

    def now(self) -> datetime:
        """Current local time from the injected clock."""
        return self._now_func()

    def is_trading_day(self, day: date) -> bool:
        """Return whether the calendar date is a trading day."""
        if day in self.extra_trading_days:
            return True
        if day in self.holidays:
            return False
        if self.skip_weekend:
            return True
        return day.weekday() < 5

    def session_day(self, now: datetime | None = None) -> date | None:
        """Return the trading day that owns the session covering now."""
        current: datetime = now or self.now()
        day: date = current.date()
        clock: time = current.time()
        previous: date = day - timedelta(days=1)

        for session in self.sessions:
            if not session.overnight:
                if session.contains(clock) and self.is_trading_day(day):
                    return day
            else:
                if clock >= session.start and self.is_trading_day(day):
                    return day
                if clock <= session.end and self.is_trading_day(previous):
                    return previous
        return None

    def is_open(self, now: datetime | None = None) -> bool:
        """Return whether the market is open at the given moment."""
        return self.session_day(now) is not None

    def current_session(self, now: datetime | None = None) -> Session | None:
        """Return the session covering now, if any."""
        current: datetime = now or self.now()
        clock: time = current.time()
        day: date | None = self.session_day(current)
        if day is None:
            return None

        previous: date = day - timedelta(days=1)
        for session in self.sessions:
            if not session.overnight:
                if session.contains(clock) and self.is_trading_day(day):
                    return session
            else:
                if clock >= session.start and self.is_trading_day(day):
                    return session
                if clock <= session.end and self.is_trading_day(previous):
                    return session
        return None

    def next_open(self, now: datetime | None = None) -> datetime:
        """Return the next session opening strictly after now."""
        current: datetime = now or self.now()
        horizon: date = current.date() + timedelta(days=10)

        best: datetime | None = None
        day: date = current.date()
        while day <= horizon:
            if self.is_trading_day(day):
                for session in self.sessions:
                    start: datetime = datetime.combine(day, session.start)
                    if start > current and (best is None or start < best):
                        best = start
            day += timedelta(days=1)

        if best is None:
            # No session configured within the horizon: fall back to tomorrow.
            return datetime.combine(current.date() + timedelta(days=1), time(0, 0))
        return best

    def time_to_next_open(self, now: datetime | None = None) -> timedelta:
        """Time remaining until the next session opens."""
        current: datetime = now or self.now()
        return self.next_open(current) - current


def load_holidays(path: str | Path) -> set[date]:
    """Load a set of holiday dates from a JSON list of \"YYYY-MM-DD\"."""
    file_path: Path = Path(path)
    if not file_path.exists():
        logger.warning(
            "[autonomous] 节假日文件不存在：%s（已按空清单处理，"
            "请确认文件路径或按交易所休市安排维护）", file_path,
        )
        return set()

    with open(file_path, encoding="utf-8") as file:
        payload: object = json.load(file)

    if not isinstance(payload, list):
        raise ValueError(f"节假日文件格式错误，应为日期列表：{file_path}")

    holidays: set[date] = set()
    item: object
    for item in payload:
        if not isinstance(item, str):
            raise ValueError(f"节假日条目必须为字符串日期：{item!r}")
        holidays.add(date.fromisoformat(item))
    return holidays


def merge_holidays(*groups: Iterable[date] | None) -> set[date]:
    """Combine holiday sources without mutating the built-in frozenset."""
    merged: set[date] = set()
    group: Iterable[date] | None
    for group in groups:
        if group:
            merged.update(group)
    return merged


def build_profile(
    profile_name: str,
    sessions: list[dict[str, str]] | None = None,
    holidays: set[date] | None = None,
    extra_trading_days: set[date] | None = None,
    night_end: str = "23:00",
) -> CalendarProfile:
    """Build a calendar profile by name or from explicit session definitions."""
    resolved: list[Session]
    skip_weekend: bool = False
    profile_holidays: frozenset[date] = frozenset(holidays or set())
    if profile_name in ("cn_futures", "cn_futures_midnight"):
        profile_holidays = frozenset(
            merge_holidays(CN_FUTURES_HOLIDAYS_2026, holidays)
        )

    if sessions is not None:
        resolved = [
            Session.from_strings(item["name"], item["start"], item["end"])
            for item in sessions
        ]
    elif profile_name == "cn_futures_midnight":
        resolved = [
            Session.from_strings(name, start, end)
            for name, start, end in (
                CN_FUTURES_DAY_SESSIONS + CN_FUTURES_MIDNIGHT_NIGHT_SESSIONS
            )
        ]
    elif profile_name == "full_day":
        resolved = [
            Session.from_strings(name, start, end)
            for name, start, end in FULL_DAY_SESSIONS
        ]
        skip_weekend = True
    else:
        resolved = [
            Session.from_strings(name, start, end)
            for name, start, end in CN_FUTURES_DAY_SESSIONS
        ]
        resolved.append(Session.from_strings("night", "21:00", night_end))

    return CalendarProfile(
        name=profile_name,
        sessions=tuple(resolved),
        holidays=profile_holidays,
        extra_trading_days=frozenset(extra_trading_days or set()),
        skip_weekend=skip_weekend,
    )


PROFILE_NAMES: list[str] = ["cn_futures", "cn_futures_midnight", "full_day"]
