"""The owner's one queue «Waiting for you»: what waits for him across the sessions and the assistant's world, grouped by what
he does about it, not by where it came from. The face shows it on its home screen (`GET /api/feed`),
the router as text for `/wait` (`POST /owner/feed`).

Built by code on every read from the platform's own rows — never a copy kept beside them that could drift:

- «Answer the question»: a question card (AskUserQuestion) shown and not answered;
- «Decide»: an approval card shown, a courier card of the last day whose session he has not written to since, and a
  session with Base files not committed yet («To the Base», `tobase`);
- «Choose»: a ping (`pings`) whose suggested replies are still open — none pressed, no message of his after it, no
  newer ping; or a session waiting with no card shown;
- «Read»: a session whose finished turn he has not read (the session's `unread`; nothing without it);
- «From the assistant»: the assistant's digest.

The digest is the one thing kept. The router knows what waits in her world — a letter or a chat she judged to ask
something of him, a draft waiting for «Send», a deadline of the base — and pushes it here (`POST /owner/waiting`)
whole each time it changes and at least hourly; the platform never calls the router. It is minimal by contract:
an id, a kind, a display name, a one-line title, a time, a sphere — no text of a letter, no address; a deadline also
its day and its Record's path. What the face does with it is one link to the bot's chat: sending and deciding stay
in Telegram, where an outward action is a button under its card. A deadline whose time has not come yet (`since`
ahead) is no one's queue: the face shows it only under «Deadlines». The same push carries the hub's status chips
(`chips`: the backup, the limit, the hub's build), for the face's «Today».
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import date
from typing import TYPE_CHECKING

from . import questions
from .courier import line
from .tobase import files_word

if TYPE_CHECKING:
    from .sessions import PlatformStore

ACTIONS = (("answer", "Answer the question"), ("decide", "Decide"), ("choose", "Choose"), ("read", "Read"),
           ("assistant", "From the assistant"))
COURIER_S = 86400            # a courier card waits in «Decide» this long, unless he wrote into its session after it
RECENT_S = 7 * 86400         # a summary older than this leaves no choice and nothing to read in the feed
ESSENCE_CHARS = 200          # the one line a row says
REPLIES, REPLY_CHARS = 3, 80  # suggested replies shown under a «Choose» row
TELEGRAM_ROWS = 10           # `/wait` names this many rows; the rest are counted
BASE_HINT = "Press «To the Base» under its result or open it in the app."
NOBODY_HINT = "They are on the «To the Base» card of any Base session, turned off: turn on the ones you want and commit."
PUSH_HINT = "Press «Push» under its result or open it in the app."

# The digest: what the router may push, and nothing more.
DIGEST_KINDS = ("letter", "chat", "draft", "deadline")
DIGEST_KEYS = {"id", "kind", "who", "title", "since", "sphere"}
DEADLINE_KEYS = {"when", "path"}  # a deadline's own, both optional: its day and its Record's path in the base
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
        allowed = DIGEST_KEYS | DEADLINE_KEYS if isinstance(item, dict) and item.get("kind") == "deadline" \
            else DIGEST_KEYS
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
        seen.add(item["id"])
        items.append({"id": item["id"], "kind": item["kind"], "who": _text(item["who"], WHO_CHARS),
                      "title": _text(item["title"], TITLE_CHARS), "since": float(since), "sphere": sphere,
                      **_deadline(item, n)})
    return items, bot


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
    """A row's one line: the first line of the text that says something."""
    first = next((s for s in str(text or "").splitlines() if s.strip()), "")
    return line(first.strip(" #*>-"), ESSENCE_CHARS)


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


def _replies(data: dict) -> list[str]:
    """A ping's suggested replies while they are open (`pings.add` parsed them; `pings._close` closes them), in their
    own order — a press names a reply by its place (`{reply: {seq, n}}`). Closed, or not shaped so: none."""
    given = data.get("replies")
    if data.get("open") is not True or not isinstance(given, list) or not all(isinstance(r, str) for r in given):
        return []
    return [line(r, REPLY_CHARS) for r in given][:REPLIES]


