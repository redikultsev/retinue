"""The owner's one queue «Waiting for you»: what waits for him across the sessions and the assistant's world, grouped by what
he does about it, not by where it came from. The face shows it on its home screen (`GET /api/feed`),
the router as text for `/wait` (`POST /owner/feed`).

Built by code on every read from the platform's own rows — never a copy kept beside them that could drift:

- «Answer the question»: a question card (AskUserQuestion) shown and not answered, with what its session wrote just
  before asking (`said`): the case and what each answer leads to, which the question alone seldom says;
- «Decide»: an approval card shown, a courier card of the last day he has not closed («Done») nor written to its session
  since, a sub-session a session proposed
  («Start» / «Not now», `subs`), and a write to a collection a session proposed («Apply» / «Dismiss», `colltools`);
- «Your step»: a session whose turn ended on a step of his own (`endings`);
- «Reply»: a session waiting for his message with no card shown;
- «Read»: a session whose finished turn he has not read (the session's `unread`; nothing without it);
- «From the assistant»: the assistant's digest.

A session whose turn ended with a turn ending (`endings`) and no card shown is placed by the ending's kind instead —
ask under «Answer the question», approve under «Decide», step under «Your step», look and done under «Read» (while
unread, as any turn there) — and its row carries the ending whole (`ending`).

The owner's computers' own sessions (`computers.py`) are rows too, while their computer reports: one whose turn ended
with an ending, by its kind as above (look and done under «Read» while the computer lists it); one that waits on him
with none, under «Reply». Such a row has no session of the room (`session` null) and carries `computer` — the device,
the session's id there and its `claude://` link —. He answers it here, on its card: his words wait for that computer's
next report (`answered` on the row until it takes them).

The digest is the one thing kept. The router knows what waits in her world — a letter or a chat she judged to ask
something of him, a draft waiting for «Send», a deadline of the base — and pushes it here (`POST /owner/waiting`)
whole each time it changes and at least hourly; the platform never calls the router. It is minimal by contract:
an id, a kind, a display name, a one-line title, a time, a sphere — no text of a letter, no address; a deadline also
its day and its Record's path; a person's row its token `t`. What the face does with it is one link to the bot's
chat: sending and deciding stay in Telegram, where an outward action is a button under its card. A person's row can
be closed in the face — «Done», «Not a task», «Tomorrow», «Undo»: the press goes back to the router as a note by its
token (`waiting-press`), and only the router knows what the token names. Beside the items, two counts: what closed
today, what ran out unmarked in the week. A deadline whose time has not come yet (`since`
ahead) is no one's queue: the face shows it only under «Deadlines». A reminder (its day `when`, its `repeat`) is no
queue's either: it fires in Telegram, and the face shows it at its time on the day and the week, with «Plan» — a
plan press that makes its task and lays it (`place`, by its id). The same push carries the
status chips (`chips`: the backup, the limit, the pages' build), for the face's «Today».
"""

from __future__ import annotations

import asyncio
import json
import math
import posixpath
import re
import secrets
import unicodedata
from datetime import date
from typing import TYPE_CHECKING

from . import migrate, questions
from .courier import INVISIBLE, line

if TYPE_CHECKING:
    from .sessions import PlatformStore

ACTIONS = (("answer", "Answer the question"), ("decide", "Decide"), ("step", "Your step"), ("reply", "Reply"),
           ("read", "Read"), ("assistant", "From the assistant"))
# A turn ending's row, by its kind (`endings.KINDS`).
ENDING_ACTIONS = {"ask": "answer", "approve": "decide", "step": "step", "look": "read", "done": "read"}
COURIER_S = 86400            # a courier card waits in «Decide» this long, unless he wrote into its session after it
RECENT_S = 7 * 86400         # a summary older than this leaves nothing to read in the feed
ESSENCE_CHARS = 200          # the one line a row says
SAID_CHARS = 4000            # a question's case: what its session wrote just before asking, cut here
TELEGRAM_ROWS = 10           # `/wait` names this many rows; the rest are counted
PUSH_HINT = "Press «Push» under its result or open it in the app."
PROPOSAL_HINT = "Start it or set it aside in the app."
CPROPOSAL_HINT = "Apply it or dismiss it in the app."

