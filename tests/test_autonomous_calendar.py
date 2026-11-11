import json
from datetime import date, datetime, time

from vnpy.autonomous.calendar import (
    CN_FUTURES_HOLIDAYS_2026,
    Session,
    TradingCalendar,
    build_profile,
    load_holidays,
    merge_holidays,
)


HOLIDAYS: set[date] = {date(2026, 10, 1), date(2026, 10, 2)}


def test_trading_day_weekday_and_holiday_rules() -> None:
    calendar = TradingCalendar.cn_futures(holidays=HOLIDAYS)

    assert calendar.is_trading_day(date(2026, 9, 30)) is True
    assert calendar.is_trading_day(date(2026, 10, 1)) is False
    assert calendar.is_trading_day(date(2026, 10, 3)) is False   # Saturday
    assert calendar.is_trading_day(date(2026, 10, 4)) is False   # Sunday

    extra = TradingCalendar.cn_futures(holidays=HOLIDAYS)
    extra.extra_trading_days.add(date(2026, 10, 3))
    assert extra.is_trading_day(date(2026, 10, 3)) is True


def test_is_open_inside_day_session() -> None:
    calendar = TradingCalendar.cn_futures(holidays=HOLIDAYS)
    morning = datetime(2026, 9, 30, 9, 30)
    afternoon = datetime(2026, 9, 30, 14, 0)
    closed = datetime(2026, 9, 30, 12, 0)

    assert calendar.is_open(morning) is True
    assert calendar.current_session(morning) == Session("day_morning", time(9, 0), time(11, 30))
    assert calendar.is_open(afternoon) is True
    assert calendar.is_open(closed) is False


def test_night_session_belongs_to_start_day() -> None:
    calendar = TradingCalendar.cn_futures(holidays=HOLIDAYS)
    night = datetime(2026, 10, 8, 22, 0)

    assert calendar.is_open(night) is True
    assert calendar.session_day(night) == date(2026, 10, 8)


def test_overnight_session_after_midnight_uses_previous_day() -> None:
    calendar = TradingCalendar.cn_futures_midnight(holidays=HOLIDAYS)
    early_morning = datetime(2026, 10, 9, 0, 30)

    assert calendar.is_open(early_morning) is True
    assert calendar.session_day(early_morning) == date(2026, 10, 8)

    holiday_night = datetime(2026, 10, 2, 1, 0)
    assert calendar.is_open(holiday_night) is False


def test_next_open_skips_weekend_and_holidays() -> None:
    calendar = TradingCalendar.cn_futures(holidays=HOLIDAYS)

    friday_evening = datetime(2026, 10, 2, 18, 0)
    assert calendar.next_open(friday_evening) == datetime(2026, 10, 5, 9, 0)

    wednesday_noon = datetime(2026, 9, 30, 12, 0)
    assert calendar.next_open(wednesday_noon) == datetime(2026, 9, 30, 13, 30)

    during_session = datetime(2026, 9, 30, 10, 0)
    assert calendar.next_open(during_session) == datetime(2026, 9, 30, 13, 30)


def test_full_day_profile_is_always_open() -> None:
    calendar = TradingCalendar.full_day()
    assert calendar.is_open(datetime(2026, 10, 3, 3, 0)) is True


def test_explicit_sessions_profile() -> None:
    profile = build_profile(
        profile_name="custom",
        sessions=[{"name": "prime", "start": "08:00", "end": "12:00"}],
    )
    calendar = TradingCalendar.from_profile(profile)

    assert calendar.is_open(datetime(2026, 10, 9, 9, 0)) is True
    assert calendar.is_open(datetime(2026, 10, 9, 13, 0)) is False


def test_load_holidays(tmp_path) -> None:
    path = tmp_path / "holidays.json"
    path.write_text(json.dumps(["2026-10-01", "2026-10-02"]), encoding="utf-8")

    holidays = load_holidays(path)
    assert holidays == {date(2026, 10, 1), date(2026, 10, 2)}

    missing = load_holidays(tmp_path / "missing.json")
    assert missing == set()


def test_load_holidays_invalid_payload_raises(tmp_path) -> None:
    path = tmp_path / "holidays.json"
    path.write_text(json.dumps(["2026-10-01", "not-a-date"]), encoding="utf-8")

    try:
        load_holidays(path)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid holiday date must raise ValueError")


def test_merge_holidays_combines_sources() -> None:
    merged = merge_holidays(CN_FUTURES_HOLIDAYS_2026, {date(2027, 1, 1)})

    assert date(2026, 10, 1) in merged
    assert date(2027, 1, 1) in merged
    assert isinstance(CN_FUTURES_HOLIDAYS_2026, frozenset)


def test_build_profile_adds_builtin_2026_holidays() -> None:
    profile = build_profile("cn_futures")

    assert date(2026, 2, 17) in profile.holidays
    assert date(2026, 10, 1) in profile.holidays
    assert date(2027, 1, 1) not in profile.holidays

    midnight_profile = build_profile("cn_futures_midnight")
    assert date(2026, 5, 1) in midnight_profile.holidays
