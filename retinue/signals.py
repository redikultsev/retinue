"""How the work went, counted: one row per thing that happened in a session, in words that are the platform's own and
not any engine's, and the week's tally of them (`POST /owner/stats`) that the router puts into the weekly review.

A row is a kind, a time, a session and at most a tool and a short `what` — never an input, a command or a message's
text, so nothing here needs keeping from anyone:

- **gate** — every hook the gate answered (`Gate.hook`): `what` is how it went — `allow` (the owner's grant), `deny`,
  `defer` (to the owner), `rule` and `rule_deny` (an always or deny rule of the CLI decides), `classifier` (nothing
  named it: auto's classifier decides), `blocked` (the classifier stopped it; its `[tag]` only), `asked` (a fallback
  run's ask, denied) — and for a deny or a defer the gate's own reason, cut;
- **tool_error**, **compaction** — read off a session's lines by the engine's adapter (`from_line`): the only place
  that knows the CLI's stream-json. Another engine gets its own adapter, the kinds stay;
- **run_error** — a turn the platform itself told the owner ended in an error («Turn ended with an error»), once a run;
- **owner_stop** — the owner pressed «Stop» (`Platform.stop`);
- **finish** — a turn's end held back by the gate (`finish.Checks`): `what` is `stop_block`;
- **loop** — the same call or failure again in a turn (`finish.Loops`): `what` is `warn`, `deny` or `cut`;
- **web** — a fetch that failed (PostToolUseFailure): `what` is how (`web_what`) — `http_403`, `timeout`,
  `too_large`… — never the address, so a site the owner read stays nobody's business here.

What other tables already keep is read there, not copied: the owner's messages (`inputs`), runs (`runs`), sessions,
and the cards and how he decided them (`approvals`).
"""

from __future__ import annotations

import re
from collections import Counter

from . import episodes
from .sessions import PlatformStore

GATE, TOOL_ERROR, COMPACTION, RUN_ERROR, OWNER_STOP = "gate", "tool_error", "compaction", "run_error", "owner_stop"
FINISH, LOOP, WEB = "finish", "loop", "web"
# A status as fetch errors word it («status code 403», «HTTP 404», «403 Forbidden»), not any number in an address.
_HTTP = re.compile(r"(?:status(?:\s*code)?|http(?:/[\d.]+)?|error)\D{0,3}\b([45]\d\d)\b|\b([45]\d\d)\s+[a-z]")
KEEP_DAYS = 400  # a year and a bit: the same week a year ago stays comparable
WHAT_CHARS = 60
TOP = 5          # the gate's reasons named in a tally
TAG = re.compile(r"\[[^\]\n]{1,40}\]")


def prepare(store: PlatformStore, now: float) -> None:
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,                    -- a line's signal: lane:seq:n, so a replayed line adds none
            at REAL NOT NULL,
            kind TEXT NOT NULL,                 -- gate | tool_error | compaction | run_error | owner_stop | finish | loop | web
            session TEXT NOT NULL,
            call TEXT NOT NULL DEFAULT '',      -- the tool call it is about: a tool error finds its tool by it
            tool TEXT NOT NULL DEFAULT '',
            what TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS signals_at ON signals (at);
        """)
    store.db.execute("DELETE FROM signals WHERE at < ?", (now - KEEP_DAYS * 86400,))
    store.db.commit()


def add(store: PlatformStore, kind: str, session: str, now: float, tool: str = "", what: str = "", call: str = "",
        key: str | None = None) -> None:
    store.db.execute("INSERT OR IGNORE INTO signals (key, at, kind, session, call, tool, what) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (key, now, kind, session, call, tool, what[:WHAT_CHARS]))
    store.db.commit()


def gate_what(answer: dict, marks: list[str]) -> str:
    """How the gate answered a PreToolUse: its decision and, for a deny or a defer, its reason; no decision — whose it
    is by the call's marks."""
    out = answer.get("hookSpecificOutput") or {}
    decision = out.get("permissionDecision")
    if decision in ("deny", "defer"):
        return f"{decision}: {out.get('permissionDecisionReason') or ''}".rstrip(": ")
    if decision:
        return str(decision)
    return "rule_deny" if "deny" in marks else "rule" if marks else "classifier"


def blocked_what(reason: str) -> str:
    """A classifier block as kept: its tag (`[Remote Repoint]`) and nothing else — the rest may quote the command."""
    tag = TAG.search(reason or "")
    return f"blocked: {tag.group(0)}" if tag else "blocked"


def web_what(error: object) -> str:
    """How a fetch failed, as kept: an HTTP status, a timeout, a page too large, or `other`."""
    text = str(error or "").lower()
    if found := _HTTP.search(text):
        return "http_" + (found.group(1) or found.group(2))
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "too large" in text or "exceeds" in text or "maximum" in text:
        return "too_large"
    return "other"