# The digest: what the router may push, and nothing more.
DIGEST_KINDS = ("letter", "chat", "draft", "deadline", "reminder")
DIGEST_KEYS = {"id", "kind", "who", "title", "since", "sphere"}
DEADLINE_KEYS = {"when", "path"}  # a deadline's own, both optional: its day and its Record's path in the base
REMINDER_KEYS = {"when", "repeat"}  # a reminder's own: its day, and how it repeats when it does
REMINDER_ID = re.compile(r"reminder:\d{1,12}")
REPEATS = ("daily", "weekdays", "weekly", "monthly")
TOKEN = re.compile(r"[0-9a-f]{32}")  # a person's row's `t`: what a press in the app names the row by (`waiting`)
PRESSES = ("done", "dismissed", "later", "open")  # «Done», «Not a task», «Tomorrow», «Undo»
COUNTS = ("closed_today", "expired_week")  # the digest's two numbers
COUNT_MAX = 100_000
DIGEST_ITEMS = 50
DIGEST_BYTES = 32 * 1024
DIGEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,95}")
SPHERE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
BOT = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")  # a Telegram username, as t.me takes it
WHO_CHARS, TITLE_CHARS = 60, 160
FUTURE_S = 400 * 86400      # a deadline's day may lie ahead; a time further than this is not a time
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# A Record's path in the base: a Space, at most two folders, a `.md` file; no dot-led part, so no `..`.
RECORD = re.compile(r"[a-z][a-z0-9_-]{0,31}(?:/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){1,3}\.md")
# The hub's status chips (`waiting.chips`): what each says, in its own words, and how it is doing.
CHIP_KEYS = ("health", "backup", "limit", "build")
CHIP_LEVELS = ("ok", "warn", "bad")
CHIP_CHARS = 60
EMAIL = re.compile(r"[^\s@<>()\[\]\"',;]+@[^\s@<>()\[\]\"',;]+\.[^\s@<>()\[\]\"',;]+")
ADDRESS = "‹address›"
# A row's sheet (`Details`): asked of the router for one row when the owner opens it, relayed, never kept.
DETAIL_WAIT_S = 12.0         # the app's request waits this long for the router's answer: its pulls and the build
DETAIL_PENDING = 8           # requests waiting at once, at most
DETAIL_HOLD_S = 25.0         # the router's long call for what was asked is held open this long, at most
DETAIL_BYTES = 24 * 1024
DETAIL_REQUEST = re.compile(r"[0-9a-f]{16}")
DETAIL_KINDS = ("letter", "chat", "draft")
DETAIL_NEEDS = ("reply", "decide", "pay", "attend", "act")
DETAIL_ASKS = 10
DETAIL_JUDGED, DETAIL_DEADLINE, DETAIL_ADDRESS, DETAIL_DRAFT = 700, 40, 120, 4000
DRAFT_REF = re.compile(r"draft:[0-9a-f]{8}")
DRAFT_STATES = ("shown", "link")
# Where a thing is, in the only shapes the router makes: a Gmail thread in a mailbox, a Telegram chat.
LINKS = {"Open in Gmail": re.compile(r"https://mail\.google\.com/mail/\?authuser=[^\s@/?#&]{1,64}@[^\s@/?#&]{1,190}"
                                     r"#all/[0-9a-f]{10,24}"),
         "Open the chat": re.compile(r"https://t\.me/[A-Za-z][A-Za-z0-9_]{3,31}")}


class Refused(ValueError):
    """A digest not shaped as the contract says: refused whole, so the one on record stays as it was."""


def _text(value: object, cap: int) -> str:
    """Somebody's words for one line: folded (NFKC: a fullwidth ＠ is an @) and invisible characters dropped before
    anything shaped as an e-mail address is written as ‹address› — the digest carries a display name, never an
    address — then one line, cut."""
    return line(EMAIL.sub(ADDRESS, line(unicodedata.normalize("NFKC", str(value)), 4 * DIGEST_BYTES)), cap)


