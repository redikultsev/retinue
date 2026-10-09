"""travel-ops: what the assistant may send to the travel sites.

The assistant calls travel-ops' MCP server herself and reads its whole answer — reviews, descriptions, photos
(architecture.md, «Вечер 2026-10-07»). What goes the other way is checked here, in code, before every call (the
engine's PreToolUse hook): the arguments of a search are what the sites receive, and the owner's data must not
ride out in them. Closed types only — airport codes, dates, numbers, choices, a place name in Latin script of a
few words — and anything else is refused with the reason, which the model reads and acts on.
"""

from __future__ import annotations

import functools
import itertools
import re
from collections.abc import Callable
from datetime import date
from typing import Any

import airportsdata
import httpx

PREFIX = "mcp__travel__"  # how the model sees travel-ops' tools: the MCP server is named `travel`
# travel-ops' tools that ask the sites and take minutes; the rest read its own memory.
SEARCHES = ("search_trip", "search_flights", "search_stays", "search_ground")


class Refused(ValueError):
    """The form is wrong; the text says what to fix, in words the model acts on."""


Check = Callable[[Any], Any]  # returns the clean value or raises ValueError with the reason


def pattern(regex: str, what: str, upper: bool = False, longest: int = 60) -> Check:
    compiled = re.compile(regex)

    def check(value):
        if not isinstance(value, str):
            raise ValueError(what)
        value = value.strip().upper() if upper else value.strip()
        if len(value) > longest or not compiled.fullmatch(value):
            raise ValueError(what)
        return value
    return check


def integer(low: int, high: int) -> Check:
    def check(value):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"целое от {low} до {high}")
        return value
    return check


def number(low: float, high: float) -> Check:
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"число от {low:g} до {high:g}")
        return value
    return check


def flag(value):
    if not isinstance(value, bool):
        raise ValueError("да или нет: true или false")
    return value


def choice(*values: str) -> Check:
    def check(value):
        if value not in values:
            raise ValueError(f"одно из: {', '.join(values)}")
        return value
    return check


def many(item: Check, most: int) -> Check:
    def check(value):
        if not isinstance(value, list) or not 1 <= len(value) <= most:
            raise ValueError(f"список, от 1 до {most}")
        try:
            return [item(v) for v in value]
        except ValueError as exc:
            raise ValueError(f"в списке: {exc}") from None
    return check


def obj(value):
    if not isinstance(value, dict):
        raise ValueError("нужен объект с полями")
    return value


