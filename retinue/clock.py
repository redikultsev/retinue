"""The owner's clock: every time the model or the owner sees is local time in the owner's zone, with the weekday.

Stored times stay UTC epoch seconds (the archive, the protocol, the scheduler's `due`). The model has no sense of
time, so whatever it is told about «now», «Friday» or «18:00» comes from here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Europe/Moscow"
LABELS = {"Europe/Moscow": "МСК"}
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")
# How a weekday may be named: short Russian, the start of the Russian word in any case, the start of the English.
_NAMES = (("пн", "понед", "mon"), ("вт", "вторн", "tue"), ("ср", "сред", "wed"), ("чт", "четв", "thu"),
          ("пт", "пятн", "fri"), ("сб", "субб", "sat"), ("вс", "воскр", "sun"))


def zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz)


def label(tz: str) -> str:
    return LABELS.get(tz, tz)


def local(ts: float, tz: str = DEFAULT_TZ) -> datetime:
    return datetime.fromtimestamp(ts, zone(tz))


def stamp(ts: float, tz: str = DEFAULT_TZ) -> str:
    """2026-10-07 14:05 МСК, среда"""
    moment = local(ts, tz)
    return f"{moment:%Y-%m-%d %H:%M} {label(tz)}, {WEEKDAYS[moment.weekday()]}"


def day(ts: float, tz: str = DEFAULT_TZ) -> str:
    """пт 9 октября, 18:00 МСК — how a reminder is named back to the owner"""
    moment = local(ts, tz)
    return f"{SHORT[moment.weekday()]} {moment.day} {MONTHS[moment.month - 1]}, {moment:%H:%M} {label(tz)}"


def until(ts: float, now: float, tz: str = DEFAULT_TZ) -> str:
    """18:00 МСК when it is today, otherwise the day as well."""
    if local(ts, tz).date() == local(now, tz).date():
        return f"{local(ts, tz):%H:%M} {label(tz)}"
    return day(ts, tz)


def weekday(name: str) -> int | None:
    """Monday is 0. None when the word is not a weekday."""
    word = name.strip().lower().rstrip(".")
    for index, (short, russian, english) in enumerate(_NAMES):
        if word == short or word.startswith(russian) or word.startswith(english):
            return index
    return None


def parse(when: str, tz: str = DEFAULT_TZ) -> datetime:
    """A local wall time as the model writes it: 2026-10-09T18:00 or 2026-10-09 18:00, in the owner's zone.
    A time with an offset is moved into the zone. Raises ValueError for anything else, a bare date included."""
    text = when.strip()
    if len(text) < 16 or text[10] not in "T ":
        raise ValueError(f"нужны дата и время вида 2026-10-09T18:00, а пришло «{when}»")
    moment = datetime.fromisoformat(text)
    moment = moment.astimezone(zone(tz)) if moment.tzinfo else moment.replace(tzinfo=zone(tz))
    return moment.replace(second=0, microsecond=0)


def next_at(hhmm: str, now: float, tz: str = DEFAULT_TZ) -> datetime:
    """The next moment strictly after `now` when the owner's clock shows HH:MM."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    today = local(now, tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if today.timestamp() > now else (today + timedelta(days=1))


def ago(seconds: float) -> str:
    """How late, in words: 7 мин, 2 ч 5 мин, 3 дн 4 ч."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч" if hours else f"{days} дн"