def clean(body: object, now: float) -> tuple[list[dict], str]:
    """The router's digest, checked: `{items: [{id, kind, who, title, since, sphere?}], bot?}`. Every item must fit,
    or none is taken (`Refused`): it replaces the one on record whole, so half of it would read as the rest done."""
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise Refused("items: not a list")
    bot = body.get("bot") or ""
    if not isinstance(bot, str) or (bot and not BOT.fullmatch(bot)):
        raise Refused("bot: not a bot name")
    if len(body["items"]) > DIGEST_ITEMS:
        raise Refused(f"items: over {DIGEST_ITEMS}")
    items, seen = [], set()
    for n, item in enumerate(body["items"]):
        kind = item.get("kind") if isinstance(item, dict) else None
        allowed = DIGEST_KEYS | DEADLINE_KEYS if kind == "deadline" else DIGEST_KEYS | REMINDER_KEYS \
            if kind == "reminder" else DIGEST_KEYS | {"t"} if kind in ("letter", "chat") else DIGEST_KEYS
        if not isinstance(item, dict) or not allowed >= set(item) or not DIGEST_KEYS - {"sphere"} <= set(item):
            raise Refused(f"items[{n}]: wrong fields")
        if not isinstance(item["id"], str) or not DIGEST_ID.fullmatch(item["id"]) or item["id"] in seen:
            raise Refused(f"items[{n}].id: invalid or repeated")
        if item["kind"] not in DIGEST_KINDS:
            raise Refused(f"items[{n}].kind: not one of {', '.join(DIGEST_KINDS)}")
        since = item["since"]
        if type(since) not in (int, float) or not math.isfinite(since) or not 0 < since <= now + FUTURE_S:
            raise Refused(f"items[{n}].since: not a time")
        sphere = item.get("sphere")
        if sphere is not None and (not isinstance(sphere, str) or not SPHERE.fullmatch(sphere)):
            raise Refused(f"items[{n}].sphere: not a sphere name")
        if not isinstance(item["who"], str) or not isinstance(item["title"], str):
            raise Refused(f"items[{n}]: who and title must be strings")
        if "t" in item and (not isinstance(item["t"], str) or not TOKEN.fullmatch(item["t"])):
            raise Refused(f"items[{n}].t: not a token")
        if kind == "reminder" and (not REMINDER_ID.fullmatch(item["id"]) or item.get("repeat", "daily") not in REPEATS):
            raise Refused(f"items[{n}]: not a reminder's id or repeat")
        seen.add(item["id"])
        items.append({"id": item["id"], "kind": item["kind"], "who": _text(item["who"], WHO_CHARS),
                      "title": _text(item["title"], TITLE_CHARS), "since": float(since), "sphere": sphere,
                      **({"t": item["t"]} if "t" in item else {}),
                      **({"repeat": item["repeat"]} if "repeat" in item else {}), **_deadline(item, n)})
    return items, bot


def clean_counts(body: object) -> dict:
    """The digest's two numbers: asks closed today and asks run out unmarked in the week, each a count — 0 when the
    router sends none. One off the contract refuses the whole push (`Refused`)."""
    out = {}
    for key in COUNTS:
        value = body.get(key, 0) if isinstance(body, dict) else 0
        if type(value) is not int or not 0 <= value <= COUNT_MAX:
            raise Refused(f"{key}: not a number")
        out[key] = value
    return out


def _deadline(item: dict, n: int) -> dict:
    """A deadline's day (`YYYY-MM-DD`, a real date) and its Record's path (`RECORD`), each when given."""
    out = {}
    if "when" in item:
        when = item["when"]
        try:
            ok = isinstance(when, str) and DAY.fullmatch(when) and date.fromisoformat(when)
        except ValueError:
            ok = False
        if not ok:
            raise Refused(f"items[{n}].when: not a date")
        out["when"] = when
    if "path" in item:
        if not isinstance(item["path"], str) or not RECORD.fullmatch(item["path"]):
            raise Refused(f"items[{n}].path: not a Record path")
        out["path"] = item["path"]
    return out


def _words(value: object, cap: int, field: str) -> str:
    """Her words or a draft's for the sheet: a string, invisible characters dropped, lines kept, anything shaped as an
    e-mail address written as ‹address› (the router masked them; this is the second look), cut."""
    if not isinstance(value, str):
        raise Refused(f"{field}: not a string")
    kept = EMAIL.sub(ADDRESS, "".join(c for c in unicodedata.normalize("NFKC", value)
                                      if unicodedata.category(c) not in INVISIBLE or c == "\n"))
    return kept if len(kept) <= cap else kept[:cap - 1] + "…"


def _link(value: object) -> dict | None:
    """`{url, label}` of one of LINKS' shapes, with the label that goes with it; anything else is dropped — the sheet
    goes without it."""
    if not isinstance(value, dict) or set(value) != {"url", "label"} or not isinstance(value["label"], str) or \
            value["label"] not in LINKS or not isinstance(value["url"], str) or \
            not LINKS[value["label"]].fullmatch(value["url"]):
        return None
    return {"url": value["url"], "label": value["label"]}


def _time(value: object, field: str) -> float:
    if type(value) not in (int, float) or not 0 < value < 1e12 or not math.isfinite(value):
        raise Refused(f"{field}: not a time")
    return float(value)


