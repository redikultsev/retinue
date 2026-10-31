"""Pings: the end of a turn as a message the owner can answer without opening the session.

Every summary note (`Platform._summary`) is a ping: a kind — `done`, `decision` (the model offered answers, or a card
of the session still waits) or `failed` (an error, the subscription's limit, a turn a restart cut, a run its lane
refused) — how long the turn took (`took_s`), where it belongs (the session's sphere or track) and its title, and up
to three suggested replies.

**The replies are a convention, not a tool.** The lanes' instructions (`deploy/lanes/instructions/common.md`) teach
the model to end a turn that leaves the owner a choice with one last line `Replies: "…" · "…"` (the older Russian
form `Ответы: «…» · «…»` is read too; either label takes "…", “…” or «…»). Code takes that line
off the text and parses it strictly (`split`): only the very last line, outside a code fence, each reply one line of
plain text of at most REPLY_CHARS with a letter or a digit, never one starting with a command character
(COMMAND_CHARS), three at most, duplicates and empty ones dropped. The line as written goes to the face (`line`),
which takes exactly it off the turn's last message instead of parsing one itself. Anything else is left as the
model wrote it — a model that forgets or garbles the line costs the buttons, never the message. No tool means no new
way out of a lane and nothing for the gate to rule on.

**A reply pressed** — a Telegram button or the face — is the owner's message into the session, input `ping:<seq>`:
the platform takes it once whichever place and however many times it is pressed (`inputs` is keyed by it), so a
press an hour later after a restart sends once. The replies of a ping close when one of them goes in (`answered`),
when the owner writes anything else into the session, or when a newer ping comes: the face drops them (`replies` of
its summary is emptied) and the Telegram message is rewritten by a `ping` note — «→ <reply>», or just without its
buttons. A ping whose session has a question or an approval card open gets no replies: the card has its own buttons.

**Loudness**: a `done` ping less than QUIET_S after the owner last acted on the session goes out silently (Telegram's
`disable_notification`) — he is looking — when that run was his own doing; a longer one, a `decision`, a `failed`
one, and one of a run the platform started by itself (`unattended`, `started`) notify (`loud`).
"""

from __future__ import annotations

import json
import re
import secrets
from typing import TYPE_CHECKING

from .courier import line
from .sessions import BUTTON_PREFIX, PlatformStore, _face_summary

if TYPE_CHECKING:
    from .platformd import Platform

DONE, DECISION, FAILED = "done", "decision", "failed"
ICONS = {DONE: "✅", DECISION: "❓", FAILED: "⚠️"}
MAX_REPLIES = 3
REPLY_CHARS = 60             # a button's words, on a phone
TITLE_CHARS = 40             # the session's title in a ping's header
QUIET_S = 120                # a done turn this soon after the owner's own input or press: no sound
BUTTON_TTL_S = 7 * 86400     # as long as a card's buttons
KEY = "ping/"                # a reply button's `approval`: ping/<seq>, never an approval's 8 hex
ACTION = "reply:"            # a reply button's action: reply:<n>
INPUT = "ping:"              # the input a pressed reply goes in as: ping:<seq>
GONE = "The button no longer works."
STALE = "Expired: the session has been written to since this summary."

# The label — `Replies:`, the instructed one, or the older `Ответы:` — bare or in bold/underline emphasis a model may
# add, then "…" · "…": each reply in straight quotes, curly ones or guillemets, never nested.
_QUOTED = r'"[^"\n]*"|“[^“”\n]*”|«[^«»\n]*»'
_LINE = re.compile(r"(?:\*\*|__)?(?:Replies|Ответы):(?:\*\*|__)?[ \t]*((?:" + _QUOTED + r")(?:[ \t]*·[ \t]*(?:" + _QUOTED +
                   r"))*)[ \t]*")