def day(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("дата вида 2026-11-14") from None
    return value


@functools.cache
def known_codes() -> frozenset[str]:
    """Every IATA airport code and city group (MOW, LON) airportsdata knows: about 7 900 of 17 576 letter triples."""
    return frozenset(airportsdata.load("IATA")) | frozenset(airportsdata.load_iata_macs())


def real_codes(check: Check) -> Check:
    def checked(value):
        codes = check(value)
        unknown = [code for code in codes.split(",") if code not in known_codes()]
        if unknown:
            raise ValueError(f"код аэропорта: {', '.join(unknown)} — нет такого аэропорта IATA")
        return codes
    return checked


# ISO 4217 codes of currencies in use (without funds, metals and XXX/XTS): a currency is a choice, not a string.
ISO_CURRENCIES = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF
CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG
HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA
MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD
RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX
USD UYU UZS VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG""".split())


def currency(value):
    if not isinstance(value, str) or value.strip().upper() not in ISO_CURRENCIES:
        raise ValueError("код валюты ISO 4217, например EUR")
    return value.strip().upper()


AIRPORTS = real_codes(pattern(r"[A-Z]{3}(,[A-Z]{3}){0,5}", "код аэропорта IATA или несколько через запятую, "
                              "например BEG или BEG,INI", upper=True))
CARRIER = pattern(r"[A-Z0-9]{2}", "код авиакомпании из двух знаков, например JU", upper=True)
# A place is a few words of Latin script: no digits, no sentence. This string is the only free one in the form.
PLACE = pattern(r"[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F'.\-]*( [A-Za-z\u00C0-\u024F'.\-]+){0,4}",
                "название латиницей, до пяти слов и 60 знаков, без цифр, например Kotor или Herceg Novi")
CLOCK = pattern(r"([01]\d|2[0-3]):[0-5]\d", "время вида 18:00")
SEARCH_ID = pattern(r"[a-z0-9]{6,16}", "search_id из результата поиска")
WATCH_ID = pattern(r"w[a-z0-9]{7}", "watch_id из watches")
CURRENCY = currency
SOURCE = pattern(r"[a-z0-9]{2,12}", "имя источника, например aviasales")
AGE = integer(0, 17)
PARTY = {"adults": integer(1, 9), "children_ages": many(AGE, 9)}
LIMIT = integer(1, 20)  # cards in one answer: the whole answer goes into the conversation, so not a hundred
ASK = {"currency": CURRENCY, "confirm": flag, "limit": LIMIT, "refresh": flag}
CABIN = choice("economy", "premium_economy", "business", "first")
MODES = many(choice("train", "bus", "ferry", "van"), 4)
KINDS = many(choice("hotel", "apartment", "room", "house", "shared_room", "other"), 6)
# A stay's name as the results gave it (any script); an amenity word for a filter travel-ops applies itself.
STAY = pattern(r"[^\x00-\x1f]+", "название из выдачи этого поиска, до 100 знаков", longest=100)
AMENITY = pattern(r"[A-Za-zА-Яа-яЁё]+( [A-Za-zА-Яа-яЁё]+)?", "слово удобства без цифр, например wifi", longest=30)

# Each tool: field -> (check, required). A field travel-ops has and the form lacks is refused: a new field of a new
# travel-ops version waits until it has a check here.
FORMS: dict[str, dict[str, tuple[Check, bool]]] = {
    "search_trip": {"origin": (AIRPORTS, False), "place": (PLACE, True), "depart": (day, True),
                    "return_date": (day, False), "checkout": (day, False), "flex_days": (integer(0, 3), False),
                    "separate_tickets": (flag, False), "country": (PLACE, False), "airports": (AIRPORTS, False),
                    "max_airports": (integer(1, 3), False), "stay_adults": (integer(1, 9), False),
                    "cabin": (CABIN, False), "max_stops": (integer(0, 3), False),
                    "min_rating": (number(0, 10), False), "max_center_km": (number(0.1, 100), False),
                    **{k: (v, False) for k, v in {**PARTY, **ASK}.items()}},
    "search_flights": {"origin": (AIRPORTS, False), "destination": (AIRPORTS, True), "depart": (day, True),
                       "return_date": (day, False), "flex_days": (integer(0, 3), False), "cabin": (CABIN, False),
                       "sources": (many(SOURCE, 8), False), "max_stops": (integer(0, 3), False),
                       "separate_tickets": (flag, False), **{k: (v, False) for k, v in {**PARTY, **ASK}.items()}},
    "search_stays": {"place": (PLACE, True), "checkin": (day, True), "checkout": (day, True),
                     "sources": (many(SOURCE, 3), False), "min_rating": (number(0, 10), False),
                     "max_center_km": (number(0.1, 100), False),
                     **{k: (v, False) for k, v in {**PARTY, **ASK}.items()}},
    "search_ground": {"origin": (PLACE, True), "destination": (PLACE, True), "depart": (day, True),
                      "modes": (MODES, False), "sources": (many(SOURCE, 3), False),
                      **{k: (v, False) for k, v in {**PARTY, **ASK}.items()}},
    "refine_flights": {"search_id": (SEARCH_ID, True), "limit": (LIMIT, False),
                       "max_stops": (integer(0, 3), False), "max_leg_hours": (number(1, 72), False),
                       "max_connection_hours": (number(0, 48), False), "depart_after": (CLOCK, False),
                       "depart_before": (CLOCK, False), "return_after": (CLOCK, False),
                       "return_before": (CLOCK, False), "airlines": (many(CARRIER, 10), False),
                       "avoid_airlines": (many(CARRIER, 10), False), "destination": (many(AIRPORTS, 6), False),
                       "checked_bag": (flag, False), "max_price": (number(0.01, 1e7), False),
                       "sort": (choice("price", "duration", "departure"), False)},
    "refine_stays": {"search_id": (SEARCH_ID, True), "limit": (LIMIT, False),
                     "min_rating": (number(0, 10), False), "min_reviews": (integer(0, 100_000), False),
                     "max_total": (number(0.01, 1e7), False), "kinds": (KINDS, False),
                     "exclude_kinds": (KINDS, False), "no_hostels": (flag, False),
                     "min_bedrooms": (integer(0, 10), False), "sources": (many(SOURCE, 3), False),
                     "max_center_km": (number(0.1, 100), False), "free_cancellation": (flag, False),
                     "must_have": (many(AMENITY, 10), False),
                     "sort": (choice("price", "rating", "reviews", "center"), False)},
    "refine_ground": {"search_id": (SEARCH_ID, True), "limit": (LIMIT, False), "modes": (MODES, False),
                      "depart_after": (CLOCK, False), "depart_before": (CLOCK, False),
                      "max_changes": (integer(0, 5), False), "max_price": (number(0.01, 1e7), False),
                      "sources": (many(SOURCE, 3), False),
                      "sort": (choice("price", "duration", "departure"), False)},
    # `stays` are names or ids from that search's own results: travel-ops matches them against the search it keeps
    # and asks the sites only for a stay it found there, so a name is never sent anywhere.
    "stay_details": {"search_id": (SEARCH_ID, True), "stays": (many(STAY, 5), True), "refresh": (flag, False)},
    "stay_photos": {"search_id": (SEARCH_ID, True), "stays": (many(STAY, 5), True),
                    "per_stay": (integer(1, 4), False)},
    "airports_near": {"place": (PLACE, True), "country": (PLACE, False), "radius_km": (integer(10, 400), False)},
    "watches": {"include_stopped": (flag, False)},
    "stop_watch": {"watch_id": (WATCH_ID, True)},
    "sources": {},
}
WATCHED = {"flights": ("search_flights", "refine_flights"), "stays": ("search_stays", "refine_stays"),
           "ground": ("search_ground", "refine_ground")}
SET_BY_THE_WATCH = ("refresh", "confirm", "limit", "currency")  # travel-ops refuses them in a watch's arguments
FORMS["watch_price"] = {"kind": (choice(*WATCHED), True), "arguments": (obj, True), "filters": (obj, False),
                        "below": (number(0.01, 1e7), False), "drop_percent": (number(1, 90), False),
                        "every_hours": (number(3, 168), False), "currency": (CURRENCY, False)}
# Not hers: the router takes the alerts and keeps them until they are told (task 9); one she took would be lost to it.
NOT_HERS = {"watch_alerts": "оповещения о цене забирает Роутер и сам будит тебя, когда цена упала"}


def fill(form: dict[str, tuple[Check, bool]], given: Any, where: str = "", partial: bool = False) -> dict:
    """The given fields, each checked and cleaned, or Refused naming the first wrong one."""
    if not isinstance(given, dict):
        raise Refused(f"{where or 'аргументы'}: нужен объект с полями")
    prefix = f"{where}: " if where else ""
    unknown = sorted(str(name) for name in set(given) - set(form))
    if unknown:
        named = ", ".join(name[:40].encode("utf-8", "backslashreplace").decode() for name in unknown[:5])
        raise Refused(f"{prefix}неизвестное поле {named}. Поля: {', '.join(form)}.")
    missing = [name for name, (_, required) in form.items()
               if required and not partial and given.get(name) is None]  # null where a value is needed: none
    if missing:
        raise Refused(f"{prefix}нет поля {', '.join(missing)}.")
    clean = {}
    for name, value in given.items():
        if value is None:
            continue  # the model sends null for «not set»: travel-ops takes the default
        try:
            clean[name] = form[name][0](value)
        except ValueError as exc:
            raise Refused(f"{where + '.' if where else ''}{name}: {exc}.") from None
    return clean


def check(tool: str, given: Any) -> dict:
    """The checked arguments of a call to travel-ops' `tool`, or Refused. A watch's arguments and filters are
    checked against the search it repeats and that search's refine tool."""
    if tool in NOT_HERS:
        raise Refused(f"{tool} не для тебя: {NOT_HERS[tool]}")
    if tool not in FORMS:
        raise Refused(f"неизвестный инструмент travel-ops: {tool}. Его нет в проверке Роутера, звать его нельзя.")
    args = fill(FORMS[tool], given)
    if tool == "watch_price":
        search, refine = WATCHED[args["kind"]]
        searching = {k: v for k, v in FORMS[search].items() if k not in SET_BY_THE_WATCH}
        refining = {k: v for k, v in FORMS[refine].items() if k not in ("search_id", "limit")}
        args["arguments"] = fill(searching, args["arguments"], "arguments")
        if "filters" in args:
            args["filters"] = fill(refining, args["filters"], "filters", partial=True)
    return args


def as_given(value: Any) -> Any:
    """The arguments as the model sent them, «not set» (null) left out, as `check` leaves it out."""
    if isinstance(value, dict):
        return {k: as_given(v) for k, v in value.items() if v is not None}
    return value


def exact(tool: str, given: Any) -> dict:
    """`check`, and the call goes out only as written: a value the check had to rewrite (case, spaces) is refused
    with its right spelling. What travel-ops receives is then exactly what was checked, with no rewrite of the
    call in between that the CLI might or might not apply."""
    clean = check(tool, given)
    sent = as_given(given)
    if sent != clean:
        for name, value in clean.items():
            if sent.get(name) != value:
                if isinstance(value, dict) and isinstance(sent.get(name), dict):
                    for inner, right in value.items():
                        if sent[name].get(inner) != right:
                            raise Refused(f"{name}.{inner}: пиши ровно {right!r}")
                raise Refused(f"{name}: пиши ровно {value!r}")
        raise Refused("аргументы: пиши ровно так, как требует форма")
    return clean


# --- the router's own client: price alerts ------------------------------------------------------------------

PROTOCOL = "2025-11-25"  # an MCP version whose single POST needs no envelope (travel-ops task 1)
MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?)?")
URL = re.compile(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[^\s<>\"'`\\]*)?")


class Unavailable(Exception):
    """travel-ops did not answer, or answered something that is not an answer."""


def words(value: Any, cap: int) -> str | None:
    """Somebody else's text in a message the router writes itself: one line, defused, cut."""
    from .core import defuse  # here, not at the top: the engine imports this module and needs none of the core

    if not isinstance(value, str) or not value.strip():
        return None
    value = defuse(" ".join(value.split()))
    return value if len(value) <= cap else value[:cap - 1] + "…"


def num(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def matching(value: Any, regex: str) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(regex, value) else None


def alert(value: Any) -> dict:
    """A price drop a watch found, as the router keeps it and tells it: the fields it needs, nothing else. The
    assistant reads travel-ops whole through her own tools; this one goes into the router's own message."""
    if not isinstance(value, dict):
        return {}
    link = value.get("link")
    fields = {"alert_id": num(value.get("alert_id")), "watch_id": matching(value.get("watch_id"), r"w[a-z0-9]{7}"),
              "what": words(value.get("what"), 120), "price": num(value.get("price")),
              "currency": matching(value.get("currency"), r"[A-Z]{3}"),
              "last_told": num(value.get("last_told")), "why": words(value.get("why"), 80),
              "seller": words(value.get("seller"), 40),
              "link": link if isinstance(link, str) and len(link) <= 1000 and URL.fullmatch(link) else None,
              "seen_at": matching(value.get("seen_at"), MOMENT.pattern),
              "sources_without_answer": words(value.get("sources_without_answer"), 120)}
    return {k: v for k, v in fields.items() if v is not None}


class TravelOps:
    """`travelops mcp --http` as the router reaches it: one JSON-RPC `tools/call` per POST, no session, so a
    restart of either side loses only the call in flight. The router only collects price alerts with it."""

    def __init__(self, url: str, http: httpx.AsyncClient | None = None) -> None:
        self.url = url
        # trust_env=False: a neighbour on an internal network, never reached through a proxy.
        self.http = http or httpx.AsyncClient(trust_env=False)
        self.ids = itertools.count(1)

    async def call(self, tool: str, arguments: dict, timeout: float = 60.0) -> dict:
        """travel-ops' structured answer. Raises Refused when travel-ops refused the call, Unavailable otherwise."""
        structured = (await self.result(tool, arguments, timeout)).get("structuredContent")
        return structured if isinstance(structured, dict) else {}

    async def result(self, tool: str, arguments: dict, timeout: float = 60.0) -> dict:
        """The whole MCP result of a call: its `content` blocks too — the pictures of `stay_photos`."""
        body = {"jsonrpc": "2.0", "id": next(self.ids), "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        try:
            response = await self.http.post(self.url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise Unavailable(f"travel-ops недоступен ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise Unavailable(f"travel-ops ответил HTTP {response.status_code}.")
        try:
            result = response.json()["result"]
        except (ValueError, KeyError, TypeError):
            raise Unavailable("travel-ops ответил не по протоколу.") from None
        if not isinstance(result, dict):
            raise Unavailable("travel-ops ответил не по протоколу.")
        if result.get("isError"):
            said = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
            raise Refused(f"travel-ops отказал: {words(said, 300) or 'без причины'}")
        return result

    async def alerts(self) -> list[dict]:
        """Price drops not yet collected, looked at without taking them: the router keeps them first."""
        result = await self.call("watch_alerts", {"take": False})
        return [a for a in (alert(x) for x in result.get("alerts") or []) if isinstance(a.get("alert_id"), int)]

    async def confirm(self, upto: int) -> list[int]:
        """Mark as given out exactly the alerts up to `upto`, the last one the router kept."""
        result = await self.call("watch_alerts", {"upto": upto})
        return [a.get("alert_id") for a in result.get("alerts") or [] if isinstance(a, dict)]