def spheres(cfg) -> dict[str, dict]:
    """The spheres the platform's config names, by name: their title and icon. None configured: none."""
    out = {}
    for sphere in getattr(cfg, "spheres", None) or ():
        get = sphere.get if isinstance(sphere, dict) else lambda key, s=sphere: getattr(s, key, None)
        if isinstance(get("name"), str):
            out[get("name")] = {"title": str(get("title") or get("name")), "icon": str(get("icon") or "")}
    return out


class Feed:
    def __init__(self, store: PlatformStore, cfg=None) -> None:
        self.store, self.cfg = store, cfg
        store.db.execute("CREATE TABLE IF NOT EXISTS assistant_digest (id INTEGER PRIMARY KEY CHECK (id = 1), "
                         "items TEXT NOT NULL, bot TEXT NOT NULL DEFAULT '', at REAL NOT NULL)")
        if "chips" not in {row[1] for row in store.db.execute("PRAGMA table_info(assistant_digest)")}:
            store.db.execute("ALTER TABLE assistant_digest ADD COLUMN chips TEXT NOT NULL DEFAULT '[]'")
        store.db.commit()

    # --- the assistant's digest ---------------------------------------------------------------------------------

    def keep(self, items: list[dict], bot: str, now: float, chips: list[dict] | None = None) -> None:
        """The digest as pushed, replacing the one before it whole — its chips too."""
        self.store.db.execute("INSERT OR REPLACE INTO assistant_digest (id, items, bot, at, chips) VALUES "
                              "(1, ?, ?, ?, ?)", (json.dumps(items, ensure_ascii=False), bot, now,
                                                  json.dumps(chips or [], ensure_ascii=False)))
        self.store.db.commit()

    def chips(self) -> list[dict]:
        row = self.store.db.execute("SELECT chips FROM assistant_digest WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else []

    def digest(self) -> tuple[list[dict], str, float | None]:
        row = self.store.db.execute("SELECT items, bot, at FROM assistant_digest WHERE id = 1").fetchone()
        return (json.loads(row[0]), row[1], row[2]) if row else ([], "", None)

    # --- the queue ------------------------------------------------------------------------------------------------

    def rows(self, now: float) -> list[dict]:
        """Every row, grouped by action in the order of ACTIONS: the cards and choices oldest first (a queue), what
        is to read newest first, the assistant's as she sent them."""
        db = self.store.db
        sessions = {s["id"]: s for s in self.store.face_sessions(now)}
        out: dict[str, list[dict]] = {action: [] for action, _ in ACTIONS}
        busy = set()
        cards = db.execute(
            "SELECT id FROM approvals a WHERE (a.kind IN ('approval', 'question') AND a.state = 'shown') OR "
            "(a.kind = 'courier' AND a.created >= ? AND NOT EXISTS (SELECT 1 FROM inputs i WHERE i.session = a.session"
            " AND i.at > a.created)) ORDER BY a.created, a.rowid", (now - COURIER_S,)).fetchall()
        for (approval_id,) in cards:
            approval = self.store.face_approval(approval_id)
            session = sessions.get(approval["session"]) if approval else None
            if session is None:
                continue
            question = approval["kind"] == "question"
            busy.add(session["id"])
            n = _open_question(approval) if question else 0
            essence = (line(questions.of(approval["input"])[n]["question"], ESSENCE_CHARS)
                       if question and questions.of(approval["input"]) else _call(approval["tool"], approval["input"]))
            out["answer" if question else "decide"].append(
                {**self._row(f"card:{approval_id}", "answer" if question else "decide", approval["kind"], session,
                             approval["created"], essence),
                 "approval": approval, "card": questions.key(approval_id, n) if question else None})
        for session in sessions.values():  # «To the Base»: its files wait for the owner's commit, since the first of them
            if session.get("base"):
                since = db.execute("SELECT MIN(at) FROM base_files WHERE session = ?", (session["id"],)).fetchone()[0]
                out["decide"].append({**self._row(f"base:{session['id']}", "decide", "base", session,
                                                  since or session["at"], f"To the Base: {files_word(session['base'])}"),
                                      "base": session["base"], "hint": BASE_HINT})
        topush = getattr(self, "topush", None)  # «Push»: commits no branch of the remote has yet
        for session in sessions.values():
            if session.get("push") and topush is not None and (repos := topush.unpushed(session["id"])):
                since = db.execute("SELECT MIN(at) FROM push_touched WHERE session = ?", (session["id"],)).fetchone()[0]
                named = ", ".join(f"{repo} · {n}" for repo, n in repos)
                out["decide"].append({**self._row(f"push:{session['id']}", "decide", "push", session,
                                                  since or session["at"], f"Push: {named}"),
                                      "push": sum(n for _, n in repos), "repos": [r for r, _ in repos],
                                      "hint": PUSH_HINT})
        tobase = getattr(self, "tobase", None)  # set by the platform; nobody's files are never left unseen
        if tobase is not None and (nobodys := tobase.nobodys()) and \
                (carrier := sessions.get(tobase.carrier() or "")) is not None:
            out["decide"].append({**self._row("base:nobody", "decide", "base", carrier, now,
                                              f"Nobody's Base files: {files_word(len(nobodys))} — changed, but by no "
                                              "session"), "title": "Nobody's Base files", "base": len(nobodys),
                                  "hint": NOBODY_HINT})
        said = {session: (seq, json.loads(data)) for session, seq, data in db.execute(
            "SELECT n.session, n.seq, n.data FROM notes n JOIN (SELECT MAX(seq) AS seq FROM notes WHERE kind = "
            "'summary' GROUP BY session) m ON n.seq = m.seq")}
        for session in sessions.values():
            if session["id"] in busy or session["state"] == "active":
                continue
            seq, data = said.get(session["id"], (None, {}))
            at = data.get("at") if isinstance(data.get("at"), (int, float)) else None
            fresh = at is not None and at >= now - RECENT_S  # his message after it closed the ping (`pings`)
            replies = _replies(data) if fresh else []
            if (fresh and replies) or \
                    (session["state"] == "waiting" and not session.get("waiting")):
                out["choose"].append({**self._row(f"choose:{session['id']}/{seq}", "choose", "session", session,
                                                  at or session["at"], _essence(data.get("text"))),
                                      "replies": replies, "summary": seq})
            elif session.get("unread") is True and (at or session["at"]) >= now - RECENT_S:
                out["read"].append(self._row(f"read:{session['id']}", "read", "session", session,
                                             at or session["at"], _essence(data.get("text"))))
        for action in ("answer", "decide", "choose"):
            out[action].sort(key=lambda r: r["since"])
        out["read"].sort(key=lambda r: -r["since"])
        items, _, _ = self.digest()
        out["assistant"] = [{"key": f"assistant:{i['id']}", "action": "assistant", "source": i["kind"],
                             "session": None, "track": None, **self._sphere(i.get("sphere")), "title": i["who"],
                             "since": i["since"], "essence": i["title"], "ref": i["id"]}
                            for i in items if i["since"] <= now]  # a deadline days ahead waits under «Deadlines» only
        return [row for action, _ in ACTIONS for row in out[action]]

    def _row(self, key: str, action: str, source: str, session: dict, since: float, essence: str) -> dict:
        return {"key": key, "action": action, "source": source, "session": session["id"], "track": session["track"],
                **self._sphere(session.get("sphere")), "title": session["title"], "since": since, "essence": essence,
                "ref": session["id"]}

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
                "deadlines": deadlines, "chips": self.chips()}

    def telegram(self, now: float) -> dict:
        """`POST /owner/feed`: the first TELEGRAM_ROWS rows for `/wait`, without the cards' inputs, and how many
        more there are."""
        rows = self.rows(now)
        titles = dict(ACTIONS)
        shown = [{"action": r["action"], "group": titles[r["action"]], "source": r["source"],
                  "session": r["session"], "approval": r["approval"]["id"] if r.get("approval") else None,
                  "card": r.get("card"), "where": r["sphere_title"] or r["track"] or "", "title": r["title"],
                  "since": r["since"], "essence": r["essence"], **({"hint": r["hint"]} if r.get("hint") else {})}
                 for r in rows[:TELEGRAM_ROWS]]
        return {"count": len(rows), "rows": shown, "more": max(0, len(rows) - TELEGRAM_ROWS)}
