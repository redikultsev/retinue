"""The life hub (`lifehub`): pages about the owner's life on his own server, behind his VPN, for his devices only.
In Retinue «хаб» already names the knowledge base's git hub, so in code the site is the lifehub.

The router writes data here and nothing else: JSON checked by a schema, pictures drawn or re-encoded by code. The
pages are built from it by Hugo in a container of its own with no network (deploy/lifehub); the model never writes
HTML, and nothing it sends is markup — Hugo escapes every value. A page is a view of the archive, the base and the
router's tables, not a third store: its numbers are counted by code.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import tempfile
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from .render import linkable
from .travel import Refused as TravelRefused, Unavailable as TravelUnavailable

log = logging.getLogger("retinue.lifehub")

ID = re.compile(r"[0-9a-f]{16}")  # a page's address: random, so that a link in a chat says nothing


class Refused(ValueError):
    """What the assistant sent does not fit; the text names the field, in words she acts on."""


# --- the schema: the same dict is the tool's input schema for her and the check here --------------------------------

TYPES = {"object": (dict, "нужен объект"), "array": (list, "нужен список"), "string": (str, "нужна строка"),
         "integer": (int, "нужно целое число"), "number": ((int, float), "нужно число"),
         "boolean": (bool, "true или false")}


def check(schema: dict, value, where: str = ""):
    """`value` if it fits `schema` — a subset of JSON Schema: type, properties, required, additionalProperties,
    items, minItems, maxItems, enum, minLength, maxLength, pattern, format date, minimum, maximum. Raises Refused
    naming the first field that does not fit."""
    name = where or "аргументы"
    kind = schema.get("type")
    if kind:
        types, words = TYPES[kind]
        if not isinstance(value, types) or (kind != "boolean" and isinstance(value, bool)):
            raise Refused(f"{name}: {words}")
    if "enum" in schema and value not in schema["enum"]:
        raise Refused(f"{name}: одно из: {', '.join(map(str, schema['enum']))}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise Refused(f"{name}: пустая строка" if schema["minLength"] == 1 else
                          f"{name}: не короче {schema['minLength']} знаков")
        if len(value) > schema.get("maxLength", len(value)):
            raise Refused(f"{name}: не длиннее {schema['maxLength']} знаков")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise Refused(f"{name}: {schema.get('description') or 'не по форме'}")
        if schema.get("format") == "date":
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError(value)
                date.fromisoformat(value)
            except ValueError:
                raise Refused(f"{name}: дата вида 2026-11-14") from None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = schema.get("minimum"), schema.get("maximum")
        if (low is not None and value < low) or (high is not None and value > high):
            raise Refused(f"{name}: от {low} до {high}")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise Refused(f"{name}: от {schema.get('minItems', 0)} до {schema.get('maxItems')} элементов")
        for i, item in enumerate(value):
            check(schema.get("items", {}), item, f"{where}[{i}]")
    if isinstance(value, dict):
        fields = schema.get("properties", {})
        if schema.get("additionalProperties") is False and (unknown := [k for k in value if k not in fields]):
            raise Refused(f"{name}: неизвестное поле {str(unknown[0])[:40]}. Поля: {', '.join(fields)}.")
        for field in schema.get("required", []):
            if field not in value:
                raise Refused(f"{name}: нет поля {field}")
        for field, inner in fields.items():
            if field in value:
                check(inner, value[field], f"{where}.{field}" if where else field)
    return value


def text(cap: int, about: str, least: int = 0) -> dict:
    return {"type": "string", "maxLength": cap, "description": about, **({"minLength": least} if least else {})}


OPTION_KINDS = ("flight", "stay", "ground")
# What she sends: her words and references to travel-ops' answers. Prices, sellers, times, ratings and photos the
# router takes from travel-ops itself by `search_id` and `link` — what is on the page was seen by code, not retold.
TRIP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["title", "summary", "start", "end", "options"],
    "properties": {
        "trip_id": {"type": "string", "pattern": "^[0-9a-f]{16}$",
                    "description": "id страницы из ответа publish_trip — только чтобы обновить ту же поездку"},
        "title": text(80, "заголовок страницы: откуда, куда, даты — «Вена — Котор, 22–24 октября»", 1),
        "summary": text(1500, "твой вывод для Владельца: что советуешь и почему, чего нет в цене, что не так с "
                              "датами — обычный текст, без разметки и ссылок"),
        "start": {"type": "string", "format": "date", "description": "первый день поездки, 2026-10-22"},
        "end": {"type": "string", "format": "date", "description": "последний день поездки, 2026-10-24"},
        "options": {"type": "array", "minItems": 1, "maxItems": 12, "description": "варианты, которые ты нашла",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["kind", "search_id", "link", "note"],
                        "properties": {
                            "kind": {"type": "string", "enum": list(OPTION_KINDS),
                                     "description": "flight — перелёт, stay — жильё, ground — поезд или автобус"},
                            "search_id": {"type": "string", "pattern": "^[a-z0-9]{6,16}$",
                                          "description": "search_id из ответа travel-ops, где этот вариант"},
                            "link": text(1000, "ссылка варианта из ответа travel-ops целиком (link.url)"),
                            "note": text(600, "твои слова о варианте: почему он, что в отзывах (как чужое мнение)"),
                            "pick": {"type": "boolean", "description": "true — этот вариант ты советуешь"},
                        }}},
    },
}


# --- the folder the pages are built from --------------------------------------------------------------------------

class Data:
    """The data folder: the router writes it, the build container reads it at any moment. A file is replaced
    whole (a temporary file and `rename`), so a build sees the old file or the new one, never half of one."""

    def __init__(self, folder: str) -> None:
        self.root = Path(folder)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o755)

    def path(self, name: str) -> Path:
        parts = PurePosixPath(name).parts
        if not name or name.startswith("/") or any(p in ("..", ".") or p.startswith(".") for p in parts):
            raise ValueError(f"not a data file name: {name!r}")
        return self.root.joinpath(*parts)

    def write_bytes(self, name: str, data: bytes) -> None:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        for folder in (path.parent, *path.parent.parents):
            if folder == self.root or self.root not in folder.parents:
                break
            os.chmod(folder, 0o755)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        except BaseException:
            os.unlink(temporary)
            raise

    def write(self, name: str, value) -> None:
        self.write_bytes(name, json.dumps(value, ensure_ascii=False, indent=1).encode())

    def read(self, name: str):
        try:
            return json.loads(self.path(name).read_text())
        except (FileNotFoundError, ValueError):
            return None

    def names(self, folder: str) -> list[str]:
        where = self.path(folder)
        return sorted(f"{folder}/{p.name}" for p in where.iterdir() if not p.name.startswith(".")) \
            if where.is_dir() else []

    def has(self, folder: str, page_id: str) -> bool:
        return bool(ID.fullmatch(page_id)) and self.path(f"{folder}/{page_id}.json").is_file()

    def new_id(self, folder: str) -> str:
        while self.has(folder, page_id := secrets.token_hex(8)):
            pass
        return page_id


# --- what the router writes: the status page ----------------------------------------------------------------------

DAY = 86400
WEEK = 7 * DAY
# How a model run is named on the status page. A kind not here is shown as it is.
RUN_KINDS = {"conversation": "разговор", "retry": "ответ после лимита", "summary": "утренняя сводка",
             "compact": "сжатие", "reminder": "напоминание", "price": "слежение за ценой", "triage": "разбор письма",
             "mail": "суждение о почте"}
TOKENS = ("вход", "выход", "кеш: чтение", "кеш: запись")
RUN_STATES = {"done": "удачно", "error": "сбой", "failed": "сбой", "rejected": "отказ", "limit": "упёрлись в лимит"}
WINDOW_NAMES = {"five_hour": "5 часов", "seven_day": "неделя"}
CHARTS = ["charts/tokens.svg", "charts/runs.svg"]
REFRESH_S = 300           # the pages' data is written this often
AFTER_RUN_S = 60          # and this long after a model run ended: what she just did shows on the status page
DEADLINE_PAST_DAYS = 30   # a deadline that passed this long ago is history, not a deadline
DEADLINES = 20            # deadlines on the «now» page at most
NEEDS = {"read": "прочитать", "reply": "ответить", "decide": "решить", "attend": "прийти", "pay": "оплатить",
         "act": "сделать"}
MAIL_DAYS = 7             # mail that needs the owner, judged this many days back


def grouped(number: int) -> str:
    """7785378 -> «7 785 378»: a page shows numbers as text, so that no template formats them."""
    return f"{number:,}".replace(",", " ")


class Lifehub:
    """The router's side of the life hub: what it writes to the data folder and when. Attached to the core like the
    mailroom; reads the router's own tables, the archive and the base, and never calls a model."""

    def __init__(self, data: Data, url: str, link_hosts: list[str], kb: str = "", build_status: str = "") -> None:
        self.data = data
        self.build_status = build_status      # build.json the builder writes after every build; "" — not known
        self.url = url.rstrip("/")            # the site's address as the owner's devices open it
        self.link_hosts = list(link_hosts)    # travel-ops' sites: a link there is clickable on a page too
        self.kb = kb                          # the knowledge base's files: deadlines are read there; "" — none
        self.core = None
        self.written = 0.0                    # when the data was last written
        self.poked = 0.0                      # when a model run last ended, until the data is written
        self.task: asyncio.Task | None = None
        self.pending: list[str] = []          # trip pages whose photos are still to fetch
        self.fetching: asyncio.Task | None = None

    def attach(self, core) -> None:
        self.core = core

    # --- when ------------------------------------------------------------------------------------------------------

    def poke(self, now: float) -> None:
        """A model run ended: the pages are written again a minute later (runs end in bursts)."""
        self.poked = self.poked or now

    def step(self, now: float) -> None:
        """Called by the router's clock: one refresh at a time, every REFRESH_S and AFTER_RUN_S after a run; the
        photos of a page just published, one page at a time."""
        due = now - self.written >= REFRESH_S or (self.poked and now - self.poked >= AFTER_RUN_S)
        if due and (self.task is None or self.task.done()):
            self.written, self.poked = now, 0.0
            self.task = asyncio.create_task(self.refresh(now))
        if self.pending and (self.fetching is None or self.fetching.done()):  # photos: one page at a time
            self.fetching = asyncio.create_task(self.photos(self.pending.pop(0), self.core.travel, now))

    async def refresh(self, now: float) -> None:
        """Every page's data, each part on its own: a part that fails is logged, the others are written."""
        for name, part in (("site", self.write_site), ("status", self.write_status), ("now", self.write_now)):
            try:
                result = part(now)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("lifehub: %s not written", name)

    def write_site(self, now: float) -> None:
        """What every page needs: the address, and the hosts a link may lead to (the template checks them again)."""
        hosts = list(dict.fromkeys(entry.partition("/")[0].lower() for entry in self.link_hosts))
        self.data.write("site.json", {"url": self.url, "link_hosts": hosts})

    # --- the «now» page ------------------------------------------------------------------------------------------

    async def now(self, now: float) -> dict:
        """The «now» page (decision 4): upcoming trips, today's and tomorrow's meetings, his reminders, the mail that
        waits for him, document deadlines from the base, the health line."""
        from . import clock

        core = self.core
        today = clock.local(now, core.tz).date()
        trips = []
        for name in self.data.names("trips"):
            trip = self.data.read(name)
            if isinstance(trip, dict) and str(trip.get("end", "")) >= today.isoformat():
                trips.append({"title": trip.get("title", ""), "start": trip.get("start", ""),
                              "end": trip.get("end", ""), "url": f"/trips/{trip.get('id')}/"})
        page = {"updated": self.stamp(now), "trips": sorted(trips, key=lambda t: t["start"]),
                "today": [], "tomorrow": [], "reminders": [core.jobs.line(job, numbered=False)
                                                          for job in core.jobs.reminders(10)],
                "mail": self.waiting(now), "deadlines": self.deadlines(today),
                "health": core.health(now).removeprefix("Здоровье за сутки: ").rstrip(".")}
        if core.mail:
            for key, day in (("today", now), ("tomorrow", now + DAY)):
                try:
                    page[key] = [line.removeprefix("- ") for line in await core.mail.agenda(day)]
                except Exception:
                    log.exception("lifehub: the agenda was not read")
                    page[key] = ["календарь не прочитан: сборщик недоступен"]
        return page

    async def write_now(self, now: float) -> None:
        self.data.write("now.json", await self.now(now))

    def waiting(self, now: float) -> list[dict]:
        """Letters she judged that ask something of the owner, the longest waiting first. About the job search:
        that it is one, nothing more — the same secret as in a notification."""
        from .mail import JOB

        if not self.core.mail:
            return []
        out = []
        for item in self.core.mail.store.needing(now - MAIL_DAYS * DAY):
            card = item["card"] or {}
            if item["kind"] == JOB or card.get("job"):
                out.append({"text": "Поиск работы — подробности в Telegram.", "from": "",
                            "needs": NEEDS.get(card.get("needs_now"), ""), "deadline": ""})
            else:
                out.append({"text": card.get("text") or card.get("summary") or "", "from": item["sender"] or "",
                            "needs": NEEDS.get(card.get("needs_now"), ""), "deadline": card.get("deadline_now") or ""})
        return out

    def deadlines(self, today: date) -> list[dict]:
        """Dates from the base's Records about the world and about the owner: `until` (a document, a status) and
        `stale_after` (a fact to check again). Superseded knowledge and long past dates are left out."""
        if not self.kb or not os.path.isdir(self.kb):
            return []
        horizon, found = today - timedelta(days=DEADLINE_PAST_DAYS), []
        root = Path(self.kb)
        for path in sorted(root.glob("*/*/**/*.md")):
            relative = path.relative_to(root)
            if relative.parts[1] not in ("profile", "knowledge") or any(p.startswith(".") for p in relative.parts):
                continue
            head = front_matter(path)
            if not head or head.get("status") == "superseded":
                continue
            for field, what in (("until", "действует до"), ("stale_after", "перепроверить")):
                value = head.get(field)
                when = value if isinstance(value, date) else _day(value)
                if when and when >= horizon:
                    found.append({"title": str(head.get("title") or relative.stem)[:120], "date": when.isoformat(),
                                  "what": what, "path": relative.as_posix()})
        return sorted(found, key=lambda d: d["date"])[:DEADLINES]

    def stamp(self, ts: float) -> str:
        from . import clock

        return clock.stamp(ts, self.core.tz)

    def status(self, now: float) -> dict:
        """The status page (decision 5): counted by code from the router's tables."""
        from .archive import ASSISTANT, OWNER, SYSTEM
        from .mail import KINDS

        core = self.core
        runs = core.store.runs_since(now - DAY)
        tally = core.mail.store.tally(now - DAY) if core.mail else {"kept": {}, "dropped": {}, "blocked": {}}
        windows = core.store.windows()
        status = {
            "updated": self.stamp(now),
            "day": {"owner": core.archive.count(OWNER, now - DAY), "answers": core.archive.count(ASSISTANT, now - DAY),
                    "notices": core.archive.count(SYSTEM, now - DAY),
                    "runs": [[RUN_STATES.get(state, state), n] for state, n in sorted(runs.items(), key=lambda x: -x[1])],
                    "reminders": core.jobs.fired_since(now - DAY),
                    "letters": {"kept": sum(tally["kept"].values()),
                                "dropped": sum(tally["dropped"].values()) + sum(tally["blocked"].values())}},
            "limit": [{"window": WINDOW_NAMES[name], "share": round(w["utilization"] * 100),
                       "resets": self.stamp(w["resets_at"]) if w["resets_at"] else "",
                       "seen": self.stamp(w["seen"])} for name, w in windows.items()],
            "tokens": [{**row, "name": RUN_KINDS.get(row["kind"], row["kind"]),
                        "cells": [RUN_KINDS.get(row["kind"], row["kind"]), *(grouped(row[k]) for k in (
                            "runs", "input", "output", "cache_read", "cache_write")), f"{row['cost_usd']:.2f}"]}
                       for row in core.store.tokens(now - WEEK)],
            "connectors": self.connectors(now),
            "mail": None,
            "charts": CHARTS,
        }
        if core.mail:
            def named(counts: dict) -> list:
                return [[KINDS.get(k, k), n] for k, n in sorted(counts.items(), key=lambda x: -x[1])]

            week = core.mail.store.tally(now - WEEK)
            status["mail"] = {
                "day": {"kept": named(tally["kept"]), "dropped": named(tally["dropped"]),
                        "blocked": [[s, n] for s, n in tally["blocked"].items()]},
                "week": {"kept": named(week["kept"]), "dropped": named(week["dropped"]),
                         "blocked": [[s, n] for s, n in week["blocked"].items()]},
                "rules": [f"отсеивать {sender}" if rule == "block" else
                          f"не уведомлять сразу: {sender}, {KINDS.get(kind, kind)}"
                          for rule, sender, kind in core.mail.store.rules()],
            }
        return status

    def connectors(self, now: float) -> list[dict]:
        """Each way in and out the router has, and how it is doing."""
        from . import clock

        core, out = self.core, []
        for channel in core.channels:
            out.append({"name": channel.name.capitalize() if channel.name == "telegram" else channel.name,
                        "ok": True, "state": "работает"})
        if core.travel is None:
            out.append({"name": "travel-ops", "ok": False, "state": "не подключён"})
        elif core.travel_state is None:
            out.append({"name": "travel-ops", "ok": True, "state": "подключён, ещё не опрошен"})
        else:
            seen, error = core.travel_state
            out.append({"name": "travel-ops", "ok": not error,
                        "state": f"{'не ответил' if error else 'ответил'} {clock.ago(now - seen)} назад"
                                 + (f": {error}" if error else "")})
        if core.mail:
            for row in core.mail.store.health():
                what = "почта" if row["source"] == "mail" else "календарь"
                if row["error"] == "invalid_grant":
                    state, ok = "нужен вход заново", False
                elif row["error"]:
                    state, ok = f"сбой: {row['error']}", False
                elif row["last_ok"]:
                    state, ok = f"синхронизирована {clock.ago(now - row['last_ok'])} назад", True
                else:
                    state, ok = "ещё не синхронизирована", False
                out.append({"name": f"{what} {row['account']}", "ok": ok, "state": state})
        else:
            out.append({"name": "почта и календарь", "ok": False, "state": "не подключены"})
        out.append({"name": "база знаний", "ok": bool(core.memory),
                    "state": "подключена" if core.memory else "не подключена"})
        if core.backup_status:
            said = core.backup(now)
            out.append({"name": "бэкап", "ok": said.startswith("бэкап —") and "не удался" not in said, "state": said})
        if said := self.health(now):
            out.append({"name": "хаб", "ok": said.startswith("хаб собран"), "state": said})
        return out

    def health(self, now: float) -> str:
        """The hub's part of the health line, from the builder's build.json: built when, or why not."""
        from . import clock

        if not self.build_status:
            return ""
        try:
            with open(self.build_status) as file:
                built = json.load(file)
            if not isinstance(built, dict):
                raise ValueError(built)
        except FileNotFoundError:
            return "хаб ещё не собран"
        except (OSError, ValueError):
            return "статус хаба не читается"
        if not built.get("ok"):
            return f"хаб не обновлён: {str(built.get('error') or 'причина не записана').rstrip('. ')}"
        return f"хаб собран {clock.ago(now - float(built.get('at') or 0))} назад"

    def write_status(self, now: float) -> None:
        from . import charts, clock

        status = self.status(now)
        self.data.write_bytes("charts/tokens.svg", charts.bars(
            "Токены за 7 дней по видам запусков", list(TOKENS),
            [(row["name"], [row["input"], row["output"], row["cache_read"], row["cache_write"]])
             for row in status["tokens"]]))
        days = []
        for back in range(6, -1, -1):
            moment = clock.local(now - back * DAY, self.core.tz)
            days.append((moment.date(), f"{clock.SHORT[moment.weekday()]} {moment.day}.{moment.month:02d}"))
        counts = {day: [0, 0] for day, _ in days}
        for ts, state in self.core.store.run_times(now - WEEK):
            day = clock.local(ts, self.core.tz).date()
            if day in counts:
                counts[day][0 if state in ("done", "limit") else 1] += 1
        self.data.write_bytes("charts/runs.svg", charts.bars(
            "Запуски модели по дням", ["удачные", "сбои"], [(label, counts[day]) for day, label in days]))
        self.data.write("status.json", status)

    # --- a trip page: her references, travel-ops' numbers ------------------------------------------------------------

    async def publish(self, trip, travel, now: float) -> tuple[bool, str]:
        """A trip page from what she sends (`TRIP_SCHEMA`): every option is looked up in travel-ops' stored search by
        its `search_id` and `link` — no site is asked — and the page takes the price, the seller, the times, the
        rating and when the price was seen from there. Refused whole, naming what to fix; nothing half-written."""
        try:
            check(TRIP_SCHEMA, trip)
        except Refused as exc:
            return False, f"Не опубликовано: {str(exc).rstrip('.')}."
        if travel is None:
            return False, "Не опубликовано: travel-ops не подключён — цены для страницы брать неоткуда."
        page_id = trip.get("trip_id")
        if page_id and not self.data.has("trips", page_id):
            return False, f"Не опубликовано: страницы {page_id} нет — опубликуй без trip_id."
        for number, option in enumerate(trip["options"]):
            if not linkable(option["link"], self.link_hosts):
                return False, (f"Не опубликовано: options[{number}].link — не ссылка на площадку travel-ops: возьми "
                               "link.url из ответа инструмента целиком.")
        views = {}
        try:
            for kind, search_id in dict.fromkeys((o["kind"], o["search_id"]) for o in trip["options"]):
                views[kind, search_id] = await travel.call(VIEWS[kind], {"search_id": search_id, "limit": VIEW_LIMIT,
                                                                         **LOOSE[kind]})
        except (TravelRefused, TravelUnavailable) as exc:
            return False, f"Не опубликовано: {str(exc).rstrip('.')}."
        options = []
        for number, option in enumerate(trip["options"]):
            view = views[option["kind"], option["search_id"]]
            found = next(((card, offer) for card, offer in offers(option["kind"], view)
                          if (offer.get("link") or {}).get("url") == option["link"]), None)
            if found is None:
                return False, (f"Не опубликовано: options[{number}] — этой ссылки нет в поиске {option['search_id']}. "
                               "Возьми link.url варианта из ответа travel-ops по этому search_id.")
            options.append(self.describe(option, *found, view))
        page_id = page_id or self.data.new_id("trips")
        self.data.write(f"trips/{page_id}.json", {
            "id": page_id, "title": trip["title"], "summary": trip["summary"], "start": trip["start"],
            "end": trip["end"], "published": self.stamp(now), "options": options})
        self.poke(now)
        if any(option.get("photo_from") for option in options):  # the page goes up now, its photos a little later
            self.pending.append(page_id)
        url = f"{self.url}/trips/{page_id}/"
        return True, (f"Опубликовано: {url} (trip_id {page_id} — чтобы обновить эту же страницу). В конце ответа "
                      f"Владельцу: [подробнее]({url})")

    async def photos(self, page_id: str, travel, now: float) -> None:
        """Photos of the page's stays: travel-ops fetches them from the sites (`stay_photos`; the router has no way
        there, and a page may not load them from there), the router's file worker re-encodes each as a JPEG, and what
        does not decode is dropped. A look that fails leaves the page as it was."""
        from .attachments import Unreadable, Upload, isolated

        name = f"trips/{page_id}.json"
        page = self.data.read(name) or {}
        found: dict[str, list[str]] = {}
        for number, option in enumerate(page.get("options") or [], 1):
            source = option.get("photo_from") or {}
            if not source.get("stay"):
                continue
            try:
                result = await travel.result("stay_photos", {"search_id": source["search_id"],
                                                             "stays": [source["stay"]], "per_stay": PHOTOS})
            except (TravelRefused, TravelUnavailable) as exc:
                log.warning("lifehub: photos of %s not fetched: %s", page_id, exc)
                continue
            names = []
            for block in result.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "image" or len(names) >= PHOTOS:
                    continue
                try:
                    done = await isolated(Upload("photo", base64.b64decode(str(block.get("data", "")))))
                except (Unreadable, ValueError):
                    continue
                names.append(f"photos/{page_id}/{number}-{len(names) + 1}.jpg")
                self.data.write_bytes(names[-1], done.files[0][1])
            if names:
                found[option["link"]] = names
        page = self.data.read(name)  # she may have published it again meanwhile: her new page, these photos
        if found and page:
            for option in page.get("options") or []:
                option["photos"] = found.get(option["link"], option.get("photos") or [])
            self.data.write(name, page)
            self.poke(now)

    def describe(self, option: dict, card: dict, offer: dict, view: dict) -> dict:
        """One option as the page shows it, in code's words from travel-ops' card: what, the price, the lines."""
        kind = option["kind"]
        price = money(offer.get("price") or offer.get("total"))
        converted = money(offer.get("converted"))
        seen = f"цена на {self.moment(offer.get('seen_at'))}"
        if view.get("searched_at"):
            seen += f" (поиск {self.moment(view['searched_at'])})"
        out = {"kind": kind, "what": "", "pick": option.get("pick") is True, "note": option["note"], "price": price,
               "converted": "" if converted == price else converted, "seller": str(offer.get("seller") or ""),
               "lines": [], "seen": seen, "link": option["link"], "linkable": True, "photos": []}
        if kind == "flight":
            stops = card.get("stops") or 0
            changes = f"{stops} {plural(stops, 'пересадка', 'пересадки', 'пересадок')}" if stops else "без пересадок"
            out["what"] = f"Перелёт {' → '.join(card.get('route') or [])}, {changes}"
            for word, legs in (("туда", card.get("outbound") or []), ("обратно", card.get("inbound") or [])):
                if legs:
                    flights = dict.fromkeys(str(leg.get("flight") or leg.get("carrier")) for leg in legs)
                    out["lines"].append(f"{word} {clock_of(legs[0]['departs'])} → {clock_of(legs[-1]['arrives'])} · "
                                        + ", ".join(flights))
            out["lines"].append("в пути " + duration(card.get("duration_min"))
                                + (f", обратно {duration(card['return_duration_min'])}"
                                   if card.get("return_duration_min") else ""))
            baggage = offer.get("baggage") or {}
            out["lines"].append("багаж: " + ("включён" + (f", {baggage['checked_kg']} кг" if baggage.get("checked_kg")
                                                           else "") if baggage.get("checked") else
                                             "только ручная кладь" if baggage.get("checked") is False else
                                             "источник не сказал"))
        elif kind == "stay":
            stay = card.get("stay") or {}
            out["what"] = f"Жильё: {stay.get('name', '')}"
            about = [STAY_KINDS.get(stay.get("kind"), "жильё")]
            if stay.get("rating") is not None:
                reviews = stay.get("reviews")
                about.append(f"рейтинг {decimal(stay['rating'])}" + (
                    f" ({reviews} {plural(reviews, 'отзыв', 'отзыва', 'отзывов')})" if reviews else ""))
            if stay.get("center_km") is not None:
                about.append(f"{decimal(stay['center_km'])} км от центра")
            out["lines"].append(" · ".join(about))
            nights = card.get("nights") or 0
            night = [f"{nights} {plural(nights, 'ночь', 'ночи', 'ночей')}"] if nights else []
            night += [f"{money(offer['per_night'])} за ночь"] if offer.get("per_night") else []
            night += [str(offer["room"])] if offer.get("room") else []
            out["lines"].append(" · ".join(night))
            if offer.get("free_cancellation"):
                out["lines"].append("бесплатная отмена" + (f" до {offer['free_cancel_until']}"
                                                           if offer.get("free_cancel_until") else ""))
            elif offer.get("free_cancellation") is False:
                out["lines"].append("без бесплатной отмены")
            out["photo_from"] = {"search_id": option["search_id"], "stay": str(stay.get("source_id") or "")}
        else:
            rides = card.get("rides") or []
            out["what"] = "Дорога " + " → ".join([str(rides[0].get("from"))] + [str(r.get("to")) for r in rides]) \
                if rides else "Дорога"
            for ride in rides:
                out["lines"].append(f"{RIDES.get(ride.get('mode'), ride.get('mode'))} {clock_of(ride.get('departs'))}"
                                    f" → {clock_of(ride.get('arrives'))} · {ride.get('carrier') or ''} "
                                    f"{ride.get('number') or ''}".rstrip(" ·"))
            out["lines"].append("в пути " + duration(card.get("duration_min")))
        return out

    def moment(self, iso) -> str:
        try:
            return self.stamp(datetime.fromisoformat(str(iso)).timestamp())
        except ValueError:
            return str(iso)


