"""The owner's clock: Moscow time with the weekday, the weekday check, how late."""

import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from retinue import clock

NOW = datetime(2026, 10, 7, 11, 5, tzinfo=timezone.utc).timestamp()  # Wednesday, 14:05 in Moscow


def test_stamp_is_moscow_time_with_the_weekday():
    assert clock.stamp(NOW) == "2026-10-07 14:05 МСК, среда"
    assert clock.stamp(100.0) == "1970-01-01 03:01 МСК, четверг"
    assert clock.stamp(NOW, "Europe/Belgrade") == "2026-10-07 13:05 Europe/Belgrade, среда"
    assert clock.day(NOW + 2 * 86400 + 3 * 3600 + 55 * 60) == "пт 9 октября, 18:00 МСК"
    assert clock.until(NOW + 3600, NOW) == "15:05 МСК" and clock.until(NOW + 86400, NOW) == "чт 8 октября, 14:05 МСК"


def test_weekday_names():
    assert [clock.weekday(w) for w in ("пт", "Пятница", "пятницу", "fri", "Friday", "ПТ.")] == [4] * 6
    assert clock.weekday("среду") == 2 and clock.weekday("вс") == 6 and clock.weekday("завтра") is None


def test_parse_local_wall_time():
    moment = clock.parse("2026-10-09T18:00")
    assert (moment.isoformat(), moment.timestamp()) == ("2026-10-09T18:00:00+03:00", NOW + 2 * 86400 + 3 * 3600 + 55 * 60)
    assert clock.parse("2026-10-09 18:00:42").isoformat() == "2026-10-09T18:00:00+03:00"
    assert clock.parse("2026-10-09T15:00+00:00").isoformat() == "2026-10-09T18:00:00+03:00", "an offset is moved into МСК"
    for bad in ("2026-10-09", "в пятницу", "2026-13-09T18:00", ""):
        with pytest.raises(ValueError):
            clock.parse(bad)


def test_next_morning_and_lateness():
    assert clock.next_at("09:00", NOW).isoformat() == "2026-10-08T09:00:00+03:00", "14:05 is past 09:00: tomorrow"
    early = datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc).timestamp()  # 08:00 МСК
    assert clock.next_at("09:00", early).isoformat() == "2026-10-07T09:00:00+03:00"
    assert [clock.ago(s) for s in (30, 7 * 60, 125 * 60, 120 * 60, 3 * 86400 + 4 * 3600, 2 * 86400)] == [
        "1 мин", "7 мин", "2 ч 5 мин", "2 ч", "3 дн 4 ч", "2 дн"]


def test_zone_data_ships_with_the_image():
    deps = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.startswith("tzdata") for d in deps), "python:3.12-slim may have no /usr/share/zoneinfo"
