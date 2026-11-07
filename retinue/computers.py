"""The owner's computers' own sessions in the app (docs/collections.md, «Its sessions»): each device with a token reports
the Claude Code sessions it runs — id, title, folder, the Base folder they belong to, state, last activity — and the
app lists them under their projects beside the room's. Read-only on this side: the app shows them; it neither starts
nor steers them.

A report is the device's whole list and replaces the one before. It is kept in memory only, per device: nothing on
disk, nothing in a log — titles and folders are the owner's —, and a restart forgets it until the next report, which
comes on each change and every `BEAT_S` at the latest. A device whose last report is older than `OFF_S` is off: its
rows stay, greyed, and count neither as waiting nor as working.

Placement is the device's: the platform does not know a computer's paths, so each session comes with the Base folder
(`project`) the computer maps its folder to; the app puts it under the nearest project of that folder, else «Other».

A session that is not at work may carry its current turn ending (`ending`), in the shape of a room session's
`turn_ending` (`endings.clean`): the feed shows it as that session's card (`feed.py`). An artifact named by `path` is a
file on the computer, which the app cannot serve: it shows its name, «on your computer». An ending off the shape is left
out — the session stays in the list — and the answer names it (`left_out`), so the computer can say why.

Each session has a place on the operator's map (`slot`): the lowest free in its project when it first shows, kept while
its computer lists it — as a room session's (`sessions.take_slot`), but in memory, as the rest of a report.

The owner answers a computer's session where he reads it, in the app (`answer`): his words wait here, in memory too, one
answer per session — a newer one replaces it —, for a session that is listed and not at work. The reply to that
computer's next report hands them over (`answers`), each with an id, again on every report until the computer names it
among `delivered` — it put the words into its session — or the session leaves its list.
"""

from __future__ import annotations

import posixpath
import re
import secrets
import unicodedata

from . import endings

BEAT_S = 30            # a device reports at least this often (`retinue/sessions` on each change, else this)
OFF_S = 120            # no report for this long: the computer is off
SESSIONS_MAX = 500     # rows in one report
TITLE_MAX = 200
FOLDER_MAX = 512
STATES = ("working", "waiting", "done")
SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
BASE_FOLDER = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63})(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}){0,7}")
LINK = re.compile(r"claude://claude\.ai/[A-Za-z0-9/_.-]{1,200}")  # the one link a device may give: back to the session
NOT_A_LIST = "A report is {\"sessions\": [ … ]}: the device's whole list, at most %d." % SESSIONS_MAX
ANSWER_MAX = 4000      # the owner's answer to a computer's session: one message
NOT_DELIVERED = "delivered: the ids of the answers put into their sessions, each a string the platform gave."


class Refused(Exception):
    """A report the platform does not take, with why; the list before it stays."""


class NotListed(Refused):
    """An answer for a session its computer does not list."""


def _line(value: object, cap: int) -> str:
    """A text as a row shows it: one line, no control character, at most `cap`."""
    if not isinstance(value, str):
        return ""
    text = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in value)
    return " ".join(text.split())[:cap]


def _thing(thing: dict) -> dict:
    """An artifact as the app opens it: a page by its address; a file of the computer by its name only."""
    if "url" in thing:
        return {**thing, "src": thing["url"], "kind": "link"}
    name = thing.get("title") or posixpath.basename(thing["path"].replace("\\", "/")) or "file"
    return {"title": name, "src": None, "kind": "other", "why": "On your computer.", "on_computer": True}


def _ending(raw: object) -> dict | str:
    """A session's turn ending as the feed shows it, or why it is left out."""
    ending = endings.clean(raw)
    if isinstance(ending, str):
        return ending
    if "artifact" in ending:
        ending["artifact"] = _thing(ending["artifact"])
    if "artifacts" in ending:
        ending["artifacts"] = [_thing(t) for t in ending["artifacts"]]
    return ending


def _row(raw: object, n: int) -> dict:
    if not isinstance(raw, dict):
        raise Refused(f"sessions[{n}] is not an object.")
    sid, state, at = raw.get("id"), raw.get("state"), raw.get("at")
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise Refused(f"sessions[{n}].id: letters, digits, _ . : - — at most 128.")
    if state not in STATES:
        raise Refused(f"sessions[{n}].state: one of {', '.join(STATES)}.")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or at < 0:
        raise Refused(f"sessions[{n}].at: its last activity, in seconds since the epoch.")
    project = raw.get("project") or ""
    if not isinstance(project, str) or (project and not BASE_FOLDER.fullmatch(project.strip("/"))):
        raise Refused(f"sessions[{n}].project: a Base folder (`work/acme`), or none.")
    link = raw.get("link") or ""
    if not isinstance(link, str) or (link and not LINK.fullmatch(link)):
        raise Refused(f"sessions[{n}].link: claude://claude.ai/…, or none.")
    row = {"id": sid, "title": _line(raw.get("title"), TITLE_MAX), "folder": _line(raw.get("folder"), FOLDER_MAX),
           "project": project.strip("/"), "state": state, "at": float(at), **({"link": link} if link else {})}
    if raw.get("ending") is not None and state != "working":
        ending = _ending(raw["ending"])
        row["ending" if isinstance(ending, dict) else "left_out"] = ending
    return row