VIEWS = {"flight": "refine_flights", "stay": "refine_stays", "ground": "refine_ground"}
# A view of a stored search with the bars as loose as they go, sorted not by price (a price order thins near-copies
# out): her option is looked for among all of it. A stay with no rating at all is still hidden by any rating bar.
LOOSE = {"flight": {"max_stops": 3, "max_leg_hours": 72, "sort": "departure"},
         "stay": {"min_rating": 0, "max_center_km": 100, "sort": "rating"}, "ground": {"sort": "departure"}}
VIEW_LIMIT = 100
PHOTOS = 2  # photos of one stay on the page
STAY_KINDS = {"hotel": "отель", "apartment": "апартаменты", "room": "комната", "house": "дом",
              "shared_room": "место в общей комнате", "other": "жильё"}
RIDES = {"train": "поезд", "bus": "автобус", "ferry": "паром", "van": "микроавтобус"}


def offers(kind: str, view: dict):
    """(card, offer) of every price in a view of travel-ops: a flight's fares by group, a stay's rates, a ride's fares."""
    for card in view.get("cards") or []:
        holders = card.get("groups") or [] if kind == "flight" else [card]
        for holder in holders:
            for offer in holder.get("rates" if kind == "stay" else "fares") or []:
                if isinstance(offer, dict):
                    yield card, offer


