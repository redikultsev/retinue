"""Turns that went badly, marked by the platform from what happened — never by asking the owner: the material any
later learning needs (lessons, a replay set), and a count in the weekly review.

A turn is marked by the platform's own events, each a `why`:

- **owner_stop** — the owner pressed «Stop» in it (`Platform.stop`);
- **run_error** — it ended in an error the platform told him of;
- **card_rejected** — he denied a call it asked for;
- **base_undo** — he undid the Base commit of its work (`tobase`);
- **unchecked_end** — it ended over files not checked since they changed, even after the gate sent it on
  (`finish.Checks`);
- **loop** — it made the same call with the same outcome until the gate denied or cut it (`finish.Loops`);
- **asked_again** — within `AGAIN_S` of its end he wrote the request that started it again in other words, and it
  had not ended with a question to him (`owner_wrote`).

One row per turn — the turn of the run it happened in (`turn_of`), never a guess from the newest summary — its whys
gathered; the owner's next message in that session, within `CORRECTION_S` of the mark, is kept as the newest marked
turn's `correction` — what he wanted instead, in his words — and the turn's summary as he got it beside it
(`episode_summaries`).
The turn's raw lines outlive the store's 90 days (`sessions.PlatformStore.prune`) while its row is here; rows past
`KEEP` or `KEEP_DAYS` go, oldest first.
"""

from __future__ import annotations

import re

from .sessions import PlatformStore

OWNER_STOP, RUN_ERROR, CARD_REJECTED, BASE_UNDO = "owner_stop", "run_error", "card_rejected", "base_undo"
UNCHECKED_END, LOOP, ASKED_AGAIN = "unchecked_end", "loop", "asked_again"
KEEP = 200               # marked turns kept
KEEP_DAYS = 180          # and for at most this long
AGAIN_S = 10 * 60        # a request written again this soon after a turn's end is that turn's miss
CORRECTION_S = 3600      # the owner's next message this soon after a mark is its correction
SAME_WORDS = 0.5         # a request is the same when this share of the words of both is shared…
MIN_WORDS = 4            # …and it has at least this many words
TEXT_CHARS = 4000
_WORD = re.compile(r"\w{3,}", re.UNICODE)


