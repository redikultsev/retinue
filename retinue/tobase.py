"""«В Базу»: the owner's button that commits what a session changed in his knowledge base (the Base) — who changed
what, the card he decides on, in Telegram and in the app. The clone and the commit itself are the Base's own
(`base.BaseRepo`, `docs/base.md`); this module never runs git and never touches the tree.

**Who changed what.** Every session on the Base's lane — its folder in the clone or not: a notes session at `/work`
can Edit `/work/base/…` too; a side question only reads, so never — gets the Base files its runs changed. Each run is
watched through windows: a `snapshot()` of the tree (path → digest) when it starts, and again at each of its marks — a
`result` line (a turn ended; one run may hold several turns, the owner's message going into a live run) and its end.
What differs between the two snapshots of a window is the run's, and is recorded for its session in SQLite
(`base_files`), with the paths its own file tools named in that window (`changes.Scan` over its raw lines) as a
cross-check: a path both name is `both`, one only the tools name (a stat the snapshot missed) is still recorded, as
is one only the snapshot names (a Bash `sed`, or another session's run in the same window). Two runs that overlap
both get a file changed in the overlap: the owner decides on the card, which marks such a file. Only a path the
clone says is uncommitted (`changed()`) is ever recorded or kept: a file committed — by any session's button — or
refreshed to the hub's content leaves every session's list (`_prune`), at every mark, every start, after a commit and
once a minute. The marks are taken after the poll that ingested their lines (`settle`) and before any new run on the
lane starts, so a window never takes in a later run's edits; every window's state is a row (`base_runs`), so a
restart picks up where it was. A run whose first snapshot failed is still watched by its file tools, and by snapshots
from its next mark on. An uncommitted file no session claims is **nobody's** («ничей»): never hidden — every card
lists such files, turned off, for the owner to include, and the feed has a row while there are any.

**The ping.** The latest ping of a session with uncommitted Base files carries «В Базу · N» beside its reply buttons
(its summary's `base`, rendered by the bridge): added — by a `ping` note that rewrites the message — once the turn's
mark is taken, which comes a poll after the ping itself; moved to the newer ping when one comes; relabelled when N
changes; taken off when nothing is left. A press opens the card.

**The card** (`base_cards`, its buttons bound to `base/<id>` in the platform's `buttons`): «В Базу · <sphere or
track> · сессия <id>», the files with A/M/D and their +/− lines, each a toggle (on by default), the commit's subject
(by default `<Space of the first file>: <session title>`, the Base's own style; a reply to the card sets it), how many
files other sessions have uncommitted (not which), and «Закоммитить» · «Открыть дифф» · «Отмена». The app shows the
same card (an event `base`), and a change in one place is the other's: a Telegram press journals an `update` note for
every copy of the card and an event for the app, a press in the app the same. «Закоммитить» commits the files turned
on, only if each is as the card showed it (the digest it was opened with; otherwise the card is shown again with
what changed), and only while the session is not running: the card moves `shown → committing` once — of two presses,
in any places, one wins — and the commit runs in the background (`base.commit`, given each path's digest as the card
showed it: the Base refuses one that moved under its own lock), while no run on the lane starts (`before_start` waits
for it, bounded); the card rewritten with its sha and
files or with what was refused and why; refused files stay on the session's list. A card left `committing` by a
restart is settled from the clone's state: what is no longer uncommitted went in, the rest is shown again for one more
press — never committed without one (tried again on the clock while the Base cannot be read). A card expires like an
approval (7 days), goes stale when the session's next ping comes, and «Отмена» closes it with its files kept on the
list. A Telegram copy lists what fits in one message, and its «Закоммитить» commits only the files it lists.

Nothing commits without the owner's press: the button is reachable only through the router's token (the bridge) and
the face's login, never from a lane.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import posixpath
import secrets
from typing import TYPE_CHECKING

from . import changes
from .spoken import unmarked
from .courier import line
from .sessions import parsed

if TYPE_CHECKING:
    from .platformd import Platform

log = logging.getLogger("retinue.tobase")

KEY = "base/"                 # a card's buttons and notes: base/<card id>
OPEN = "base/open"            # the ping's «В Базу · N»: its digest is the session
TOGGLE, COMMIT, DIFF, CANCEL = "t:", "commit", "diff", "cancel"
LABELS = {COMMIT: "Закоммитить", DIFF: "Открыть дифф", CANCEL: "Отмена"}
TTL_S = 7 * 86400             # a card and its buttons live as long as an approval's
MAX_TOGGLES = 12              # a file toggle per Telegram button; past this the rest are toggled in the app
LISTED_CHARS = 2800           # the file list of a Telegram card; past it «…и ещё N» (the card stays under 4000)
STATUS_CHARS = 700            # its status lines; past them «…и ещё N строк»
SUBJECT_CHARS = 120
TITLE_CHARS = 70
DIFF_LIMIT = 1024 * 1024      # «Открыть дифф»
COUNT_LIMIT = 256 * 1024      # one file's diff read to count its lines
COUNTED = 60                  # files whose +/− are counted; the rest show none
PRUNE_S = 60.0                # the lists checked against the clone at least this often while any is not empty
LIVE = ("shown", "committing")
GONE = "Карточка больше не действует."
NO_BASE = "База не подключена."
NOTHING = "У сессии нет незакоммиченных файлов Базы."
RUNNING = "Сессия сейчас работает — дождись её итога."
UNAVAILABLE = "Клон Базы сейчас недоступен — попробуй позже."
RESTARTED = "Коммит прервался перезапуском платформы — нажми «Закоммитить» ещё раз."
REOPEN = "Карточка больше не действует — открой «В Базу» заново."
NOBODY = "ничей"


def plural(n: int, one: str, few: str, many: str) -> str:
    tail = n % 100
    word = one if tail % 10 == 1 and tail != 11 else few if 2 <= tail % 10 <= 4 and not 12 <= tail <= 14 else many
    return f"{n} {word}"


def files_word(n: int) -> str:
    return plural(n, "файл", "файла", "файлов")


def counted(diff: str) -> tuple[int, int]:
    """Lines added and removed in a unified diff, its headers left out."""
    add = rem = 0
    for row in diff.splitlines():
        if row.startswith("+") and not row.startswith("+++ "):
            add += 1
        elif row.startswith("-") and not row.startswith("--- "):
            rem += 1
    return add, rem


def short(path: str, cap: int = 40) -> str:
    """A path for a button: its end kept, where the file's name is."""
    return path if len(path) <= cap else "…" + path[-(cap - 1):]