def money(value) -> str:
    """{"amount": "497.00", "currency": "EUR"} -> «497 EUR»."""
    if not isinstance(value, dict):
        return ""
    try:
        amount = Decimal(str(value.get("amount")))
        shown = f"{amount:.0f}" if amount == amount.to_integral() else f"{amount:.2f}".replace(".", ",")
    except InvalidOperation:
        return ""
    return f"{shown} {value.get('currency') or ''}".strip()


def decimal(number) -> str:
    return f"{number:g}".replace(".", ",")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def clock_of(iso) -> str:
    """A local time as the source gives it (the airport's, the station's): «22.10 12:50»."""
    try:
        return f"{datetime.fromisoformat(str(iso)):%d.%m %H:%M}"
    except ValueError:
        return str(iso)


def duration(minutes) -> str:
    if not isinstance(minutes, int):
        return "?"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours and minutes else f"{hours} ч" if hours else f"{minutes} мин"


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def front_matter(path: Path) -> dict | None:
    """The YAML head of a Record, or None when there is none or it does not parse."""
    try:
        text = path.read_text(errors="replace")
        if not text.startswith("---\n") or (end := text.find("\n---", 4)) < 0:
            return None
        head = yaml.safe_load(text[4:end])
        return head if isinstance(head, dict) else None
    except (OSError, yaml.YAMLError):
        return None
