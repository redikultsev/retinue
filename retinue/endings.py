"""Turn endings: a session says, at the end of its turn, what it leaves the owner — a question, a permission, a thing to
look at, a step of his own, a result — so «Waiting for you» shows the right card without the owner opening the session.

A session the owner started calls `turn_ending` on the platform's own server (`subs`, through the gate like the other
tools there) with one object of a closed shape (`clean`). Off the shape, the call is refused with what to fix, and the
session, which sees the refusal, calls again: an unknown kind; an `ask` or `approve` without `request` or `why_owner`;
an `ask` with options and no `context`; a `look` without an artifact; a `step` without `what` and `where`; a `done`
without `what`. An artifact by `path` must be a file in the session's own folder that the app can serve: under `out/`
of the folder — handed over at the turn's end (`outfiles`) and served by `/api/files/<id>` — or, for a session of the
Base, a file of the Base, served by `/api/base/<path>`; anything else is refused when it is declared, so the session
learns it can not be shown while it can still put a copy where it can.

One ending per session, the last declared, stored with the run that declared it and the session's inputs at that
moment. It is the session's current one while no newer run and no newer input exist — the owner's message, a button
that resumes the run, a report, a follow-up: a new turn clears it with no write of its own. The feed (`feed.py`)
carries it on the session's row as `ending`, each artifact with the URL the app opens it by (`src`) and what it is
(`kind`).
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from typing import TYPE_CHECKING
from urllib.parse import quote

from . import migrate, outfiles
from .spaces import kind_of

if TYPE_CHECKING:
    from .platformd import Platform

TOOL = "turn_ending"
KINDS = ("ask", "approve", "look", "step", "done")
WHY_OWNER = ("goal", "fact", "money", "secret", "external")
NEXT_BY = ("me", "you")
TEXT_CHARS = 2000            # one field's words
LONG_CHARS = 8000            # a command or a text to copy
LIST_MAX = 10                # options, guesses, items, checks, artifacts
PATH_CHARS = 1000
URL = re.compile(r"https?://[^\s\x00-\x1f\x7f]{1,2000}")
TEXTS = ("request", "context", "why", "without", "will", "what", "where", "expect", "next")
LONGS = ("command", "copy")
LISTS = ("guesses", "items", "checks")
FIELDS = {"kind", "why_owner", "options", "artifact", "artifacts", "wait", "next_by", *TEXTS, *LONGS, *LISTS}

TOOLS = [{
    "name": TOOL,
    "description": "Declare what this turn leaves the owner, once, as the last thing before your summary: a question "
                   "(ask), a permission (approve), a thing to look at (look), a step he does himself (step), or a "
                   "result (done). He sees it as a card in his queue and answers from it without opening the "
                   "session. A turn that leaves him nothing needs none.",
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["kind"],
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "request": {"type": "string", "description": "what the owner asked and why, in his words; every kind "
                                                         "but step"},
            "why_owner": {"type": "string", "enum": list(WHY_OWNER),
                          "description": "ask, approve: why the decision is his — his goal or taste, his fact, "
                                         "money, a secret or root, an irreversible outside action"},
            "options": {"type": "array", "description": "ask: his choices, your advice first",
                        "items": {"type": "object", "additionalProperties": False, "required": ["label", "effect"],
                                  "properties": {"label": {"type": "string"},
                                                 "effect": {"type": "string", "description": "what changes"}}}},
            "context": {"type": "string", "description": "ask with options: what was found and what the advice "
                                                         "rests on"},
            "why": {"type": "string", "description": "ask without options: why you ask, what the answer changes"},
            "guesses": {"type": "array", "items": {"type": "string"}, "description": "ask without options: short "
                                                                                     "likely answers"},
            "without": {"type": "string", "description": "ask, approve: what happens with no answer"},
            "will": {"type": "string", "description": "approve: what you will do"},
            "items": {"type": "array", "items": {"type": "string"}, "description": "approve: what exactly, where"},
            "artifact": {"$ref": "#/$defs/artifact"},
            "artifacts": {"type": "array", "items": {"$ref": "#/$defs/artifact"}},
            "checks": {"type": "array", "items": {"type": "string"}, "description": "look: what to look at"},
            "what": {"type": "string", "description": "step: what he does; done: what came out"},
            "where": {"type": "string", "description": "step: where he does it"},
            "command": {"type": "string"}, "copy": {"type": "string"},
            "expect": {"type": "string", "description": "step: what to send back"},
            "wait": {"type": "boolean", "description": "step: you wait for it now"},
            "next": {"type": "string", "description": "done: what comes next"},
            "next_by": {"type": "string", "enum": list(NEXT_BY), "description": "done: whose move it is"}},
        "$defs": {"artifact": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string", "description": "a file in your folder: under out/, or in the "
                                                                     "Base"},
                           "url": {"type": "string", "description": "an http(s) page"},
                           "title": {"type": "string"}}}}}}]

INSTRUCTIONS = (f"Turn endings: when a turn leaves the owner something — a question, a permission, a thing to look at, "
                f"a step of his own, a result — call {TOOL} once, last, before your summary.")


def _text(value: object, cap: int) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= cap else None


def clean(arguments: object) -> dict | str:
    """The ending in its shape, or what to fix in it — one sentence the session acts on."""
    if not isinstance(arguments, dict):
        return "Give one object: {kind, …}."
    if unknown := set(arguments) - FIELDS:
        return f"Unknown field(s): {', '.join(sorted(map(str, unknown)))}. The fields are those of the tool's schema."
    kind = arguments.get("kind")
    if kind not in KINDS:
        return f"kind: one of {', '.join(KINDS)}."
    out: dict = {"kind": kind}
    for field, cap in [*((f, TEXT_CHARS) for f in TEXTS), *((f, LONG_CHARS) for f in LONGS)]:
        if field in arguments:
            if (value := _text(arguments[field], cap)) is None:
                return f"{field}: words, up to {cap} characters."
            out[field] = value
    for field in LISTS:
        if field in arguments:
            given = arguments[field]
            if not isinstance(given, list) or len(given) > LIST_MAX or \
                    any(_text(v, TEXT_CHARS) is None for v in given):
                return f"{field}: a list of up to {LIST_MAX} lines of words."
            out[field] = [v.strip() for v in given]
    if "options" in arguments:
        given = arguments["options"]
        if not isinstance(given, list) or not 1 <= len(given) <= LIST_MAX or any(
                not isinstance(o, dict) or set(o) != {"label", "effect"} or _text(o["label"], TEXT_CHARS) is None
                or _text(o["effect"], TEXT_CHARS) is None for o in given):
            return f"options: 1 to {LIST_MAX} of {{label, effect}} — effect is what changes if he picks it."
        out["options"] = [{"label": o["label"].strip(), "effect": o["effect"].strip()} for o in given]
    if "why_owner" in arguments:
        if arguments["why_owner"] not in WHY_OWNER:
            return f"why_owner: one of {', '.join(WHY_OWNER)}."
        out["why_owner"] = arguments["why_owner"]
    if "next_by" in arguments:
        if arguments["next_by"] not in NEXT_BY:
            return "next_by: me or you."
        out["next_by"] = arguments["next_by"]
    if "wait" in arguments:
        if not isinstance(arguments["wait"], bool):
            return "wait: true or false."
        out["wait"] = arguments["wait"]
    for field in ("artifact", "artifacts"):
        if field in arguments:
            given = [arguments[field]] if field == "artifact" else arguments[field]
            if not isinstance(given, list) or not 1 <= len(given) <= LIST_MAX:
                return f"artifacts: 1 to {LIST_MAX} of {{path or url, title}}."
            things = []
            for thing in given:
                if not isinstance(thing, dict) or set(thing) - {"path", "url", "title"} or \
                        len({"path", "url"} & set(thing)) != 1:
                    return f"{field}: {{path or url, title}} — exactly one of path and url."
                if "title" in thing and _text(thing["title"], TEXT_CHARS) is None:
                    return f"{field}: title, words."
                if "url" in thing and (not isinstance(thing["url"], str) or not URL.fullmatch(thing["url"])):
                    return f"{field}: url, an http(s) address."
                if "path" in thing and _text(thing["path"], PATH_CHARS) is None:
                    return f"{field}: path, a file in your folder."
                things.append({k: v.strip() for k, v in thing.items()})
            out[field] = things[0] if field == "artifact" else things
    return _refusal(out) or out


def _refusal(ending: dict) -> str | None:
    """What the kind needs and the ending lacks."""
    kind = ending["kind"]
    if kind != "step" and "request" not in ending:
        return (f"request: what the owner asked and why, in his words — a {kind} card says first what it answers "
                "(«You asked: …»).")
    if kind in ("ask", "approve") and "why_owner" not in ending:
        return (f"why_owner: why this decision is his — one of {', '.join(WHY_OWNER)}. If none fits, it is not his "
                "to make: decide it yourself and end with done.")
    if kind == "ask" and "options" in ending and "context" not in ending:
        return "context: what you found and what your advice rests on — he decides from the card alone."
    if kind == "look" and not artifacts(ending):
        return "artifact: the thing to look at — {path or url, title}."
    if kind == "step" and not ("what" in ending and "where" in ending):
        return "what and where: what the owner does himself, and where — the place, the app, the address."
    if kind == "done" and "what" not in ending:
        return "what: what came out, in a sentence or two."
    return None


def artifacts(ending: dict) -> list[dict]:
    return ([ending["artifact"]] if "artifact" in ending else []) + list(ending.get("artifacts") or [])


def _shown(folder: str) -> str:
    return posixpath.join("/work", folder) if folder else "/work"


def kind_of_mime(mime: str) -> str:
    """What the app does with a handed-over file of this type."""
    mime = mime.split(";")[0]
    if mime == "application/pdf":
        return "pdf"
    if mime in ("text/html", "text/markdown"):
        return mime.split("/")[1]
    if mime.startswith("image/"):
        return "image"
    return "text" if mime.startswith("text/") or mime == "application/json" else "other"


# The store's numbered migrations (`migrate`, docs/migrations.md): append a step, never change one released.
STEPS: tuple[migrate.Step, ...] = (
    """CREATE TABLE IF NOT EXISTS turn_endings (
        session TEXT PRIMARY KEY,           -- one per session: the last one declared
        run TEXT NOT NULL,                  -- the run that declared it: a newer run is a new turn
        inputs INTEGER NOT NULL,            -- the session's last input then (its rowid): a newer one is a new turn
        data TEXT NOT NULL,                 -- the ending, each artifact with where it is served from (`_where`)
        at REAL NOT NULL
    )""",
)


class Endings:
    def __init__(self, platform: Platform) -> None:
        self.platform, self.store = platform, platform.store
        migrate.run(self.store.db, "endings", STEPS)

    def declare(self, run: str, session: dict, arguments: dict) -> tuple[str, bool]:
        """The `turn_ending` call: checked, its files placed, stored for this turn — or refused with what to fix."""
        ending = clean(arguments)
        if isinstance(ending, str):
            return f"Not recorded — {ending}", True
        for thing in artifacts(ending):
            if "path" in thing:
                where = self._place(session, thing["path"])
                if isinstance(where, str):
                    return f"Not recorded — artifact {thing['path']!r}: {where}", True
                thing["_where"] = where
        db = self.store.db
        inputs = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM inputs WHERE session = ?",
                            (session["id"],)).fetchone()[0]
        db.execute("INSERT OR REPLACE INTO turn_endings (session, run, inputs, data, at) VALUES (?, ?, ?, ?, ?)",
                   (session["id"], run, inputs, json.dumps(ending, ensure_ascii=False), self.platform.clock()))
        db.commit()
        return ("Recorded: the owner sees it in his queue when this turn ends. A later call replaces it. Now end the "
                "turn with your summary."), False

    def _place(self, session: dict, path: str) -> dict | str:
        """Where the app serves a file the session names, or why it cannot: inside the session's folder, a file that
        is there now, under `out/` of a type it hands over, or in the Base for a session of the Base's lane."""
        track = self.platform.tracks.get(session.get("track") or "")
        cwd = session.get("cwd") if session.get("cwd") is not None else (track.cwd if track else "")
        folder = posixpath.normpath(cwd.strip("/")) if cwd.strip("/") else ""
        if path.startswith("/"):
            norm = posixpath.normpath(path)
            if norm != "/work" and not norm.startswith("/work/"):
                return f"outside your folder {_shown(folder)} — name a file in it."
            rel = norm[len("/work/"):] if norm != "/work" else ""
        else:
            rel = posixpath.normpath(posixpath.join(folder, path))
        inside = rel if not folder else rel[len(folder) + 1:] if rel.startswith(folder + "/") else None
        if inside is None or not inside or inside == "." or rel.split("/")[0] == "..":
            return f"outside your folder {_shown(folder)} — name a file in it."
        if any(part.startswith(".") for part in inside.split("/")):
            return "a hidden file or folder — the app shows none."
        lane = self.platform.cfg.lanes.get(session.get("lane") or "")
        if lane is None:
            return "this session's lane is gone."
        mount = os.path.realpath(lane.work_mount)
        real = os.path.realpath(os.path.join(mount, rel))
        home = os.path.realpath(os.path.join(mount, folder)) if folder else mount
        if real != home and not real.startswith(home + os.sep):
            return f"a link that leads outside your folder {_shown(folder)} — name the file itself."
        if not os.path.isfile(real):
            return "no such file — write it first, then declare it."
        if inside.startswith(outfiles.OUT + "/"):
            under = inside[len(outfiles.OUT) + 1:]
            if outfiles.mime_of(under) is None:
                return f"not a type the app takes from out/ — one of {' '.join(outfiles.TYPES)}."
            return {"in": "out", "lane": session["lane"], "cwd": cwd, "path": under}
        base = getattr(self.platform.cfg, "base", None)
        tree = base.tree.strip("/") if base is not None else ""
        if base is not None and session.get("lane") == base.lane and rel.startswith(tree + "/"):
            return {"in": "base", "path": rel[len(tree) + 1:]}
        return (f"the app shows a file only from out/ of your folder — save a copy as "
                f"{_shown(posixpath.join(folder, outfiles.OUT))}/{posixpath.basename(inside)} and declare that.")

    def current(self, sessions: list[str]) -> dict[str, tuple[dict, float]]:
        """The sessions' endings still current — declared in their latest run, no input since — as the app shows them,
        with when each was declared."""
        if not sessions:
            return {}
        rows = self.store.db.execute(
            "SELECT e.session, e.data, e.at FROM turn_endings e WHERE e.session IN "
            f"({','.join('?' * len(sessions))}) "
            "AND e.run = (SELECT r.id FROM runs r WHERE r.session = e.session ORDER BY r.rowid DESC LIMIT 1) "
            "AND e.inputs = (SELECT COALESCE(MAX(i.rowid), 0) FROM inputs i WHERE i.session = e.session)",
            sessions).fetchall()
        return {session: (self._face(json.loads(data)), at) for session, data, at in rows}

    def _face(self, ending: dict) -> dict:
        for field in ("artifact", "artifacts"):
            if field in ending:
                shown = [self._served(thing) for thing in ([ending[field]] if field == "artifact" else ending[field])]
                ending[field] = shown[0] if field == "artifact" else shown
        return ending

    def _served(self, thing: dict) -> dict:
        """An artifact as the app opens it: `src` — the URL it opens in place, null with `why` while there is none —
        and `kind`: image, pdf, html, markdown, text, audio, video, other; `link` for a page by its address."""
        where = thing.pop("_where", None)
        if "url" in thing:
            return {**thing, "src": thing["url"], "kind": "link"}
        if where is None:
            return {**thing, "src": None, "kind": "other", "why": "Not served."}
        if where["in"] == "base":
            kind = kind_of(where["path"])
            route = "/api/base-view/" if kind == "pdf" else "/api/base/"
            return {**thing, "src": route + quote(where["path"]), "kind": kind}
        row = self.store.db.execute("SELECT id, mime FROM out_files WHERE lane = ? AND cwd = ? AND path = ? "
                                    "ORDER BY at DESC, rowid DESC LIMIT 1",
                                    (where["lane"], where["cwd"], where["path"])).fetchone()
        kind = kind_of_mime(outfiles.mime_of(where["path"]) or "")
        if row is None:
            return {**thing, "src": None, "kind": kind,
                    "why": "Not collected from out/ yet — it comes when the turn ends; the session's Files say why "
                           "if it was left out."}
        return {**thing, "src": f"/api/files/{row[0]}" + ("/view" if kind in ("pdf", "html") else ""), "kind": kind}