def clean_detail(body: object) -> dict:
    """A row's sheet from the router (`waiting.detail`), checked as the digest is — its fields, their types and caps,
    a link only of a shape the router makes — before it goes to the app. Relayed, never kept."""
    if not isinstance(body, dict) or set(body) - {"kind", "who", "account", "asks", "more", "draft", "open"} \
            or not {"kind", "who", "asks"} <= set(body) or body["kind"] not in DETAIL_KINDS:
        raise Refused("detail: wrong fields")
    asks = body["asks"]
    if not isinstance(asks, list) or len(asks) > DETAIL_ASKS:
        raise Refused("asks: not a list")
    out_asks = []
    for n, ask in enumerate(asks):
        if not isinstance(ask, dict) or set(ask) - {"need", "text", "deadline", "at", "open"} or \
                not {"need", "text", "at"} <= set(ask) or ask["need"] not in DETAIL_NEEDS:
            raise Refused(f"asks[{n}]: wrong fields")
        out_asks.append({"need": ask["need"], "text": _words(ask["text"], DETAIL_JUDGED, f"asks[{n}].text"),
                         "deadline": line(_words(ask.get("deadline", ""), 4 * DETAIL_DEADLINE, f"asks[{n}].deadline"),
                                          DETAIL_DEADLINE),
                         "at": _time(ask["at"], f"asks[{n}].at"),
                         **({"open": link} if (link := _link(ask.get("open"))) else {})})
    more = body.get("more", 0)
    if type(more) is not int or not 0 <= more <= COUNT_MAX:
        raise Refused("more: not a number")
    out = {"kind": body["kind"], "who": _text(_words(body["who"], 4 * WHO_CHARS, "who"), WHO_CHARS),
           "account": line(body.get("account", "") if isinstance(body.get("account", ""), str) else "", DETAIL_ADDRESS),
           "asks": out_asks, "more": more}
    if link := _link(body.get("open")):
        out["open"] = link
    if "draft" in body:
        draft = body["draft"]
        if body["kind"] != "draft" or not isinstance(draft, dict) or \
                set(draft) != {"text", "channel", "subject", "expires", "state"} or \
                draft["channel"] not in ("mail", "telegram") or draft["state"] not in DRAFT_STATES:
            raise Refused("draft: wrong fields")
        out["draft"] = {"text": _words(draft["text"], DETAIL_DRAFT, "draft.text"), "channel": draft["channel"],
                        "subject": line(_words(draft["subject"], 4 * TITLE_CHARS, "draft.subject"), TITLE_CHARS),
                        "expires": _time(draft["expires"], "draft.expires"), "state": draft["state"]}
    return out


class Details:
    """The relay of a row's sheet. The app asks (`ask`: the row's id and token, queued in memory); the router's own
    long call (`asked`, `POST /owner/waiting/asked`) takes what was asked as soon as it is, and the router answers by
    the request's number (`answer`); the app's request, still waiting, gets it. The platform never calls the router, and
    nothing goes through the notes' journal: a request is not held back behind the cards, and none outlives the app's
    wait. What the router said is held only while that request waits — never written, never logged; an answer nobody
    waits for any more is dropped."""

    def __init__(self) -> None:
        self.waiting: dict[str, asyncio.Future] = {}
        self.queued: dict[str, dict] = {}  # asked, not yet taken by the router: number → {r, ref, t?}
        self._asked = asyncio.Event()
        self._call = 0  # the router's long calls, counted: only the newest takes what was asked

    def ask(self, ref: str, token: str | None) -> str | None:
        """A new request's number, queued for the router; None while DETAIL_PENDING already wait."""
        if len(self.waiting) >= DETAIL_PENDING:
            return None
        number = secrets.token_hex(8)
        self.waiting[number] = asyncio.get_running_loop().create_future()
        self.queued[number] = {"r": number, "ref": ref, **({"t": token} if token else {})}
        self._asked.set()
        return number

    async def asked(self, hold: float) -> list[dict]:
        """What the app asked and the router has not taken — at once, or as soon as something is, or nothing after
        `hold` seconds."""
        self._call += 1
        call = self._call
        if not self.queued:
            self._asked.clear()
            try:
                await asyncio.wait_for(self._asked.wait(), hold)
            except TimeoutError:
                return []
            if call != self._call:  # a newer call came — the router restarted while this one was held: it takes it
                self._asked.set()
                return []
        taken = list(self.queued.values())
        self.queued.clear()
        return taken

    def again(self, taken: list[dict]) -> None:
        """What was taken by a call whose router is gone (a restart while it was held): queued again for the next one,
        those still waited for."""
        for ask in taken:
            if ask.get("r") in self.waiting:
                self.queued[ask["r"]] = ask
        if self.queued:
            self._asked.set()

    async def wait(self, number: str, timeout: float = DETAIL_WAIT_S) -> tuple[str, dict | None]:
        """How it went and the sheet: `ok` with it; `gone` — the router does not know the row any more; `refused` —
        what it said was off the contract; `late` — it said nothing in time. However it ends — the app gone too — the
        request is forgotten."""
        try:
            detail = await asyncio.wait_for(self.waiting[number], timeout)
        except TimeoutError:
            return "late", None
        except Refused:
            return "refused", None
        finally:
            self.waiting.pop(number, None)
            self.queued.pop(number, None)
        return ("ok", detail) if detail is not None else ("gone", None)

    def answer(self, number: str, detail: dict | None | Refused) -> bool:
        """The router's answer to a request — the sheet, None, or why it was refused; False when no request of that
        number waits."""
        future = self.waiting.get(number)
        if future is None or future.done():
            return False
        if isinstance(detail, Refused):
            future.set_exception(detail)
        else:
            future.set_result(detail)
        return True


