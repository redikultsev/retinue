"""Pings: the end of a turn as a message the owner can answer without opening the session.

Every summary note (`Platform._summary`) is a ping: a kind — `done`, `decision` (a question or an approval card of
the session waits on him) or `failed` (an error, the subscription's limit, a turn a restart cut, a run its lane
refused) — how long the turn took (`took_s`), and where it belongs (the session's sphere or track) and its title.
A session that needs the owner's choice asks with a question card (AskUserQuestion, `questions`), never with
buttons under its summary; a reply to the ping's message goes into the session.

**Loudness**: a `done` ping less than QUIET_S after the owner last acted on the session goes out silently (Telegram's
`disable_notification`) — he is looking — when that run was his own doing; a longer one, a `decision`, a `failed`
one, and one of a run the platform started by itself (`unattended`, `started`) notify (`loud`).

**Where it is read**: a session the owner drives from the app — his last message into it was written there — is
read in the app (`in_app`). Telegram does not get its turns again: only one that took AWAY_S or longer, when he may
have left the app, and then as its header line with the app's button.
"""

from __future__ import annotations


from .courier import line
from .sessions import PlatformStore

DONE, DECISION, FAILED = "done", "decision", "failed"
ICONS = {DONE: "✅", DECISION: "❓", FAILED: "⚠️"}
TITLE_CHARS = 40             # the session's title in a ping's header
QUIET_S = 120                # a done turn this soon after the owner's own input or press: no sound
AWAY_S = 300                 # a turn of a session he drives from the app: this long, and Telegram hears of it
APP = "face:"                # an input written in the app


def loud(data: dict) -> bool:
    """A ping notifies unless it is a done turn of a run the owner started himself, shorter than QUIET_S, or a done
    turn that was not his to watch (`quiet`: a sub-session's on its parent's word, a parent's on a report — `subs`)."""
    if data.get("kind") == DONE and data.get("quiet") is True:
        return False
    return data.get("kind") != DONE or data.get("attended") is not True or \
        not isinstance(data.get("took_s"), (int, float)) or data["took_s"] >= QUIET_S


def took_words(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60} min" if s % 3600 >= 60 else f"{s // 3600} h"


def header(data: dict) -> str:
    """`<icon> <sphere or track> · <title> · <took>`, plain text: the bridge escapes it."""
    parts = [str(data.get("place") or ""), str(data.get("title") or ""), took_words(data.get("took_s"))]
    return f"{ICONS.get(str(data.get('kind')), ICONS[DONE])} " + " · ".join(p for p in parts if p)


def took(store: PlatformStore, session: str, run: str | None, now: float) -> int:
    """Seconds since the owner last acted on the session: his last message, or the start of the run that ends — a
    run starts on his message or on his decision on a card (a run that waited for room on its lane counts from its
    start)."""
    marks = [store.last_input_at(session)]
    if run and (row := store.db.execute("SELECT started FROM runs WHERE id = ?", (run,)).fetchone()):
        marks.append(row[0])
    since = max((m for m in marks if m is not None), default=now)
    return max(0, round(now - since))


def prepare(store: PlatformStore) -> None:
    """The runs no owner's action started: a run that continues a turn a restart cut, a resume the gate's own shutdown
    left owed. Their ping is never silent."""
    store.db.execute("CREATE TABLE IF NOT EXISTS ping_unattended (key TEXT PRIMARY KEY)")  # q<queue seq> | r<run>
    store.db.commit()


def unattended(store: PlatformStore, session: str) -> None:
    """The session's newest queued entry was not the owner's doing: called right after it is queued."""
    if (seq := store.db.execute("SELECT MAX(seq) FROM queue WHERE session = ?", (session,)).fetchone()[0]) is not None:
        store.db.execute("INSERT OR IGNORE INTO ping_unattended VALUES (?)", (f"q{seq}",))
        store.db.commit()


def started(store: PlatformStore, run: str, seq: int, by_owner: bool) -> None:
    """A run started for queued entry `seq`: unattended when the entry was, or when the caller says so."""
    if store.db.execute("DELETE FROM ping_unattended WHERE key = ?", (f"q{seq}",)).rowcount or not by_owner:
        store.db.execute("INSERT OR IGNORE INTO ping_unattended VALUES (?)", (f"r{run}",))
    store.db.commit()


def attended(store: PlatformStore, run: str | None) -> bool:
    return run is not None and store.db.execute("SELECT 1 FROM ping_unattended WHERE key = ?",
                                                (f"r{run}",)).fetchone() is None


def in_app(store: PlatformStore, session: str) -> bool:
    """The owner drives the session from the app: his last message into it was written there."""
    row = store.db.execute("SELECT input_id FROM inputs WHERE session = ? AND by IS NULL ORDER BY at DESC, rowid DESC "
                           "LIMIT 1", (session,)).fetchone()
    return row is not None and row[0].startswith(APP)


def add(store: PlatformStore, session: dict, head: str, text: str, kind: str, now: float, *, run: str | None = None,
        spheres: dict[str, str] | None = None, quiet: bool = False, line_before: str = "", **extra) -> int:
    """The summary note of a ping: `decision` when a card of the session waits on the owner. Its number."""
    sid = session["id"]
    waiting = store.db.execute("SELECT 1 FROM approvals WHERE session = ? AND state = 'shown' AND kind IN "
                               "('approval', 'question')", (sid,)).fetchone() is not None
    if kind == DONE and waiting:
        kind = DECISION
    face = store.face_session(sid) or {}
    sphere = face.get("sphere")
    place = (spheres or {}).get(sphere) or sphere or session.get("track") or ""
    data = {"head": head, "text": text, "at": now, "turn": session.get("turn"), "kind": kind,
            "took_s": took(store, sid, run, now), "attended": attended(store, run), "line": line_before or None,
            "place": place, "title": line(face.get("title") or "", TITLE_CHARS),
            **({"quiet": True} if quiet else {}), **({"in_app": True} if in_app(store, sid) else {}), **extra}
    seq = store._note("summary", sid, data)
    store.db.commit()
    return seq