class Computers:
    """The devices' last reports, in memory."""

    def __init__(self) -> None:
        self._last: dict[str, dict] = {}  # device name -> {"at": when reported, "sessions": [rows]}
        self._answers: dict[str, dict[str, dict]] = {}  # device name -> session id -> the owner's answer waiting

    def report(self, device: str, params: object, now: float) -> tuple[int, list[dict]]:
        """A device's whole list, replacing the one before: the number of rows taken, and the endings left out with
        why. Refused: the list before stays. The answers it names `delivered` go, and those of sessions it no longer
        lists."""
        sessions = params.get("sessions") if isinstance(params, dict) else None
        if not isinstance(sessions, list) or len(sessions) > SESSIONS_MAX:
            raise Refused(NOT_A_LIST)
        delivered = params.get("delivered", [])
        if not isinstance(delivered, list) or len(delivered) > SESSIONS_MAX or \
                not all(isinstance(d, str) for d in delivered):
            raise Refused(NOT_DELIVERED)
        rows = [_row(raw, n) for n, raw in enumerate(sessions)]
        if len({r["id"] for r in rows}) != len(rows):
            raise Refused("Two sessions with one id: each once.")
        listed, done = {r["id"] for r in rows}, set(delivered)
        waiting = {sid: a for sid, a in self._answers.get(device, {}).items() if sid in listed and a["id"] not in done}
        if waiting:
            self._answers[device] = waiting
        else:
            self._answers.pop(device, None)
        left_out = [{"id": r["id"], "why": r.pop("left_out")} for r in rows if "left_out" in r]
        before = {r["id"]: r for r in (self._last.get(device) or {}).get("sessions", ())}
        taken: dict[str, set[int]] = {}
        for r in rows:  # a session listed before keeps its place
            if (old := before.get(r["id"])) is not None and old["project"] == r["project"]:
                r["slot"] = old["slot"]
                taken.setdefault(r["project"], set()).add(r["slot"])
        for r in rows:  # a new one, or one moved to another project: the lowest place free there
            if "slot" not in r:
                used = taken.setdefault(r["project"], set())
                r["slot"] = next(n for n in range(len(used) + 1) if n not in used)
                used.add(r["slot"])
        self._last[device] = {"at": now, "sessions": rows}
        return len(rows), left_out

    def forget(self, device: str) -> None:
        """A revoked device: its rows go at once, and the answers waiting for it."""
        self._last.pop(device, None)
        self._answers.pop(device, None)

    def answer(self, device: str, session: str, text: object, now: float) -> dict:
        """The owner's words for a session its computer lists and that is not at work, kept until the computer takes
        them; a newer answer to that session replaces the one before."""
        words = text.strip() if isinstance(text, str) else ""
        if not words or len(words) > ANSWER_MAX:
            raise Refused(f"text: the answer, at most {ANSWER_MAX} characters.")
        row = next((r for r in (self._last.get(device) or {}).get("sessions", ()) if r["id"] == session), None)
        if row is None:
            raise NotListed("That computer does not list this session now.")
        if row["state"] == "working":
            raise Refused("The session is at work: answer it when its turn ends.")
        given = {"id": secrets.token_hex(8), "session": session, "text": words, "at": now}
        self._answers.setdefault(device, {})[session] = given
        return dict(given)

    def answers(self, device: str) -> list[dict]:
        """The owner's answers waiting for a computer: what the reply to its report hands over."""
        return [dict(a) for a in self._answers.get(device, {}).values()]

    def answered(self, device: str, session: str) -> str | None:
        """The words waiting for one session, if any: its card says them instead of asking again."""
        given = self._answers.get(device, {}).get(session)
        return given["text"] if given else None

    def listing(self, now: float, live: set[str] | None = None) -> list[dict]:
        """For the app: every device that reported, by name, with `off` when its last report is older than `OFF_S`.
        `live`: the device names whose token still opens; a revoked one's rows are left out."""
        out = []
        for name in sorted(self._last):
            if live is not None and name not in live:
                continue
            last = self._last[name]
            out.append({"device": name, "at": last["at"], "off": now - last["at"] > OFF_S,
                        "sessions": [dict(r) for r in last["sessions"]]})
        return out