def prepare(store: PlatformStore, now: float) -> None:
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            session TEXT NOT NULL,
            turn INTEGER NOT NULL,
            at REAL NOT NULL,                   -- the first mark
            why TEXT NOT NULL,                  -- its marks, comma-separated, in the order they came
            correction TEXT,                    -- his next message in the session, when it came within CORRECTION_S
            PRIMARY KEY (session, turn)
        );
        CREATE INDEX IF NOT EXISTS episodes_at ON episodes (at);
        -- every turn's summary as the owner got it, for as long as the store keeps the turn's raw lines: a turn
        -- marked after its end (an undo, a request asked again) finds its own
        CREATE TABLE IF NOT EXISTS episode_summaries (
            session TEXT NOT NULL,
            turn INTEGER NOT NULL,
            text TEXT NOT NULL,
            PRIMARY KEY (session, turn)
        );
        """)
    _trim(store, now)
    store.db.commit()


def _trim(store: PlatformStore, now: float) -> None:
    store.db.execute("DELETE FROM episodes WHERE at < ?", (now - KEEP_DAYS * 86400,))
    store.db.execute("DELETE FROM episodes WHERE rowid NOT IN (SELECT rowid FROM episodes ORDER BY at DESC LIMIT ?)",
                     (KEEP,))
    # a summary goes with its turn's raw lines (`PlatformStore.prune`), unless the turn is marked
    store.db.execute("DELETE FROM episode_summaries WHERE NOT EXISTS (SELECT 1 FROM raw WHERE raw.session = "
                     "episode_summaries.session AND raw.turn = episode_summaries.turn) AND NOT EXISTS (SELECT 1 FROM "
                     "episodes e WHERE e.session = episode_summaries.session AND e.turn = episode_summaries.turn)")


def turn_of(store: PlatformStore, session_id: str, run: str | None = None) -> int | None:
    """The turn a run belongs to: the turn of its lines, or — an open run whose first line has not come — the one
    they will get (`PlatformStore.add_raw`: the next after another run's). No run: the session's open one. None when
    there is no such run, or it ended with no line."""
    if run is None:
        found = store.db.execute("SELECT id FROM runs WHERE session = ? AND ended IS NULL ORDER BY started DESC "
                                 "LIMIT 1", (session_id,)).fetchone()
        if found is None:
            return None
        run = found[0]
    lined = store.db.execute("SELECT MIN(turn) FROM raw WHERE run = ?", (run,)).fetchone()[0]
    if lined is not None:
        return lined
    if store.db.execute("SELECT 1 FROM runs WHERE id = ? AND ended IS NULL", (run,)).fetchone() is None:
        return None
    row = store.db.execute("SELECT turn, turn_run FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None or not isinstance(row[0], int):
        return None
    turn, turn_run = row
    return turn + 1 if turn_run is not None and turn_run != run else turn


def mark(store: PlatformStore, session_id: str, why: str, now: float, turn: int | None = None,
         run: str | None = None) -> None:
    """The turn — `turn`, else `run`'s, else the session's open run's — marked `why`; a mark it has already is not
    added again. Nothing to mark without a turn."""
    turn = turn if turn is not None else turn_of(store, session_id, run)
    if turn is None or store.session(session_id) is None:
        return
    with store.db:
        row = store.db.execute("SELECT why FROM episodes WHERE session = ? AND turn = ?", (session_id, turn)).fetchone()
        if row is None:
            store.db.execute("INSERT INTO episodes (session, turn, at, why) VALUES (?, ?, ?, ?)",
                             (session_id, turn, now, why))
            _trim(store, now)
        elif why not in row[0].split(","):
            store.db.execute("UPDATE episodes SET why = ? WHERE session = ? AND turn = ?",
                             (f"{row[0]},{why}", session_id, turn))


def ended(store: PlatformStore, session_id: str, run: str, text: str) -> None:
    """The summary the owner got at the end of `run`'s turn, kept on its row when the turn is marked (a mark can come
    before the end: a stop, a loop — or after it: an undo)."""
    if (turn := turn_of(store, session_id, run)) is None:
        return
    with store.db:
        store.db.execute("INSERT INTO episode_summaries (session, turn, text) VALUES (?, ?, ?) ON CONFLICT DO UPDATE "
                         "SET text = excluded.text", (session_id, turn, text[:TEXT_CHARS]))


def trim(store: PlatformStore, now: float) -> None:
    """The rows past `KEEP_DAYS` or `KEEP` go: every tick, not only at a mark."""
    with store.db:
        _trim(store, now)


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")}


def same_request(a: str, b: str) -> bool:
    """Two messages that ask the same thing in other words: most of their longer words shared."""
    wa, wb = _words(a), _words(b)
    if len(wa) < MIN_WORDS or len(wb) < MIN_WORDS or a.strip() == b.strip():
        return False
    return len(wa & wb) / len(wa | wb) >= SAME_WORDS


def owner_wrote(store: PlatformStore, session_id: str, text: str, now: float) -> None:
    """The owner's message into a session with no turn running or queued: the correction of its newest marked turn
    still without one, marked within `CORRECTION_S`; and a mark `asked_again` of the turn that just ended when it
    asks again what he asked to start that turn — unless the turn ended asking him something."""
    session = store.session(session_id)
    if session is None or turn_of(store, session_id) is not None or \
            store.db.execute("SELECT 1 FROM queue WHERE session = ?", (session_id,)).fetchone():
        return
    ended_at = float(session.get("idle_since") or 0)
    said = store.last_summary(session_id) or {}
    turn = said.get("turn") if isinstance(said.get("turn"), int) else None
    before = store.db.execute("SELECT text FROM inputs WHERE session = ? AND by IS NULL AND text != '' AND at <= ? "
                              "ORDER BY at DESC, rowid DESC LIMIT 1", (session_id, ended_at)).fetchone()
    asking = str(said.get("text") or "").rstrip().endswith("?")
    if turn is not None and before and session.get("turn_by") in (None, "owner") and not asking and \
            0 <= now - ended_at <= AGAIN_S and same_request(before[0], text):
        mark(store, session_id, ASKED_AGAIN, now, turn=turn)
    with store.db:
        store.db.execute("UPDATE episodes SET correction = ? WHERE rowid = (SELECT rowid FROM episodes WHERE "
                         "session = ? AND correction IS NULL AND at >= ? ORDER BY turn DESC LIMIT 1)",
                         (text[:TEXT_CHARS], session_id, now - CORRECTION_S))


def tally(store: PlatformStore, since: float, until: float) -> dict:
    """The week's marked turns: how many, and how many of each mark."""
    counts: dict[str, int] = {}
    turns = 0
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'episodes'").fetchone():
        return {"turns": 0, "by_mark": {}}
    for (why,) in store.db.execute("SELECT why FROM episodes WHERE at >= ? AND at < ?", (since, until)):
        turns += 1
        for one in why.split(","):
            counts[one] = counts.get(one, 0) + 1
    return {"turns": turns, "by_mark": counts}