def from_line(line: dict) -> list[tuple[str, str]]:
    """The signals of one stream-json line of Claude Code, as (kind, call): a tool's result that is an error, the
    conversation compacted. A subagent's lines count as the session's."""
    kind = line.get("type")
    if kind == "system" and line.get("subtype") == "compact_boundary":
        return [(COMPACTION, "")]
    if kind == "user":
        content = (line.get("message") or {}).get("content")
        if isinstance(content, list):
            return [(TOOL_ERROR, str(block.get("tool_use_id") or "")) for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error")]
    return []


# What going wrong is counted as, for the day against the week before (`worse`): the SQL of one count over [?, ?).
WORSE = {
    "turns ended with an error": "SELECT COUNT(*) FROM signals WHERE kind = 'run_error' AND at >= ? AND at < ?",
    "tool errors": "SELECT COUNT(*) FROM signals WHERE kind = 'tool_error' AND at >= ? AND at < ?",
    "loops denied or cut": "SELECT COUNT(*) FROM signals WHERE kind = 'loop' AND what IN ('deny', 'cut') AND at >= ? "
                           "AND at < ?",
    "fetches that failed": "SELECT COUNT(*) FROM signals WHERE kind = 'web' AND at >= ? AND at < ?",
}
WORSE_MIN = 3      # a day's count under this is never news
WORSE_TIMES = 2    # …nor one under this many times the week's usual day
STARTED_KEY = "platform.started"


def worse(store: PlatformStore, now: float) -> dict:
    """What went wrong more in the last day than on the week's usual day before it — the day's count at least
    `WORSE_MIN` and `WORSE_TIMES` the median of the seven days before — and when the platform last started, if that
    was within the day: an update is the first suspect. Counted by code, for the morning summary."""
    day, lines = 86400.0, []
    for what, sql in WORSE.items():
        today = int(store.db.execute(sql, (now - day, now)).fetchone()[0] or 0)
        week = sorted(int(store.db.execute(sql, (now - (n + 1) * day, now - n * day)).fetchone()[0] or 0)
                      for n in range(1, 8))
        usual = week[3]
        if today >= WORSE_MIN and today >= WORSE_TIMES * max(usual, 1):
            lines.append({"what": what, "day": today, "usual": usual})
    started = store.get(STARTED_KEY)
    started = float(started) if started is not None and now - float(started) < day else None
    return {"lines": lines, "started": started if lines else None}


def tally(store: PlatformStore, since: float, until: float) -> dict:
    """The week as numbers: what `/owner/stats` answers. Counted by code; the model that words the review never
    counts."""
    db, span = store.db, (since, until)

    def one(sql: str, *args) -> int:
        return int(db.execute(sql, args or span).fetchone()[0] or 0)

    gate = Counter(dict(db.execute("SELECT what, COUNT(*) FROM signals WHERE kind = 'gate' AND at >= ? AND at < ? "
                                   "GROUP BY what", span).fetchall()))
    how = Counter()
    for what, n in gate.items():
        how[what.split(":", 1)[0]] += n
    errors = Counter(dict(db.execute(
        "SELECT COALESCE(NULLIF(e.tool, ''), (SELECT g.tool FROM signals g WHERE g.kind = 'gate' AND g.call = e.call "
        "AND e.call != '' LIMIT 1), '?'), COUNT(*) FROM signals e WHERE e.kind = 'tool_error' AND e.at >= ? "
        "AND e.at < ? GROUP BY 1", span).fetchall()))
    cards = Counter(dict(db.execute("SELECT kind || ':' || state, COUNT(*) FROM approvals WHERE created >= ? "
                                    "AND created < ? GROUP BY kind, state", span).fetchall()))
    return {
        "since": since, "until": until,
        "sessions": one("SELECT COUNT(*) FROM sessions WHERE created >= ? AND created < ? AND sub_of IS NULL"),
        "sub_sessions": one("SELECT COUNT(*) FROM sessions WHERE created >= ? AND created < ? AND sub_of IS NOT NULL"),
        "runs": one("SELECT COUNT(*) FROM runs WHERE started >= ? AND started < ?"),
        "owner": {"messages": one("SELECT COUNT(*) FROM inputs WHERE at >= ? AND at < ? AND by IS NULL"),
                  "stops": one("SELECT COUNT(*) FROM signals WHERE kind = 'owner_stop' AND at >= ? AND at < ?")},
        "engine": {"tool_errors": sum(errors.values()),
                   "tool_errors_by_tool": dict(errors.most_common(TOP)),
                   "compactions": one("SELECT COUNT(*) FROM signals WHERE kind = 'compaction' AND at >= ? AND at < ?"),
                   "run_errors": one("SELECT COUNT(*) FROM signals WHERE kind = 'run_error' AND at >= ? AND at < ?")},
        "turns": {"sent_on_to_check": one("SELECT COUNT(*) FROM signals WHERE kind = 'finish' AND at >= ? AND at < ?"),
                  "loops": dict(db.execute("SELECT what, COUNT(*) FROM signals WHERE kind = 'loop' AND at >= ? "
                                           "AND at < ? GROUP BY what", span).fetchall()),
                  "web_failures": dict(db.execute("SELECT what, COUNT(*) FROM signals WHERE kind = 'web' AND at >= ? "
                                                  "AND at < ? GROUP BY what", span).fetchall())},
        "episodes": episodes.tally(store, since, until),
        "worse": worse(store, until),
        "gate": {"calls": sum(how.values()), "by_decision": dict(how),
                 "reasons": dict([(w, n) for w, n in gate.most_common() if ":" in w][:TOP])},
        "cards": {"approval": _states(cards, "approval"), "question": _states(cards, "question"),
                  "courier": sum(n for k, n in cards.items() if k.startswith("courier:"))},
    }


def _states(cards: Counter, kind: str) -> dict:
    """A kind's cards by how they ended: granted, rejected, answered, still open, gone by themselves."""
    states = {k.split(":", 1)[1]: n for k, n in cards.items() if k.startswith(kind + ":")}
    return {"shown": sum(states.values()),
            "granted": sum(states.get(s, 0) for s in ("granted", "executing", "executed")),
            "answered": states.get("answered", 0),
            "rejected": states.get("rejected", 0),
            "open": sum(states.get(s, 0) for s in ("pending", "shown")),
            "lapsed": sum(states.get(s, 0) for s in ("expired", "stale", "dropped"))}
