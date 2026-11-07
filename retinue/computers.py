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
"""

from __future__ import annotations

import re
import unicodedata

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


class Refused(Exception):
    """A report the platform does not take, with why; the list before it stays."""


def _line(value: object, cap: int) -> str:
    """A text as a row shows it: one line, no control character, at most `cap`."""
    if not isinstance(value, str):
        return ""
    text = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in value)
    return " ".join(text.split())[:cap]


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
    return {"id": sid, "title": _line(raw.get("title"), TITLE_MAX), "folder": _line(raw.get("folder"), FOLDER_MAX),
            "project": project.strip("/"), "state": state, "at": float(at), **({"link": link} if link else {})}


class Computers:
    """The devices' last reports, in memory."""

    def __init__(self) -> None:
        self._last: dict[str, dict] = {}  # device name -> {"at": when reported, "sessions": [rows]}

    def report(self, device: str, params: object, now: float) -> int:
        """A device's whole list, replacing the one before; the number of rows taken. Refused: the list before stays."""
        sessions = params.get("sessions") if isinstance(params, dict) else None
        if not isinstance(sessions, list) or len(sessions) > SESSIONS_MAX:
            raise Refused(NOT_A_LIST)
        rows = [_row(raw, n) for n, raw in enumerate(sessions)]
        if len({r["id"] for r in rows}) != len(rows):
            raise Refused("Two sessions with one id: each once.")
        self._last[device] = {"at": now, "sessions": rows}
        return len(rows)

    def forget(self, device: str) -> None:
        """A revoked device: its rows go at once."""
        self._last.pop(device, None)

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
