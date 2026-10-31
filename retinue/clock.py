"""The owner's clock: every time the model or the owner sees is local time in the owner's zone, with the weekday.

Stored times stay UTC epoch seconds (the archive, the protocol, the scheduler's `due`). The model has no sense of
time, so whatever it is told about «now», «Friday» or «18:00» comes from here.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_TZ = "UTC"  # the owner's own zone comes from the config (owner_tz); UTC until he names one
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
# How a weekday may be named: short Russian, the start of the Russian word in any case, the start of the English.
_NAMES = (("пн", "понед", "mon"), ("вт", "вторн", "tue"), ("ср", "сред", "wed"), ("чт", "четв", "thu"),
          ("пт", "пятн", "fri"), ("сб", "субб", "sat"), ("вс", "воскр", "sun"))

# «I'm in Tokyo» / «я в Tokyo»: the owner's words around a city, English and the Russian he may still use.
_SAYING = re.compile(r"^(?:i['’]?m|i am|я)\s+(?:in|at|в|во)\s+", re.IGNORECASE)


def zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz)

_NOT_PLACES = ("Etc/", "SystemV/", "posix/", "right/")


def valid_zone(tz: str) -> bool:
    """A zone the owner can live in: Area/City (or UTC) that zoneinfo knows. The legacy names without a slash —
    Japan, PRC, W-SU, EST — are refused: the same check wherever a zone is taken or read back."""
    if tz != "UTC" and ("/" not in tz or tz.startswith(_NOT_PLACES)):
        return False
    try:
        ZoneInfo(tz)
    except Exception:  # not a zone, or a path-like name zoneinfo refuses
        return False
    return True


def find_zone(name: str) -> str | None:
    """An IANA zone from what the model passes: «Asia/Tokyo», or a city as the zone names it («Tokyo», «new
    york», «Singapore» → Asia/Singapore). The owner's own words around a city are dropped: «I'm in Tokyo», «I am
    in Tokyo», «я в Tokyo». None when nothing matches — the model is told to give the zone itself.
    Whatever it returns passes `valid_zone`."""
    text = _SAYING.sub("", name.strip()).strip().replace(" ", "_")
    if not text:
        return None
    zones = available_timezones()
    if text in zones and valid_zone(text):
        return text
    matches = sorted(z for z in zones if valid_zone(z) and z.rsplit("/", 1)[-1].casefold() == text.casefold())
    return matches[0] if len(matches) == 1 else None


def label(tz: str) -> str:
    """A zone is named by its IANA name: unambiguous for the model and the owner, where abbreviations are not."""
    return tz


def local(ts: float, tz: str = DEFAULT_TZ) -> datetime:
    return datetime.fromtimestamp(ts, zone(tz))


def stamp(ts: float, tz: str = DEFAULT_TZ) -> str:
    """2026-10-07 14:05 UTC, Wednesday"""
    moment = local(ts, tz)
    return f"{moment:%Y-%m-%d %H:%M} {label(tz)}, {WEEKDAYS[moment.weekday()]}"


def day(ts: float, tz: str = DEFAULT_TZ) -> str:
    """Fri 9 October, 18:00 UTC — how a reminder is named back to the owner"""
    moment = local(ts, tz)
    return f"{SHORT[moment.weekday()]} {moment.day} {MONTHS[moment.month - 1]}, {moment:%H:%M} {label(tz)}"


def until(ts: float, now: float, tz: str = DEFAULT_TZ) -> str:
    """18:00 UTC when it is today, otherwise the day as well."""
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
        raise ValueError(f"need a date and time like 2026-10-09T18:00, got «{when}»")
    moment = datetime.fromisoformat(text)
    moment = moment.astimezone(zone(tz)) if moment.tzinfo else moment.replace(tzinfo=zone(tz))
    return moment.replace(second=0, microsecond=0)


def next_at(hhmm: str, now: float, tz: str = DEFAULT_TZ) -> datetime:
    """The next moment strictly after `now` when the owner's clock shows HH:MM."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    today = local(now, tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if today.timestamp() > now else (today + timedelta(days=1))


def ago(seconds: float) -> str:
    """How late, in words: 7 min, 2 h 5 min, 3 d 4 h."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"
