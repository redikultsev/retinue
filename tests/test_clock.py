"""The owner's clock: the time in his zone (UTC until he names one) with the weekday, the weekday check, how late."""

import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from retinue import clock

NOW = datetime(2026, 10, 7, 14, 5, tzinfo=timezone.utc).timestamp()  # Wednesday, 14:05 UTC


def test_stamp_is_the_owners_time_with_the_weekday():
    assert clock.stamp(NOW) == "2026-10-07 14:05 UTC, Wednesday"
    assert clock.stamp(100.0) == "1970-01-01 00:01 UTC, Thursday"
    assert clock.stamp(NOW, "Europe/Berlin") == "2026-10-07 16:05 Europe/Berlin, Wednesday"
    assert clock.stamp(NOW, "Asia/Tokyo") == "2026-10-07 23:05 Asia/Tokyo, Wednesday", "any zone by its IANA name"
    assert clock.day(NOW + 2 * 86400 + 3 * 3600 + 55 * 60) == "Fri 9 October, 18:00 UTC"
    assert clock.until(NOW + 3600, NOW) == "15:05 UTC" and clock.until(NOW + 86400, NOW) == "Thu 8 October, 14:05 UTC"


def test_weekday_names():
    assert [clock.weekday(w) for w in ("пт", "Пятница", "пятницу", "fri", "Friday", "ПТ.")] == [4] * 6
    assert clock.weekday("среду") == 2 and clock.weekday("вс") == 6 and clock.weekday("завтра") is None
    assert [clock.weekday(w) for w in ("Mon", "Wednesday", "thu.", "SUNDAY")] == [0, 2, 3, 6], "English first"
    assert clock.weekday("tomorrow") is None


def test_a_zone_from_the_owners_words_in_either_language():
    for said in ("Tokyo", "I'm in Tokyo", "I am in tokyo", "im in Tokyo", "я в Tokyo", "Asia/Tokyo"):
        assert clock.find_zone(said) == "Asia/Tokyo", said
    assert clock.find_zone("I'm in New York") == "America/New_York"
    assert clock.find_zone("I'm in Atlantis") is None and clock.find_zone("I'm in") is None


def test_parse_local_wall_time():
    moment = clock.parse("2026-10-09T18:00")
    assert (moment.isoformat(), moment.timestamp()) == ("2026-10-09T18:00:00+00:00", NOW + 2 * 86400 + 3 * 3600 + 55 * 60)
    assert clock.parse("2026-10-09 18:00:42").isoformat() == "2026-10-09T18:00:00+00:00"
    assert clock.parse("2026-10-09T18:00+03:00").isoformat() == "2026-10-09T15:00:00+00:00", "an offset is moved into the zone"
    assert clock.parse("2026-10-09T18:00", "Asia/Tokyo").isoformat() == "2026-10-09T18:00:00+09:00"
    for bad in ("2026-10-09", "в пятницу", "2026-13-09T18:00", ""):
        with pytest.raises(ValueError):
            clock.parse(bad)


def test_next_morning_and_lateness():
    assert clock.next_at("09:00", NOW).isoformat() == "2026-10-08T09:00:00+00:00", "14:05 is past 09:00: tomorrow"
    early = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc).timestamp()  # 08:00 UTC
    assert clock.next_at("09:00", early).isoformat() == "2026-10-07T09:00:00+00:00"
    assert clock.next_at("09:00", early, "Asia/Tokyo").isoformat() == "2026-10-08T09:00:00+09:00", "17:00 in Tokyo"
    assert [clock.ago(s) for s in (30, 7 * 60, 125 * 60, 120 * 60, 3 * 86400 + 4 * 3600, 2 * 86400)] == [
        "1 min", "7 min", "2 h 5 min", "2 h", "3 d 4 h", "2 d"]


def test_zone_data_ships_with_the_image():
    deps = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.startswith("tzdata") for d in deps), "python:3.12-slim may have no /usr/share/zoneinfo"