def clean_chips(body: object) -> list[dict]:
    """The hub's status chips beside the digest: `chips: [{key, text, level}]`, at most one of each key, or none. One
    off the contract refuses the whole push (`Refused`), as an item does."""
    chips = body.get("chips", []) if isinstance(body, dict) else []
    if not isinstance(chips, list) or len(chips) > len(CHIP_KEYS):
        raise Refused("chips: not a list")
    out, seen = [], set()
    for n, chip in enumerate(chips):
        if not isinstance(chip, dict) or set(chip) != {"key", "text", "level"} or chip["key"] not in CHIP_KEYS or \
                chip["key"] in seen or chip["level"] not in CHIP_LEVELS or not isinstance(chip["text"], str):
            raise Refused(f"chips[{n}]: invalid")
        seen.add(chip["key"])
        out.append({"key": chip["key"], "text": _text(chip["text"], CHIP_CHARS), "level": chip["level"]})
    return out


def _essence(text: object) -> str:
    """A row's one line: the first line of the text that says something, as words — no Markdown emphasis marks."""
    first = next((s for s in str(text or "").splitlines() if s.strip()), "")
    return line(re.sub(r"\*\*|__", "", first).strip(" #*>-"), ESSENCE_CHARS)


def _said(text: str) -> str:
    """A question's case as its session wrote it, Markdown kept: whole, or cut at a line's end before SAID_CHARS."""
    text = text.strip()
    if len(text) <= SAID_CHARS:
        return text
    cut = text[:SAID_CHARS]
    return cut[: cut.rfind("\n")].rstrip() + "\n\n…" if "\n" in cut else cut.rstrip() + "…"


def _ending_essence(ending: dict) -> str:
    """A turn ending's one line: what it asks of him or says, by its kind."""
    kind = ending["kind"]
    first = (ending.get("artifact") or (ending.get("artifacts") or [{}])[0]) if kind == "look" else {}
    said = {"ask": ending.get("context") or ending.get("why"), "approve": ending.get("will"),
            "look": first.get("title") or first.get("path") or first.get("url"), "step": ending.get("what"),
            "done": ending.get("what")}[kind]
    return _essence(said or ending.get("request"))


def _call(tool: str, tool_input: dict) -> str:
    """A card's call in one line: the command, the file, or the input."""
    for key in ("command", "file_path", "notebook_path", "url", "pattern", "path"):
        if isinstance(tool_input.get(key), str) and tool_input[key].strip():
            return line(f"{tool}: {tool_input[key]}" if key != "command" else tool_input[key], ESSENCE_CHARS)
    return line(f"{tool} {json.dumps(tool_input, ensure_ascii=False)}", ESSENCE_CHARS)


def _open_question(approval: dict) -> int:
    """The first question of a question card not answered yet."""
    answered = (approval.get("view") or {}).get("answers") or {}
    asked = questions.of(approval["input"])
    return next((n for n in range(len(asked)) if str(n) not in answered), 0)


def project_of(cwd: str | None, tree: str) -> str | None:
    """A folder of the Base as its project's path (`base/work/acme` → `work/acme`), relative to the lane's /work as a
    session's `cwd` is; the Base's root, a folder outside it or one under a dot-led folder: no project."""
    if not isinstance(cwd, str) or not cwd.strip("/"):
        return None
    path, root = posixpath.normpath(cwd.strip("/")), posixpath.normpath(tree.strip("/"))
    if not path.startswith(root + "/"):
        return None
    project = path[len(root) + 1:]
    return project if not any(part.startswith(".") for part in project.split("/")) else None


def spheres(cfg) -> dict[str, dict]:
    """The spheres the platform's config names, by name: their title and icon. None configured: none."""
    out = {}
    for sphere in getattr(cfg, "spheres", None) or ():
        get = sphere.get if isinstance(sphere, dict) else lambda key, s=sphere: getattr(s, key, None)
        if isinstance(get("name"), str):
            out[get("name")] = {"title": str(get("title") or get("name")), "icon": str(get("icon") or "")}
    return out