_REPLY = re.compile(r'"([^"\n]*)"|“([^“”\n]*)”|«([^«»\n]*)»')
_FENCE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})(.*)")
# What a reply may not start with: the CLI reads `/…` as a command or a skill and `!`/`#` as its own shortcuts — a
# button must never put a model-written (maybe page-steered) command in the owner's mouth.
COMMAND_CHARS = "/!#"
# Letters by category that show as blank space: a reply made of them is no words at all.
FILLERS = dict.fromkeys(map(ord, "\u115f\u1160\u3164\uffa0"))


def _in_fence(rows: list[str]) -> bool:
    """The rows end inside an open code fence (CommonMark: closed only by a fence of the same character, at least as
    long, with nothing after it)."""
    opened = ""
    for row in rows:
        if not (fence := _FENCE.match(row)):
            continue
        marker, rest = fence.groups()
        if not opened:
            opened = marker
        elif marker[0] == opened[0] and len(marker) >= len(opened) and not rest.strip():
            opened = ""
    return bool(opened)


def _reply(raw: str) -> str:
    """One reply as a button may carry it, or '' when it may not."""
    reply = line(raw.translate(FILLERS), 10_000)  # invisible, bidi and blank characters out, one line
    if len(reply) > REPLY_CHARS or reply[:1] in COMMAND_CHARS or not any(c.isalnum() for c in reply):
        return ""
    return reply


def parse(text: str) -> tuple[str, list[str], str]:
    """The text without its replies line, the replies, and the line as it was written (stripped); the text untouched,
    no replies and '' when its last line is not a well-formed replies line, sits in an open code fence, or leaves no
    reply after the caps."""
    lines = text.rstrip().split("\n")
    written = lines[-1].strip()
    found = _LINE.fullmatch(written)
    if not found or _in_fence(lines[:-1]):
        return text, [], ""
    replies: list[str] = []
    for quoted in _REPLY.findall(found.group(1)):
        if (reply := _reply("".join(quoted))) and reply not in replies:
            replies.append(reply)
    if not replies:
        return text, [], ""
    return "\n".join(lines[:-1]).rstrip(), replies[:MAX_REPLIES], written


def split(text: str) -> tuple[str, list[str]]:
    """`parse` without the line."""
    body, replies, _ = parse(text)
    return body, replies


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


def key(seq: int) -> str:
    return f"{KEY}{seq}"


def seq_of(bound: str) -> int | None:
    """The ping a button is bound to; None for an approval's button."""
    rest = bound[len(KEY):] if bound.startswith(KEY) else ""
    return int(rest) if rest.isdigit() else None


def input_of(seq: int) -> str:
    return f"{INPUT}{seq}"


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


def add(store: PlatformStore, session: dict, head: str, text: str, kind: str, now: float, *, run: str | None = None,
        spheres: dict[str, str] | None = None, quiet: bool = False, machine: bool = False, line_before: str = "",
        **extra) -> int:
    """The summary note of a ping and its reply buttons bound to it, in one transaction, after an earlier ping of the
    session is closed (its own). Its number."""
    sid = session["id"]
    replies: list[str] = []
    written = ""
    if kind == DONE:
        text, replies, written = parse(text)
    waiting = store.db.execute("SELECT 1 FROM approvals WHERE session = ? AND state = 'shown' AND kind IN "
                               "('approval', 'question')", (sid,)).fetchone() is not None
    if waiting:
        replies = []  # the card asks, with its own buttons
    if kind == DONE and (replies or waiting):
        kind = DECISION
    if not machine or replies:  # a turn not of the owner's doing leaves his open choice open unless it offers its own
        close_open(store, sid, now)
    if line_before:  # the sphere's line right before the replies' one (`subs.sphere_line`): the face hides both
        written = f"{line_before}\n{written}" if written else line_before
    face = store.face_session(sid) or {}
    sphere = face.get("sphere")
    place = (spheres or {}).get(sphere) or sphere or session.get("track") or ""
    buttons = [[reply, BUTTON_PREFIX + secrets.token_urlsafe(12)] for reply in replies]
    data = {"head": head, "text": text, "at": now, "turn": session.get("turn"), "kind": kind,
            "took_s": took(store, sid, run, now), "attended": attended(store, run), "line": written or None,
            "place": place, "title": line(face.get("title") or "", TITLE_CHARS),
            "replies": replies, "open": bool(replies), "answered": None, "buttons": buttons,
            **({"quiet": True} if quiet else {}), **extra}
    seq = store._note("summary", sid, data)
    store.db.executemany("INSERT INTO buttons (id, approval, action, digest, expires) VALUES (?, ?, ?, ?, ?)",
                         [(button, key(seq), f"{ACTION}{n}", sid, now + BUTTON_TTL_S)
                          for n, (_, button) in enumerate(buttons)])
    store.db.commit()
    return seq