def subject_of(text: str) -> str:
    """The owner's words as a commit subject: one line, the voice mark off, cut."""
    text = line(text, 10_000)
    return line(unmarked(text), SUBJECT_CHARS)


class ToBase:
    def __init__(self, platform: Platform) -> None:
        self.platform, self.store = platform, platform.store
        self._lock = asyncio.Lock()          # one mark, prune or open at a time: they read and write the same rows
        self._pruned = 0.0
        self._commits: dict[str, asyncio.Task] = {}  # the commits running now, by card
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS base_runs (
                run TEXT PRIMARY KEY,             -- a run of a session on the Base's lane, watched
                session TEXT NOT NULL,
                before TEXT,                      -- the tree's snapshot at its last mark: path -> digest, JSON;
                                                  -- NULL: it failed, the file tools alone count for that window
                last_raw INTEGER NOT NULL DEFAULT 0  -- the raw line (`raw.id`) its last mark read up to
            );
            CREATE TABLE IF NOT EXISTS base_cards (
                id TEXT PRIMARY KEY,              -- 8 hex; its buttons and notes are base/<id>
                session TEXT NOT NULL,
                state TEXT NOT NULL,              -- shown | committing | committed | failed | cancelled | stale | expired
                files TEXT NOT NULL,              -- [{path, status, add, del, on, digest, nobody}], JSON
                subject TEXT NOT NULL DEFAULT '', -- the owner's; '' = `fallback`
                fallback TEXT NOT NULL,           -- `<Space>: <title>`
                ping INTEGER,                     -- the session's latest ping when it was opened
                result TEXT NOT NULL DEFAULT '{}', -- {sha, files, refused, said}
                created REAL NOT NULL,
                at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS base_cards_session ON base_cards (session, state);
            CREATE TABLE IF NOT EXISTS base_dirty (
                path TEXT PRIMARY KEY,            -- the clone's uncommitted paths as it last said (`_dirty`)
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS base_pings (
                session TEXT PRIMARY KEY,         -- the one ping of the session that carries «В Базу · N»
                seq INTEGER NOT NULL,
                n INTEGER NOT NULL
            );
            """)
        self.store.db.commit()
        self._recovering: asyncio.Task | None = None
        # The app's «В Базу · N» counts only while a Base is configured: rows left from before are never shown.
        self.store.base_shown = lambda: self.base is not None

    # --- where the Base is ------------------------------------------------------------------------------------------

    @property
    def base(self):
        """The Base's clone (`base.BaseRepo`), or None when the platform has none configured."""
        return getattr(self.platform, "base", None)

    def lane(self) -> str:
        """The Base's lane: `base.lane` of `platform.yaml`."""
        cfg = getattr(self.platform.cfg, "base", None)
        return getattr(cfg, "lane", None) or "private"

    def watched(self, session: dict | None) -> bool:
        """A session that can change the Base: on its lane, whatever its folder (`/work` holds the clone), not a side
        question (its runs only read)."""
        return self.base is not None and session is not None and not session.get("parent") and \
            session.get("lane") == self.lane()

    def _rel(self, path: str) -> str | None:
        """A path a file tool named, relative to the clone's root; None for one outside it."""
        root = posixpath.join("/work", str(self.base.tree_rel).strip("/"))
        path = posixpath.normpath(path) if posixpath.isabs(path) else ""
        return path[len(root) + 1:] if path.startswith(root + "/") else None

    # --- who changed what ---------------------------------------------------------------------------------------------

    async def before_start(self, session: dict) -> bool:
        """Before a run on the Base's lane starts (and before the Base is refreshed for it): False while a commit is
        in flight — the run must not change what is being staged; it stays queued, and the next drain (a poll on)
        tries again, so neither the drain nor the Base's `busy` ever waits on the other. Otherwise every mark owed is
        taken, so no earlier window takes in what this run does."""
        if not self.watched(session):
            return True
        if self._commits:
            return False
        await self.settle()
        return True

    async def run_started(self, session: dict, run: str) -> None:
        """A run of a session in the clone is about to start: its first window opens on the tree as it is now."""
        if not self.watched(session):
            return
        async with self._lock:
            try:
                before = json.dumps(await asyncio.to_thread(self.base.snapshot))
            except Exception as exc:  # the type only: a name in the tree may say anything
                log.error("session %s: the Base's snapshot failed (%s); its file tools alone count", session["id"],
                          type(exc).__name__)
                before = None
            last = self.store.db.execute("SELECT COALESCE(MAX(id), 0) FROM raw").fetchone()[0]
            self.store.db.execute("INSERT OR REPLACE INTO base_runs (run, session, before, last_raw) "
                                  "VALUES (?, ?, ?, ?)", (run, session["id"], before, last))
            self.store.db.commit()

    async def settle(self) -> None:
        """Every mark owed — a watched run's new `result` line, or its end — taken: the window's difference recorded
        for its session; then the lists checked against the clone and the pings brought up to date. Cheap when nothing
        is owed: a few rows read, and the clone asked once a minute while any list is not empty."""
        if self.base is None:
            self._unping()
            return
        async with self._lock:
            await self._settle()

    def _unping(self) -> None:
        """No Base configured: a ping still carrying «В Базу · N» from before loses it."""
        for session, seq in self.store.db.execute("SELECT session, seq FROM base_pings").fetchall():
            self._ping_button(seq, None)
            self.store.db.execute("DELETE FROM base_pings WHERE session = ?", (session,))
        self.store.db.commit()

    async def _settle(self) -> None:
        db, now = self.store.db, self.platform.clock()
        owed = []
        for run, session, last_raw in db.execute("SELECT run, session, last_raw FROM base_runs").fetchall():
            ended = db.execute("SELECT ended FROM runs WHERE id = ?", (run,)).fetchone()
            ended = ended is None or ended[0] is not None
            # Only the lines since its last mark are looked at (a range of `raw.id`): a long session costs nothing.
            if ended or db.execute("SELECT 1 FROM raw WHERE id > ? AND run = ? AND json_extract(line, '$.type') = "
                                   "'result'", (last_raw, run)).fetchone():
                owed.append((run, session, ended))
        if not owed and not (now - self._pruned >= PRUNE_S and db.execute("SELECT 1 FROM base_files").fetchone()):
            return
        touched: set[str] = set()
        if owed:
            try:
                after = await asyncio.to_thread(self.base.snapshot)
                dirty = await self._dirty()
            except Exception as exc:
                log.error("the Base's snapshot failed (%s); the marks wait for the next poll", type(exc).__name__)
                return
            for run, session, ended in owed:
                if ended and db.execute("SELECT 1 FROM raw WHERE run = ? AND session = ?", (run, session)).fetchone() \
                        is None:
                    db.execute("DELETE FROM base_runs WHERE run = ?", (run,))  # never started: nothing of its own
                    continue
                touched.add(session)
                self._mark(run, session, after, dirty, ended, now)
            db.commit()
        else:
            try:
                dirty = await self._dirty()
            except Exception as exc:
                log.error("the Base's state was not read (%s)", type(exc).__name__)
                return
        touched |= self._prune(dirty)
        for session in touched:
            self.ping(session)

    def _mark(self, run: str, session: str, after: dict, dirty: dict[str, str], ended: bool, now: float) -> None:
        db = self.store.db
        before_text, last_raw = db.execute("SELECT before, last_raw FROM base_runs WHERE run = ?", (run,)).fetchone()
        before = json.loads(before_text) if before_text is not None else None
        by_snapshot = set() if before is None else {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        scan, upto = changes.Scan(), last_raw
        for raw_id, text in db.execute("SELECT id, line FROM raw WHERE id > ? AND run = ? ORDER BY id",
                                       (last_raw, run)):
            scan.line(parsed(text))
            upto = raw_id
        by_tools = {rel for c in scan.changes if (rel := self._rel(c.path)) is not None}
        for path in (by_snapshot | by_tools) & dirty.keys():
            via = "both" if path in by_snapshot and path in by_tools else "snapshot" if path in by_snapshot else "tool"
            row = db.execute("SELECT via FROM base_files WHERE session = ? AND path = ?", (session, path)).fetchone()
            if row is None:
                db.execute("INSERT INTO base_files (session, path, via, at) VALUES (?, ?, ?, ?)", (session, path, via, now))
            elif row[0] != via and row[0] != "both":
                db.execute("UPDATE base_files SET via = 'both' WHERE session = ? AND path = ?", (session, path))
        if ended:
            db.execute("DELETE FROM base_runs WHERE run = ?", (run,))
        else:
            db.execute("UPDATE base_runs SET before = ?, last_raw = ? WHERE run = ?", (json.dumps(after), upto, run))
        self.store._session_event(session)

    async def _dirty(self) -> dict[str, str]:
        """The clone's uncommitted paths and their A/M/D, as it says now — kept (`base_dirty`), so the feed can name
        the files no session claims without asking the clone."""
        dirty = {c.path: c.status for c in await asyncio.to_thread(self.base.changed)}
        db = self.store.db
        db.execute("DELETE FROM base_dirty")
        db.executemany("INSERT INTO base_dirty (path, status) VALUES (?, ?)", sorted(dirty.items()))
        db.commit()
        return dirty

    def nobodys(self) -> list[str]:
        """The uncommitted files no session claims, as the clone last said."""
        if self.base is None:
            return []
        return [p for (p,) in self.store.db.execute(
            "SELECT path FROM base_dirty WHERE path NOT IN (SELECT path FROM base_files) ORDER BY path")]

    def carrier(self) -> str | None:
        """The session whose card offers nobody's files when no session has any of its own: the latest on the Base's
        lane. Its card lists them turned off — nothing goes unless the owner turns it on."""
        row = self.store.db.execute("SELECT id FROM sessions WHERE lane = ? AND parent IS NULL ORDER BY at DESC LIMIT 1",
                                    (self.lane(),)).fetchone()
        return row[0] if row else None

    def _prune(self, dirty: dict[str, str]) -> set[str]:
        """Every list loses what is no longer uncommitted — committed by any button, or refreshed to the hub's
        content. The sessions whose list changed."""
        db = self.store.db
        self._pruned = self.platform.clock()
        gone = [(s, p) for s, p in db.execute("SELECT session, path FROM base_files") if p not in dirty]
        db.executemany("DELETE FROM base_files WHERE session = ? AND path = ?", gone)
        sessions = {s for s, _ in gone}
        for session in sessions:
            self.store._session_event(session)
        db.commit()
        return sessions

    def paths(self, session: str) -> list[str]:
        return [p for (p,) in self.store.db.execute("SELECT path FROM base_files WHERE session = ? ORDER BY path",
                                                    (session,))]

    def count(self, session: str) -> int:
        return self.store.db.execute("SELECT COUNT(*) FROM base_files WHERE session = ?", (session,)).fetchone()[0]

    # --- the ping's button ----------------------------------------------------------------------------------------

    def ping(self, session: str) -> None:
        """The session's latest ping carries «В Базу · N» while N > 0, and no older ping does; an open card of an
        older ping goes stale."""
        db, now = self.store.db, self.platform.clock()
        latest = db.execute("SELECT MAX(seq) FROM notes WHERE kind = 'summary' AND session = ?", (session,)).fetchone()[0]
        n = self.count(session)
        held = db.execute("SELECT seq, n FROM base_pings WHERE session = ?", (session,)).fetchone()
        if held is not None and (held[0] != latest or n == 0):
            self._ping_button(held[0], None)
            db.execute("DELETE FROM base_pings WHERE session = ?", (session,))
            held = None
        if latest is not None:
            for card in self.cards(session, ("shown",)):
                if card["ping"] is not None and card["ping"] < latest:
                    self._finish(card, "stale", {"said": "Устарела: у сессии новый итог — «В Базу» под ним."}, now)
        if n > 0 and latest is not None and (held is None or held[1] != n):
            button = self.store.add_button(OPEN, "open", session, now + TTL_S)
            self._ping_button(latest, [f"В Базу · {n}", button])
            db.execute("INSERT OR REPLACE INTO base_pings (session, seq, n) VALUES (?, ?, ?)", (session, latest, n))
        db.commit()

    def _ping_button(self, seq: int, button: list | None) -> None:
        """The ping's summary note with `base` set (or taken off), and a `ping` note that rewrites its message: its
        reply buttons stay while they are open."""
        row = self.store.db.execute("SELECT session, data FROM notes WHERE seq = ? AND kind = 'summary'",
                                    (seq,)).fetchone()
        if row is None:
            return
        data = {**json.loads(row[1]), "base": button}
        self.store.db.execute("UPDATE notes SET data = ? WHERE seq = ?", (json.dumps(data, ensure_ascii=False), seq))
        self.store._note("ping", row[0], {**data, "seq": seq})

    # --- the card -------------------------------------------------------------------------------------------------

    CARD = ("id", "session", "state", "files", "subject", "fallback", "ping", "result", "created", "at")

    def card(self, card_id: str) -> dict | None:
        row = self.store.db.execute(f"SELECT {', '.join(self.CARD)} FROM base_cards WHERE id = ?",
                                    (card_id,)).fetchone()
        if row is None:
            return None
        card = dict(zip(self.CARD, row))
        card["files"], card["result"] = json.loads(card["files"]), json.loads(card["result"])
        return card

    def cards(self, session: str, states: tuple[str, ...] = ()) -> list[dict]:
        where = f" AND state IN ({', '.join('?' * len(states))})" if states else ""
        ids = self.store.db.execute(f"SELECT id FROM base_cards WHERE session = ?{where} ORDER BY created, rowid",
                                    (session, *states)).fetchall()
        return [self.card(card_id) for (card_id,) in ids]

    async def open(self, session_id: str, *, telegram: bool) -> tuple[str, dict | None]:
        """«В Базу»: the session's card — its open one brought up to date, or a new one — and, from Telegram, a copy
        of it sent as a message. What to tell the owner, and the card as the app shows it."""
        if self.base is None:
            return NO_BASE, None
        session = self.store.session(session_id)
        if session is None:
            return "Нет такой сессии.", None
        async with self._lock:
            try:
                dirty = await self._dirty()
                snapshot = await asyncio.to_thread(self.base.snapshot)
            except Exception as exc:  # `base.Unavailable` (an OSError): the clone's folder or the hub is away
                log.error("session %s: the Base was not read (%s)", session_id, type(exc).__name__)
                return UNAVAILABLE if isinstance(exc, OSError) else f"База не прочиталась ({type(exc).__name__}).", None
            for touched in self._prune(dirty):
                self.ping(touched)
            paths, nobodys = self.paths(session_id), self.nobodys()
            if self.cards(session_id, ("committing",)):
                return "Коммит этой сессии уже идёт.", None
            if not paths and not nobodys:
                self.ping(session_id)
                return NOTHING, None
            files = await self._files(paths, dirty, snapshot) + \
                [{**f, "on": False, "nobody": True} for f in await self._files(nobodys, dirty, snapshot)]
            # The awaits above let a press in: a card that started committing meanwhile is left alone.
            if self.cards(session_id, ("committing",)):
                return "Коммит этой сессии уже идёт.", None
            now = self.platform.clock()
            [card] = self.cards(session_id, ("shown",))[-1:] or [None]
            if card is None:
                card = self._new(session, files, now)
            else:
                kept = {f["path"]: f["on"] for f in card["files"]}
                card["files"] = [{**f, "on": kept.get(f["path"], f["on"])} for f in files]
                if not self._save(card, now):
                    return "Коммит этой сессии уже идёт.", None
            self._show(card, now, send=telegram)
            return ("Карточка — ниже." if telegram else ""), self.face(card)

    async def _files(self, paths: list[str], dirty: dict[str, str], snapshot: dict) -> list[dict]:
        """Each path with its A/M/D, its +/− lines (the first COUNTED files) and its digest as the card shows it."""
        out = []
        for n, path in enumerate(paths):
            add = rem = None
            if n < COUNTED:
                try:
                    add, rem = counted(await asyncio.to_thread(self.base.diff, [path], COUNT_LIMIT))
                except Exception as exc:
                    log.warning("the Base's diff of one file failed (%s)", type(exc).__name__)
            out.append({"path": path, "status": dirty.get(path, "M"), "add": add, "del": rem, "on": True,
                        "digest": snapshot.get(path)})
        return out

    def _new(self, session: dict, files: list[dict], now: float) -> dict:
        title = line((self.store.face_session(session["id"]) or {}).get("title") or session["track"], TITLE_CHARS)
        first = next((f for f in files if not f.get("nobody")), files[0])["path"]
        space = first.split("/", 1)[0] if "/" in first else ""
        latest = self.store.db.execute("SELECT MAX(seq) FROM notes WHERE kind = 'summary' AND session = ?",
                                       (session["id"],)).fetchone()[0]
        card = {"id": secrets.token_hex(4), "session": session["id"], "state": "shown", "files": files, "subject": "",
                "fallback": line(f"{space}: {title}" if space else title, SUBJECT_CHARS), "ping": latest,
                "result": {}, "created": now, "at": now}
        self.store.db.execute(f"INSERT INTO base_cards ({', '.join(self.CARD)}) VALUES ({', '.join('?' * 10)})",
                              self._row(card))
        return card

    @staticmethod
    def _row(card: dict) -> tuple:
        return (card["id"], card["session"], card["state"], json.dumps(card["files"], ensure_ascii=False),
                card["subject"], card["fallback"], card["ping"], json.dumps(card["result"], ensure_ascii=False),
                card["created"], card["at"])

    def _save(self, card: dict, now: float, states: tuple[str, ...] = ("shown",)) -> bool:
        """The card written back while it is still in one of `states`; True when it was."""
        cursor = self.store.db.execute(
            f"UPDATE base_cards SET state = ?, files = ?, subject = ?, result = ?, at = ? WHERE id = ? AND state IN "
            f"({', '.join('?' * len(states))})",
            (card["state"], json.dumps(card["files"], ensure_ascii=False), card["subject"],
             json.dumps(card["result"], ensure_ascii=False), now, card["id"], *states))
        card["at"] = now
        return cursor.rowcount == 1

    def _show(self, card: dict, now: float, *, send: bool = False) -> list[list[str]]:
        """The card's new state to everyone: the app's event, an `update` note for every Telegram copy, and with
        `send` a new copy. Its buttons as they now are."""
        face = self.face(card)
        buttons = self._buttons(card, now)
        note = {"view": face["view"], "buttons": buttons}
        self.store._event("base", card["session"], card["id"], face)
        self.store._note("update", card["session"], note, approval=KEY + card["id"])
        if send:
            self.store._note("card", card["session"], note, approval=KEY + card["id"])
        self.store.db.commit()
        return buttons

    def _buttons(self, card: dict, now: float) -> list[list[str]]:
        """A shown card's buttons: a toggle for each of its first files (bound to the path, so a stale copy never
        toggles another file), then «Закоммитить», «Открыть дифф», «Отмена». A live button is kept, a missing one
        made; a card no longer shown shows none. None is ever spent: the card's state answers a press — «Уже
        закоммичено» to an old copy's — and a card a restart puts back takes its old buttons again."""
        key = KEY + card["id"]
        if card["state"] != "shown":
            return []
        live = dict(self.store.live_buttons(key, now))
        wanted = [(f"{'☑' if f['on'] else '☐'} {f['status']} {short(f['path'])}", TOGGLE + f["path"])
                  for f in card["files"][:MAX_TOGGLES]] + [(LABELS[a], a) for a in (COMMIT, DIFF, CANCEL)]
        return [[label, live.get(action) or self.store.add_button(key, action, card["id"], card["created"] + TTL_S)]
                for label, action in wanted]

    def subject(self, card: dict) -> str:
        return card["subject"] or card["fallback"]

    def face(self, card: dict) -> dict:
        """The card as the app shows it, and its `view` as Telegram does (`courier.CardView`)."""
        db = self.store.db
        mine = {f["path"] for f in card["files"]}
        shared = {p for (p,) in db.execute("SELECT DISTINCT path FROM base_files WHERE session != ?", (card["session"],))
                  if p in mine}
        others = db.execute("SELECT COUNT(DISTINCT path), COUNT(DISTINCT session) FROM base_files WHERE session != ?",
                            (card["session"],)).fetchone()
        session = self.store.face_session(card["session"]) or {}
        sphere = session.get("sphere")
        place = next((f"{s.icon} {s.title}".strip() for s in self.platform.cfg.spheres if s.name == sphere), None) \
            if sphere else None
        place = place or sphere or session.get("track") or ""
        files = [{"path": f["path"], "status": f["status"], "add": f["add"], "del": f["del"], "on": f["on"],
                  "shared": f["path"] in shared, "nobody": bool(f.get("nobody"))} for f in card["files"]]
        result = card["result"]
        return {"id": card["id"], "session": card["session"], "state": card["state"], "place": place,
                "files": files, "subject": self.subject(card), "custom": bool(card["subject"]),
                "others": {"files": others[0], "sessions": others[1]}, "sha": result.get("sha"),
                "said": result.get("said") or "", "refused": result.get("refused") or [],
                "committed": result.get("files") or [], "created": card["created"], "at": card["at"],
                "view": self._view(card, place, files, others)}

    @staticmethod
    def _rows(files: list[dict]) -> list[str]:
        """The file lines of a Telegram copy: as many as fit LISTED_CHARS, in the card's order."""
        rows, size = [], 0
        for f in files:
            counts = f" +{f['add']} −{f['del']}" if f["add"] is not None else ""
            marks = (" ⇄" if f.get("shared") else "") + (f" · {NOBODY}" if f.get("nobody") else "")
            row = f"{'☑' if f['on'] else '☐'} {f['status']} {f['path']}{counts}{marks}"
            if size + len(row) > LISTED_CHARS:
                break
            rows.append(row)
            size += len(row) + 1
        return rows

    def _view(self, card: dict, place: str, files: list[dict], others: tuple[int, int]) -> dict:
        head = [f"В Базу · {place} · сессия {card['session']}", f"Тема: {self.subject(card)}"]
        rows = self._rows(files)
        if len(rows) < len(files):
            rows.append(f"…и ещё {files_word(len(files) - len(rows))}: из Telegram не коммитятся — они в приложении")
        status = []
        if any(f["shared"] for f in files):
            status.append("⇄ — этот файл менялся и в другой сессии.")
        if nobody := sum(1 for f in files if f["nobody"]):
            status.append(f"«{NOBODY}» — {files_word(nobody)} не из сессий: изменены, но ничья сессия их не меняла. "
                          "Выключены — включи, чтобы закоммитить.")
        if others[0]:
            status.append(f"Ещё {files_word(others[0])} не закоммичено в других сессиях "
                          f"({plural(others[1], 'сессия', 'сессии', 'сессий')}).")
        if card["state"] == "shown":
            if len(files) > MAX_TOGGLES:
                status.append(f"Переключатели — у первых {MAX_TOGGLES} файлов, остальные — в приложении.")
            status.append("Ответь на карточку, чтобы сменить тему.")
        elif card["state"] == "committing":
            status.append("Коммичу…")
        if card["result"].get("said"):
            status += card["result"]["said"].split("\n")
        kept, size = [], 0
        for n, row in enumerate(status):
            if size + len(row) > STATUS_CHARS:
                kept.append(f"…и ещё {plural(len(status) - n, 'строка', 'строки', 'строк')} — в приложении")
                break
            kept.append(row)
            size += len(row) + 1
        return {"head": head, "body": "\n".join(rows), "status": "\n".join(kept), "link": []}

    def _finish(self, card: dict, state: str, result: dict, now: float, states: tuple[str, ...] = ("shown",)) -> bool:
        card = {**card, "state": state, "result": result}
        if not self._save(card, now, states):
            return False
        self._show(card, now)
        return True

    # --- what the owner does: in Telegram (`press`, `reply`) and in the app (face.py) ---------------------------------

    async def press(self, bound: dict) -> dict:
        """A Telegram button of «В Базу»: the ping's (the card sent), or one of a card's. What the bridge does: the
        toast, the card's view and the buttons it keeps, or the diff as a document."""
        if bound["approval"] == OPEN:
            toast, _ = await self.open(bound["digest"], telegram=True)
            return {"toast": toast}
        card = self.card(bound["approval"][len(KEY):])
        if card is None:
            return {"toast": GONE}
        action = bound["action"]
        if action == DIFF:
            text = await self.diff(card)
            return {"toast": "Дифф — файлом.", "document": {"name": f"base-{card['id']}.diff", "text": text}}
        if action.startswith(TOGGLE):
            toast, card = self.toggle(card, action[len(TOGGLE):], None)
        elif action == COMMIT:
            toast, card = await self.commit(card, listed=len(self._rows(self.face(card)["files"])))
        elif action == CANCEL:
            toast, card = self.cancel(card)
        else:
            return {"toast": GONE}
        if card is None:
            return {"toast": toast}
        face = self.face(card)
        return {"toast": toast, "view": face["view"], "keep": self._buttons(card, self.platform.clock())}

    def reply(self, key: str, session_id: str, text: str) -> str | None:
        """The owner's reply to a card's Telegram message: its text is the commit's subject while the card is shown;
        a card no longer shown takes it too, and says so — a subject never goes into the session as a prompt. None
        only for a message that is no «В Базу» card."""
        if not key.startswith(KEY):
            return None
        card = self.card(key[len(KEY):]) if key != OPEN else None
        if card is None or card["session"] != session_id or card["state"] != "shown":
            return REOPEN
        return self.set_subject(card, text)[0]

    def toggle(self, card: dict, path: str, on: bool | None) -> tuple[str, dict | None]:
        """A file turned on or off (`on` None: the other way round)."""
        if card["state"] != "shown":
            return GONE, None
        files = [{**f, "on": (not f["on"] if on is None else on)} if f["path"] == path else f for f in card["files"]]
        if files == card["files"]:
            return ("" if any(f["path"] == path for f in files) else "Такого файла в карточке нет."), card
        card = {**card, "files": files}
        now = self.platform.clock()
        if not self._save(card, now):
            return GONE, None
        self._show(card, now)
        return "", card

    def set_subject(self, card: dict, text: str) -> tuple[str, dict | None]:
        if card["state"] != "shown":
            return GONE, None
        subject = subject_of(text)
        card = {**card, "subject": "" if subject == card["fallback"] else subject}
        now = self.platform.clock()
        if not self._save(card, now):
            return GONE, None
        self._show(card, now)
        return f"Тема коммита: «{self.subject(card)}»", card

    def cancel(self, card: dict) -> tuple[str, dict | None]:
        if not self._finish(card, "cancelled", {"said": "Отменено. Файлы остались незакоммиченными."},
                            self.platform.clock()):
            return GONE, None
        return "Отменено.", self.card(card["id"])

    async def diff(self, card: dict) -> str:
        """The bounded diff of the files turned on (all of them when none is), as the clone has them now."""
        paths = [f["path"] for f in card["files"] if f["on"]] or [f["path"] for f in card["files"]]
        try:
            return await asyncio.to_thread(self.base.diff, paths, DIFF_LIMIT) if self.base is not None else ""
        except Exception as exc:
            return f"{UNAVAILABLE}\n" if isinstance(exc, OSError) else f"Дифф не получился: {type(exc).__name__}\n"

    async def commit(self, card: dict, listed: int | None = None) -> tuple[str, dict | None]:
        """«Закоммитить»: the files turned on, as the card showed them, committed in the background — once, whichever
        place pressed it and however often. `listed`: a Telegram press, whose copy lists only the first files — only
        those go."""
        if card["state"] == "committing":
            return "Коммичу — подожди.", None
        if card["state"] != "shown":
            return ("Уже закоммичено." if card["state"] == "committed" else GONE), None
        if self.base is None:
            return NO_BASE, None
        chosen = [f for f in card["files"][:listed] if f["on"]]
        if not chosen:
            return "Не выбрано ни одного файла.", None
        if any(r["session"] == card["session"] for r in self.store.open_runs()):
            return RUNNING, None
        try:
            snapshot = await asyncio.to_thread(self.base.snapshot)
        except Exception as exc:
            return UNAVAILABLE if isinstance(exc, OSError) else f"База не прочиталась ({type(exc).__name__}).", None
        now_card = self.card(card["id"])  # a toggle or a subject that came while the tree was read: press again
        if now_card is None or now_card["state"] != "shown" or (now_card["files"], now_card["subject"]) != \
                (card["files"], card["subject"]):
            return "Карточка изменилась, пока я её читал, — проверь и нажми ещё раз.", now_card
        moved = [f["path"] for f in chosen if snapshot.get(f["path"]) != f["digest"]]
        if moved:  # what the owner saw is not what would go: the card again, with the files as they are now
            toast, fresh = await self.open(card["session"], telegram=False)
            named = ", ".join(map(short, moved[:2])) + (f" и ещё {len(moved) - 2}" if len(moved) > 2 else "")
            return (f"Файлы изменились после того, как карточка открылась: {named}. Проверь и нажми ещё раз."
                    if fresh else toast), self.card(card["id"])
        now = self.platform.clock()
        busy = {**card, "state": "committing", "result": {}}
        if not self._save(busy, now):
            return GONE, None
        self._show(busy, now)
        self._commits[card["id"]] = asyncio.create_task(
            self._commit(busy, {f["path"]: f["digest"] for f in chosen}))
        return "Коммичу…", busy

    async def _commit(self, card: dict, expect: dict[str, str | None]) -> None:
        try:
            try:
                body = f"Retinue: сессия {card['session']}, карточка {card['id']}."
                # The digests the owner saw: the Base checks them again under its own lock and refuses a path whose
                # file moved; `session` goes into the commit's trailer. A Base without them only had the check above.
                params = inspect.signature(self.base.commit).parameters
                anything = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
                given = {k: v for k, v in (("expect", expect), ("session", card["session"]))
                         if anything or k in params}
                if "expect" not in given:
                    log.warning("card %s: the Base's commit takes no `expect`: the digests are not checked again",
                                card["id"])
                done = await self.base.commit(list(expect), self.subject(card), body, **given)
            except Exception as exc:  # said without its words: a path or a line of the hook may say anything
                log.error("card %s: the commit failed: %s", card["id"], type(exc).__name__)
                why = UNAVAILABLE if isinstance(exc, OSError) else f"ошибка платформы ({type(exc).__name__})."
                result = {"sha": None, "said": f"Коммит не удался. {why} Файлы остались незакоммиченными."}
            else:
                result = self._result(done)
            self._finish(card, "committed" if result.get("sha") else "failed", result, self.platform.clock(),
                         ("committing",))
            await self.settle_lists()
            self._commits.pop(card["id"], None)
            await self.platform.drain()  # a run the commit held back (`before_start`) goes now
        finally:
            self._commits.pop(card["id"], None)

    @staticmethod
    def _result(done) -> dict:
        """The commit's answer (`base.Committed`) as the card says it."""
        files = [f"{c.status} {c.path}" for c in (done.files or [])]
        refused = [*(f"конфликт: {line(c, 200)}" for c in done.conflicts or []),
                   *(line(r, 200) for r in done.refused or []),
                   *(f"не в Базе: {line(p, 200)}" for p in done.left_out or [])]
        if done.sha:
            said = f"Закоммичено: {str(done.sha)[:7]} · {files_word(len(files))}."
        else:
            said = "Ничего не закоммичено."
        if refused:
            said += " Не вошли и остались незакоммиченными:\n" + "\n".join(f"• {r}" for r in refused)
        return {"sha": done.sha, "files": files, "refused": refused, "said": said}

    async def settle_lists(self) -> None:
        """After a commit: every list checked against the clone now, and the pings brought up to date."""
        async with self._lock:
            try:
                dirty = await self._dirty()
            except Exception as exc:
                log.error("the Base's state was not read (%s)", type(exc).__name__)
                return
            for session in self._prune(dirty) | {s for (s,) in self.store.db.execute("SELECT session FROM base_pings")}:
                self.ping(session)

    async def idle(self) -> None:
        """The commits running now, finished (tests, shutdown)."""
        while self._commits:
            await asyncio.gather(*self._commits.values(), return_exceptions=True)

    # --- the clock and a restart ------------------------------------------------------------------------------------

    def tick(self, now: float) -> None:
        """A card unpressed for TTL_S expires, like an approval; a card left `committing` with no commit running is
        settled again (`recover`) — the Base may have been unreadable, or off, when the platform started."""
        stuck = [c for (c,) in self.store.db.execute("SELECT id FROM base_cards WHERE state = 'committing'")
                 if c not in self._commits]
        if stuck and self.base is not None and (self._recovering is None or self._recovering.done()):
            try:
                self._recovering = asyncio.get_running_loop().create_task(self.recover())
            except RuntimeError:  # no loop: called outside the platform's own clock
                pass
        for (card_id,) in self.store.db.execute("SELECT id FROM base_cards WHERE state = 'shown' AND created <= ?",
                                                (now - TTL_S,)).fetchall():
            if (card := self.card(card_id)) is not None:
                self._finish(card, "expired", {"said": "Истекла: решения не было семь дней."}, now)

    async def recover(self) -> None:
        """A card a restart left `committing`: what of it the clone no longer has uncommitted went in; the rest is
        shown again for one more press. Nothing is committed again without one."""
        if self.base is None:
            return
        left = [c for (card_id,) in self.store.db.execute("SELECT id FROM base_cards WHERE state = 'committing'")
                if card_id not in self._commits and (c := self.card(card_id)) is not None]
        if not left:
            return
        try:
            dirty = await self._dirty()
        except Exception as exc:
            log.error("the cards left committing were not settled (%s)", type(exc).__name__)
            return
        now = self.platform.clock()
        for card in left:
            chosen = [f["path"] for f in card["files"] if f["on"]]
            went = [p for p in chosen if p not in dirty]
            if not went:
                self._finish({**card, "state": "shown"}, "shown", {"said": RESTARTED}, now, ("committing",))
                continue
            said = f"Закоммичено до перезапуска платформы: {files_word(len(went))}."
            if len(went) < len(chosen):
                said += f" Остались незакоммиченными: {', '.join(p for p in chosen if p in dirty)}."
            self._finish(card, "committed", {"sha": None, "files": went, "said": said}, now, ("committing",))
        await self.settle_lists()

    # --- the app and the feed ---------------------------------------------------------------------------------------

    def face_cards(self, session: str) -> list[dict]:
        """A session's cards for its snapshot in the app: the open one and the last few decided."""
        return [self.face(c) for c in self.cards(session)[-5:]]