def _converge(db) -> None:
    """Step 1: what the upgrade at open did before the steps were numbered — the digest's chips and numbers."""
    columns = migrate.columns(db, "assistant_digest")
    if "chips" not in columns:
        db.execute("ALTER TABLE assistant_digest ADD COLUMN chips TEXT NOT NULL DEFAULT '[]'")
    if "counts" not in columns:
        db.execute("ALTER TABLE assistant_digest ADD COLUMN counts TEXT NOT NULL DEFAULT '{}'")


# The store's numbered migrations (`migrate`, docs/migrations.md): append a step, never change one released.
STEPS: tuple[migrate.Step, ...] = (_converge,)


class Feed:
    def __init__(self, store: PlatformStore, cfg=None) -> None:
        self.store, self.cfg = store, cfg
        store.project_of = self.session_project  # every session's row in the face names its project
        migrate.run(store.db, "feed", STEPS, "CREATE TABLE IF NOT EXISTS assistant_digest (id INTEGER PRIMARY KEY "
                    "CHECK (id = 1), items TEXT NOT NULL, bot TEXT NOT NULL DEFAULT '', at REAL NOT NULL)")

    # --- the assistant's digest ---------------------------------------------------------------------------------

    def keep(self, items: list[dict], bot: str, now: float, chips: list[dict] | None = None,
             counts: dict | None = None) -> None:
        """The digest as pushed, replacing the one before it whole — its chips and numbers too."""
        self.store.db.execute("INSERT OR REPLACE INTO assistant_digest (id, items, bot, at, chips, counts) VALUES "
                              "(1, ?, ?, ?, ?, ?)", (json.dumps(items, ensure_ascii=False), bot, now,
                                                     json.dumps(chips or [], ensure_ascii=False),
                                                     json.dumps(counts or {})))
        self.store.db.commit()

    def chips(self) -> list[dict]:
        row = self.store.db.execute("SELECT chips FROM assistant_digest WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else []

    def counts(self) -> dict:
        """The digest's two numbers as last pushed; 0 each before."""
        row = self.store.db.execute("SELECT counts FROM assistant_digest WHERE id = 1").fetchone()
        return {key: 0 for key in COUNTS} | (json.loads(row[0]) if row else {})

    def digest(self) -> tuple[list[dict], str, float | None]:
        row = self.store.db.execute("SELECT items, bot, at FROM assistant_digest WHERE id = 1").fetchone()
        return (json.loads(row[0]), row[1], row[2]) if row else ([], "", None)

    # --- the queue ------------------------------------------------------------------------------------------------

    def rows(self, now: float) -> list[dict]:
        """Every row, grouped by action in the order of ACTIONS: the cards and choices oldest first (a queue), what
        is to read newest first, the assistant's as she sent them."""
        db = self.store.db
        sessions = {s["id"]: s for s in self.store.face_sessions(now)}
        self._projects = self._session_projects(sessions)
        out: dict[str, list[dict]] = {action: [] for action, _ in ACTIONS}
        busy = set()
        cards = db.execute(
            "SELECT id FROM approvals a WHERE (a.kind IN ('approval', 'question') AND a.state = 'shown') OR "
            "(a.kind = 'courier' AND a.state = 'dropped' AND a.created >= ? AND NOT EXISTS (SELECT 1 FROM inputs i WHERE i.session = a.session"
            " AND i.at > a.created)) ORDER BY a.created, a.rowid", (now - COURIER_S,)).fetchall()
        for (approval_id,) in cards:
            approval = self.store.face_approval(approval_id)
            session = sessions.get(approval["session"]) if approval else None
            if session is None:
                continue
            question = approval["kind"] == "question"
            busy.add(session["id"])
            n = _open_question(approval) if question else 0
            essence = (line(questions.of(approval["input"])[n]["question"].strip().split("\n")[0], ESSENCE_CHARS)
                       if question and questions.of(approval["input"]) else _call(approval["tool"], approval["input"]))
            out["answer" if question else "decide"].append(
                {**self._row(f"card:{approval_id}", "answer" if question else "decide", approval["kind"], session,
                             approval["created"], essence),
                 "approval": approval, "card": questions.key(approval_id, n) if question else None,
                 **({"said": _said(self.store.said_before(session["id"], approval["tool_use_id"]))} if question else {})})
        topush = getattr(self, "topush", None)  # «Push»: commits no branch of the remote has yet
        for session in sessions.values():
            if session.get("push") and topush is not None and (repos := topush.unpushed(session["id"])):
                since = db.execute("SELECT MIN(at) FROM push_touched WHERE session = ?", (session["id"],)).fetchone()[0]
                named = ", ".join(f"{repo} · {n}" for repo, n in repos)
                out["decide"].append({**self._row(f"push:{session['id']}", "decide", "push", session,
                                                  since or session["at"], f"Push: {named}"),
                                      "push": sum(n for _, n in repos), "repos": [r for r, _ in repos],
                                      "hint": PUSH_HINT})
        subs = getattr(self, "subs", None)  # set by the platform: a sub-session a session proposed, not started
        for proposal in subs.waiting_proposals() if subs is not None else ():
            if (session := sessions.get(proposal["session"])) is not None:
                out["decide"].append({**self._row(f"proposal:{proposal['id']}", "decide", "proposal", session,
                                                  proposal["created"], f"Sub-session: {proposal['title']}"),
                                      "proposal": proposal, "hint": PROPOSAL_HINT})
        colltools = getattr(self, "colltools", None)  # set by the platform: a write to a collection an agent proposed
        for proposal in colltools.waiting() if colltools is not None else ():
            n = len(proposal["changes"])
            key, essence = f"cproposal:{proposal['id']}", (f"{proposal['collection']}: {n} change"
                                                           f"{'' if n == 1 else 's'} proposed")
            if (session := sessions.get(proposal["session"] or "")) is not None:
                row = self._row(key, "decide", "collection_proposal", session, proposal["created"], essence)
            elif proposal["session"] is None:  # the assistant's, a computer's or a rule's: no session to name it by
                row = {"key": key, "action": "decide", "source": "collection_proposal", "session": None,
                       "track": None, **self._sphere(None), "project": None, "title": _proposer(proposal["actor"]),
                       "since": proposal["created"], "essence": essence, "ref": proposal["id"]}
            else:
                continue
            out["decide"].append({**row, "collection_proposal": proposal, "hint": CPROPOSAL_HINT})
        endings = getattr(subs, "endings", None)  # what a turn left him (`endings`), current ones only
        ended = endings.current([s for s in sessions if s not in busy]) if endings is not None else {}
        said = {session: (seq, json.loads(data)) for session, seq, data in db.execute(
            "SELECT n.session, n.seq, n.data FROM notes n JOIN (SELECT MAX(seq) AS seq FROM notes WHERE kind = "
            "'summary' GROUP BY session) m ON n.seq = m.seq")}
        for session in sessions.values():
            if session["id"] in busy or session["state"] == "active":
                continue
            seq, data = said.get(session["id"], (None, {}))
            at = data.get("at") if isinstance(data.get("at"), (int, float)) else None
            if session["id"] in ended:
                ending, declared = ended[session["id"]]
                action = ENDING_ACTIONS[ending["kind"]]
                if action != "read" or (session.get("unread") is True and declared >= now - RECENT_S):
                    out[action].append({**self._row(f"ending:{session['id']}", action, "session", session, declared,
                                                    _ending_essence(ending) or _essence(data.get("text"))),
                                        "ending": ending})
            elif session["state"] == "waiting" and not session.get("waiting"):
                out["reply"].append(self._row(f"reply:{session['id']}/{seq}", "reply", "session", session,
                                              at or session["at"], _essence(data.get("text"))))
            elif session.get("unread") is True and (at or session["at"]) >= now - RECENT_S:
                out["read"].append(self._row(f"read:{session['id']}", "read", "session", session,
                                             at or session["at"], _essence(data.get("text"))))
        machines = getattr(self, "computers", None)  # set by the platform: the owner's computers' own sessions
        for machine in machines.listing(now) if machines is not None else ():
            if not machine["off"]:
                for row in self._computer_rows(machine["device"], machine["sessions"]):
                    out[row["action"]].append(row)
        for action in ("answer", "decide", "step", "reply"):
            out[action].sort(key=lambda r: r["since"])
        out["read"].sort(key=lambda r: -r["since"])
        items, _, _ = self.digest()
        out["assistant"] = [{"key": f"assistant:{i['id']}", "action": "assistant", "source": i["kind"],
                             "session": None, "track": None, **self._sphere(i.get("sphere")), "project": None,
                             "title": i["who"],
                             "since": i["since"], "essence": i["title"], "ref": i["id"],
                             **({"t": i["t"]} if i.get("t") else {})}
                            # a deadline days ahead waits under «Deadlines» only; a reminder is the day's, not a queue's
                            for i in items if i["since"] <= now and i["kind"] != "reminder"]
        return [row for action, _ in ACTIONS for row in out[action]]

    def _row(self, key: str, action: str, source: str, session: dict, since: float, essence: str) -> dict:
        return {"key": key, "action": action, "source": source, "session": session["id"], "track": session["track"],
                **self._sphere(session.get("sphere")), "project": getattr(self, "_projects", {}).get(session["id"]),
                "title": session["title"], "since": since, "essence": essence, "ref": session["id"]}

    def _computer_rows(self, device: str, sessions: list[dict]):
        """A computer's sessions as rows (`computers.py`): one that ended its turn with a word for him, by its ending;
        one that waits on him with none, «Reply». He answers it on its card (`answered`: his words, until the computer
        takes them); the row also says which computer and its link back."""
        machines = getattr(self, "computers", None)
        for s in sessions:
            if s["state"] == "working":
                continue
            ending = s.get("ending")
            if ending is None and s["state"] != "waiting":
                continue
            action = ENDING_ACTIONS[ending["kind"]] if ending else "reply"
            yield {"key": f"{'ending' if ending else 'reply'}:{device}/{s['id']}", "action": action,
                   "source": "computer", "session": None, "track": None, **self._sphere(None),
                   "project": s["project"] or None, "title": s["title"], "since": s["at"],
                   "essence": _ending_essence(ending) if ending else "", "ref": s["id"],
                   "computer": {"device": device, "id": s["id"], **({"link": s["link"]} if s.get("link") else {})},
                   **({"ending": ending} if ending else {}),
                   **({"answered": said} if machines and (said := machines.answered(device, s["id"])) else {})}

    def session_project(self, lane: str, track: str, cwd: str | None) -> str | None:
        """A session's project: its folder in the Base (`project_of`) — the folder it was made with, its track's for
        one older than that column; none off the Base's lane."""
        base = getattr(self.cfg, "base", None)
        if base is None or lane != base.lane:
            return None
        if cwd is None:
            known = next((t for t in getattr(self.cfg, "tracks", None) or () if t.name == track), None)
            cwd = known.cwd if known else None
        return project_of(cwd, base.tree)

    def _session_projects(self, sessions: dict[str, dict]) -> dict[str, str | None]:
        """Each session's project, as its row in the face says it (`session_project`, set on the store above)."""
        return {session["id"]: session.get("project") for session in sessions.values()}

    def _sphere(self, name: object) -> dict:
        known = spheres(self.cfg).get(name) if isinstance(name, str) else None
        return {"sphere": name if isinstance(name, str) else None,
                "sphere_title": known["title"] if known else (name if isinstance(name, str) else None),
                "sphere_icon": known["icon"] if known else None}

    def face(self, now: float) -> dict:
        """`GET /api/feed`: the rows by group, each card whole (the face decides it in place), the digest's age and
        the bot's name for «Open in Telegram»."""
        rows = self.rows(now)
        items, bot, at = self.digest()
        groups = [{"action": action, "title": title, "rows": [r for r in rows if r["action"] == action]}
                  for action, title in ACTIONS]
        deadlines = sorted(({"id": i["id"], "when": i["when"], "title": i["title"], **self._sphere(i.get("sphere")),
                             "path": i.get("path")} for i in items if i["kind"] == "deadline" and i.get("when")),
                           key=lambda d: d["when"])
        return {"count": len(rows), "groups": [g for g in groups if g["rows"]], "bot": bot, "assistant_at": at,
                "deadlines": deadlines, "chips": self.chips(), **self.counts()}

    def telegram(self, now: float) -> dict:
        """`POST /owner/feed`: the first TELEGRAM_ROWS rows for `/wait`, without the cards' inputs, and how many
        more there are."""
        rows = self.rows(now)
        titles = dict(ACTIONS)
        shown = [{"action": r["action"], "group": titles[r["action"]], "source": r["source"],
                  "session": r["session"], "approval": r["approval"]["id"] if r.get("approval") else None,
                  "card": r.get("card"), "where": r["sphere_title"] or r["track"] or "",
                  "title": r["title"], "since": r["since"], "essence": r["essence"],
                  **({"hint": r["hint"]} if r.get("hint") else {}),
                  **({"space": r["collection_proposal"]["space"]} if r.get("collection_proposal") else {})}
                 for r in rows[:TELEGRAM_ROWS]]
        return {"count": len(rows), "rows": shown, "more": max(0, len(rows) - TELEGRAM_ROWS)}


def _proposer(actor: str) -> str:
    """Who proposed a write with no session behind it, as a row's title: the assistant, a computer by its device's name,
    or a rule by its name."""
    if actor == "assistant":
        return "Assistant"
    if actor.startswith(("device:", "mac:")):  # mac: the old name of device: (#66)
        return f"Computer {actor.split(':', 1)[1].split('/')[0]}"
    return f"Rule {actor.removeprefix('code:')}"