def _open(store: PlatformStore, session: str) -> list[tuple[int, dict]]:
    rows = store.db.execute("SELECT seq, data FROM notes WHERE kind = 'summary' AND session = ? "
                            "AND json_extract(data, '$.open') = 1 ORDER BY seq", (session,))
    return [(seq, json.loads(data)) for seq, data in rows]


def _close(store: PlatformStore, session: str, seq: int, data: dict, answered: str | None, now: float) -> bool:
    """One ping's replies closed — answered with `answered`, or not — in the face and, by a `ping` note, in
    Telegram; its buttons spent. False when it was closed already."""
    closed = {**data, "open": False, "answered": answered}
    cursor = store.db.execute("UPDATE notes SET data = ? WHERE seq = ? AND json_extract(data, '$.open') = 1",
                              (json.dumps(closed, ensure_ascii=False), seq))
    if cursor.rowcount != 1:
        return False
    store.db.execute("UPDATE buttons SET used = ? WHERE approval = ? AND used IS NULL", (now, key(seq)))
    store._event("summary", session, f"{session}/{seq}", _face_summary(session, seq, closed))
    store._note("ping", session, {**{k: v for k, v in closed.items() if k != "buttons"}, "seq": seq})
    return True


def close_open(store: PlatformStore, session: str, now: float, input_id: str = "", text: str = "") -> None:
    """The owner wrote into the session (`input_id`, `text`), or a newer ping comes: every open ping of it closes —
    the one that input is a reply of as answered with it, the rest as stale."""
    for seq, data in _open(store, session):
        _close(store, session, seq, data, text if input_id == input_of(seq) else None, now)
    store.db.commit()


async def press(platform: Platform, session: str, seq: int, n: object) -> str:
    """A reply pressed, in Telegram or in the face: it goes into the session as the owner's input `ping:<seq>`, once.
    What to tell him."""
    store = platform.store
    row = store.db.execute("SELECT session, data FROM notes WHERE seq = ? AND kind = 'summary'", (seq,)).fetchone()
    if row is None or row[0] != session:
        return GONE
    data = json.loads(row[1])
    replies = data.get("replies") or []
    if type(n) is not int or not 0 <= n < len(replies) or \
            not isinstance(data.get("at"), (int, float)) or data["at"] + BUTTON_TTL_S <= platform.clock():
        return GONE  # the face's press lives as long as the Telegram button
    if (given := store.input_session(input_of(seq))) is not None:  # sent before a restart that kept it open
        answered = store.db.execute("SELECT text FROM inputs WHERE input_id = ?", (input_of(seq),)).fetchone()[0]
        if given == session and data.get("open"):
            _close(store, session, seq, data, answered, platform.clock())
            store.db.commit()
        return f'Already sent: "{answered}"'
    if not data.get("open"):
        return f'Already sent: "{data["answered"]}"' if data.get("answered") else STALE
    return await platform.write(input_of(seq), session, replies[n])


def face_summary(store: PlatformStore, session: str, seq: int) -> dict | None:
    """The ping as the face shows it now: what a press in the face answers with."""
    row = store.db.execute("SELECT data FROM notes WHERE seq = ? AND kind = 'summary' AND session = ?",
                           (seq, session)).fetchone()
    return _face_summary(session, seq, json.loads(row[0])) if row else None


def reply_of(action: str) -> int | None:
    rest = action[len(ACTION):] if action.startswith(ACTION) else ""
    return int(rest) if rest.isdigit() else None
