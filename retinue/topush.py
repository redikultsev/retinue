"""«Запушить»: the owner's button that pushes what a session committed in one of his repositories in the room as a
branch, and opens a pull request (GitHub) or a merge request (GitLab) — never the default branch, never forced. The
repositories, their mirrors and the push itself are `code.CodeRepos` (docs/code.md); this module is who worked
where, the button on the ping and the card he decides on, in Telegram and in the app. It never runs git itself and
never touches the room.

**Who worked where.** A session on the room's lane works in a repository when its folder is in one
(`/work/<root>/<name>`, `/new code <name>`), or when any of its calls names the repository's path (a Bash `cd`, an
Edit of a file there): read from its raw lines past a cursor (`meta.push_cursor`), recorded in `push_touched`. At
the end of a turn (a `result` line) or of a run, each repository a session worked in is looked at again: its branch,
its tip and how many of its commits no branch of the remote has yet — `push_repos`, one row per repository, since a
repository has one working clone in the room whichever session committed. The look fetches the session's branch into
the platform's outbox through `CodeRepos.count`, which treats the room's repository as hostile.

**The ping.** The latest ping of such a session carries «Запушить · N» — one button per repository with N > 0 (with
its name when there are several) — beside its reply buttons and «В Базу». A press opens the card.

**The card** (`push_cards`, its buttons bound to `push/<id>`): «Запушить · <repo> · <sphere or track> · сессия <id>»,
the local branch and the remote one it goes to (`retinue/<session>-<slug>` by default; a reply of one word sets it,
a phrase sets the PR's title), the commits (subjects) and files with +/− lines, what in the work tree is not
committed («попроси сессию закоммитить» — the platform never commits for it), and «Отправить» · «Открыть дифф» ·
«Отмена». The card holds the tip's sha it was opened on; «Отправить» checks the room's branch still points there
(else the card comes back with what changed), moves `shown → pushing` once — of two presses anywhere, one wins — and
pushes in the background; `pushed` with the PR's link, or back to `shown` with why. Changes to `.github/workflows/`
are refused on the card before any push: a fine-grained token without the Workflows permission cannot push them. A
card a restart left `pushing` is settled from the remote: its branch at the card's sha → pushed (the PR is opened or
found again); otherwise shown again for one more press. A card expires after 7 days, goes stale at the session's
next ping, and «Отмена» closes it. Nothing is pushed without the owner's press: the button is reachable only through
the router's token (the bridge) and the face's login, never from a lane.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import unicodedata
from typing import TYPE_CHECKING

from . import code as codemod
from .config import BRANCH
from .code import shown
from .courier import line
from .spoken import unmarked
from .sessions import parsed
from .tobase import plural, short

if TYPE_CHECKING:
    from .platformd import Platform

log = logging.getLogger("retinue.topush")

KEY = "push/"                 # a card's buttons and notes: push/<card id>
OPEN = "push/open"            # the ping's «Запушить · N»: its digest is <session>/<repo>
SEND, DIFF, CANCEL = "send", "diff", "cancel"
LABELS = {SEND: "Отправить", DIFF: "Открыть дифф", CANCEL: "Отмена"}
TTL_S = 7 * 86400
LOOK_TIMEOUT_S = 180.0        # a card's look or a ping's count; past it the owner is told, nothing waits on it
DIFF_LIMIT = 1024 * 1024
LISTED_CHARS = 2600           # commits and files of a Telegram card; past it «…и ещё N»
STATUS_CHARS = 900
TITLE_CHARS = 120
LIVE = ("shown", "pushing")
CURSOR = "push_cursor"        # the `meta` row: the raw line up to which calls were read for repository paths
GONE = "Карточка больше не действует."
NO_CODE = "Репозитории не подключены."
REOPEN = "Карточка больше не действует — открой «Запушить» заново."
RESTARTED = "Пуш прервался перезапуском платформы — нажми «Отправить» ещё раз."
DIRTY = "есть незакоммиченные правки — попроси сессию закоммитить, в пуш они не попадут"
WORKFLOWS = "Правки .github/workflows токен не пушит — убери их из ветки или запушь сам."
HINT = "Ответь на карточку: одно слово — имя ветки, фраза — заголовок PR."


def commits_word(n: int) -> str:
    return plural(n, "коммит", "коммита", "коммитов")


def slug(text: str, cap: int = 40) -> str:
    """A branch-name part from a branch or a commit subject: ASCII letters and digits joined by `-`, cut."""
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")[:cap].rstrip("-")


class ToPush:
    def __init__(self, platform: Platform) -> None:
        self.platform, self.store = platform, platform.store
        self._lock = asyncio.Lock()          # one read of the new lines at a time; never held across git
        self._repos: dict[str, asyncio.Lock] = {}   # one look or count per repository at a time
        self._counting: dict[str, asyncio.Task] = {}  # the recounts running, by repository
        self._again: set[str] = set()        # repositories owed one more recount when the running one ends
        self._pushes: dict[str, asyncio.Task] = {}
        self._recovering: asyncio.Task | None = None
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS push_cards (
                id TEXT PRIMARY KEY,              -- 8 hex; its buttons and notes are push/<id>; its outbox ref
                session TEXT NOT NULL,
                repo TEXT NOT NULL,
                state TEXT NOT NULL,              -- shown | pushing | pushed | cancelled | stale | expired
                look TEXT NOT NULL,               -- code.Look as the card was opened on, JSON
                target TEXT NOT NULL,             -- the remote branch it goes to
                custom INTEGER NOT NULL DEFAULT 0,  -- the owner named the branch
                title TEXT NOT NULL,              -- the PR's
                body TEXT NOT NULL,
                ping INTEGER,                     -- the session's latest ping when it was opened
                result TEXT NOT NULL DEFAULT '{}',  -- {url, said}
                created REAL NOT NULL,
                at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS push_cards_session ON push_cards (session, state);
            CREATE TABLE IF NOT EXISTS push_pings (
                session TEXT PRIMARY KEY,         -- the one ping of the session that carries «Запушить»
                seq INTEGER NOT NULL,
                labels TEXT NOT NULL              -- what its buttons said, JSON: rewritten when it changes
            );
            """)
        # Repositories no longer configured (or the code off) count for no session: their rows go.
        known = list(self.code.repos) if self.code is not None else []
        self.store.db.execute(f"DELETE FROM push_repos WHERE repo NOT IN ({', '.join('?' * len(known))})", known)
        self.store.db.commit()

    # --- where the code is ----------------------------------------------------------------------------------------

    @property
    def code(self) -> codemod.CodeRepos | None:
        return getattr(self.platform, "code", None)

    def lane(self) -> str:
        cfg = getattr(self.platform.cfg, "code", None)
        return getattr(cfg, "lane", None) or "private"

    def repo_of_cwd(self, cwd: str | None) -> str | None:
        """The repository a session's folder is in: `<root>/<name>[/…]`."""
        if self.code is None or not cwd:
            return None
        parts = cwd.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == self.code.cfg.root and parts[1] in self.code.repos:
            return parts[1]
        return None

    def _named(self, text: str) -> set[str]:
        root = re.escape(self.code.cfg.root)
        return {name for name in self.code.repos
                if re.search(rf"(?<![\w.-]){root}/{re.escape(name)}(?![\w.-])", text)}

    # --- who worked where -----------------------------------------------------------------------------------------

    async def run_started(self, session: dict) -> None:
        """A run of a session in a repository's folder: it works there."""
        if (repo := self.repo_of_cwd(session.get("cwd"))) is not None and not session.get("parent"):
            self._touch(session["id"], repo)
            self.store.db.commit()

    def _touch(self, session: str, repo: str) -> None:
        self.store.db.execute("INSERT OR IGNORE INTO push_touched (session, repo, at) VALUES (?, ?, ?)",
                              (session, repo, self.platform.clock()))

    async def settle(self) -> None:
        """The calls since the cursor read for repository paths, and every repository a turn or run that ended worked
        in looked at again; then the pings brought up to date. Cheap when nothing new came."""
        if self.code is None:
            self._unping()
            return
        async with self._lock:
            repos = self._settle()
        for repo in sorted(repos):
            self.recount_soon(repo)

    def _repo_lock(self, repo: str) -> asyncio.Lock:
        return self._repos.setdefault(repo, asyncio.Lock())

    def recount_soon(self, repo: str) -> None:
        """A recount in the background: the poll that asked never waits on git, and one repository's slow look holds
        back no other session's ping. Asked again while one runs, one more follows it."""
        if repo in self._counting:
            self._again.add(repo)
            return

        async def run() -> None:
            try:
                while True:
                    self._again.discard(repo)
                    await self.recount(repo)
                    if repo not in self._again:
                        break
            finally:
                self._counting.pop(repo, None)
        self._counting[repo] = asyncio.create_task(run())

    def _settle(self) -> set[str]:
        db = self.store.db
        row = db.execute("SELECT value FROM meta WHERE key = ?", (CURSOR,)).fetchone()
        if row is None:  # the first look: from now on, not the lines from before the button
            cursor = db.execute("SELECT COALESCE(MAX(id), 0) FROM raw").fetchone()[0]
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (CURSOR, cursor))
            db.commit()
        else:
            cursor = int(row[0])
        owed: set[str] = set()   # sessions whose turn or run ended
        upto = cursor
        for raw_id, session, text in db.execute(
                "SELECT r.id, r.session, r.line FROM raw r JOIN sessions s ON s.id = r.session WHERE r.id > ? AND "
                "s.lane = ? AND s.parent IS NULL ORDER BY r.id LIMIT 5000", (cursor, self.lane())).fetchall():
            upto = raw_id
            item = parsed(text)
            kind = item.get("type")
            if kind == "assistant":
                for block in (item.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        for repo in self._named(json.dumps(block.get("input"), ensure_ascii=False)):
                            self._touch(session, repo)
            elif kind == "result" or (kind == "lane" and item.get("event") in ("exited", "stopped", "refused")):
                owed.add(session)
        if upto == cursor:
            return set()
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (CURSOR, upto))
        db.commit()
        return {r for (r,) in db.execute(f"SELECT DISTINCT repo FROM push_touched WHERE session IN "
                                         f"({', '.join('?' * len(owed))})", tuple(owed))} if owed else set()

    async def recount(self, repo: str) -> None:
        """The repository's branch, tip and unpushed commits as the room has them now, and every ping of a session
        that worked in it brought up to date."""
        if self.code is None or repo not in self.code.repos:
            return
        try:
            async with self._repo_lock(repo):
                branch, sha, n = await asyncio.wait_for(
                    asyncio.to_thread(self.code.count, repo, f"refs/retinue/heads/{repo}"), LOOK_TIMEOUT_S)
        except codemod.Absent as exc:    # no clone yet, no commit: nothing to push, nothing to say
            log.info("repo %s: not counted: %s", repo, exc)
            branch, sha, n, why = "", "", 0, ""
        except codemod.Refused as exc:   # detached, a hostile layout, a pack it will not copy: said on the ping
            log.info("repo %s: not counted: %s", repo, exc)
            branch, sha, n, why = "", "", 0, line(str(exc), 300)
        except Exception as exc:  # the type and git's words (the token never in them): the mirror or the remote
            log.warning("repo %s: not counted (%s: %s)", repo, type(exc).__name__, str(exc)[:200])
            return
        else:
            why = ""
        db = self.store.db
        db.execute("INSERT OR REPLACE INTO push_repos (repo, branch, sha, n, at, why) VALUES (?, ?, ?, ?, ?, ?)",
                   (repo, branch, sha, n, self.platform.clock(), why))
        db.commit()
        for (session,) in db.execute("SELECT session FROM push_touched WHERE repo = ?", (repo,)).fetchall():
            self.store._session_event(session)
            self.ping(session)

    def blocked(self, session: str) -> list[tuple[str, str]]:
        """(repo, why) of every repository the session worked in whose last count the room's repository refused."""
        if self.code is None:
            return []
        return [(r, w) for r, w in self.store.db.execute(
            "SELECT t.repo, r.why FROM push_touched t JOIN push_repos r ON r.repo = t.repo WHERE t.session = ? AND "
            "r.why != '' ORDER BY t.repo", (session,)) if r in self.code.repos and not self.code.repos[r].off]

    def unpushed(self, session: str) -> list[tuple[str, int]]:
        """(repo, N) of every repository the session worked in that has commits no branch of the remote has."""
        if self.code is None:
            return []
        return [(r, n) for r, n in self.store.db.execute(
            "SELECT t.repo, r.n FROM push_touched t JOIN push_repos r ON r.repo = t.repo WHERE t.session = ? AND "
            "r.n > 0 ORDER BY t.repo", (session,)) if r in self.code.repos and not self.code.repos[r].off]

    # --- the ping's buttons ---------------------------------------------------------------------------------------

    def _unping(self) -> None:
        for session, seq in self.store.db.execute("SELECT session, seq FROM push_pings").fetchall():
            self._ping_buttons(seq, None)
            self.store.db.execute("DELETE FROM push_pings WHERE session = ?", (session,))
        self.store.db.commit()

    def ping(self, session: str) -> None:
        """The session's latest ping carries «Запушить · N» per repository with N > 0, and no older ping does; an
        open card of an older ping goes stale."""
        db, now = self.store.db, self.platform.clock()
        latest = db.execute("SELECT MAX(seq) FROM notes WHERE kind = 'summary' AND session = ?", (session,)).fetchone()[0]
        counted, blocked = self.unpushed(session), self.blocked(session)
        many = len(counted) + len(blocked) > 1
        # A repository the count refused keeps a button: its press says why, as the card would.
        wanted = [(repo, f"Запушить {repo} · {n}" if many else f"Запушить · {n}") for repo, n in counted] + \
            [(repo, f"⚠ Запушить {repo}: почему нет" if many else "⚠ Запушить: почему нет") for repo, _ in blocked]
        labels = [label for _, label in wanted]
        held = db.execute("SELECT seq, labels FROM push_pings WHERE session = ?", (session,)).fetchone()
        if held is not None and (held[0] != latest or not wanted):
            self._ping_buttons(held[0], None)
            db.execute("DELETE FROM push_pings WHERE session = ?", (session,))
            held = None
        if latest is not None:
            for card in self.cards(session, ("shown",)):
                if card["ping"] is not None and card["ping"] < latest:
                    self._finish(card, "stale", {"said": "Устарела: у сессии новый итог — «Запушить» под ним."}, now)
        if wanted and latest is not None and (held is None or json.loads(held[1]) != labels):
            buttons = [[label, self.store.add_button(OPEN, "open", f"{session}/{repo}", now + TTL_S)]
                       for repo, label in wanted]
            self._ping_buttons(latest, buttons)
            db.execute("INSERT OR REPLACE INTO push_pings (session, seq, labels) VALUES (?, ?, ?)",
                       (session, latest, json.dumps(labels, ensure_ascii=False)))
        db.commit()

    def _ping_buttons(self, seq: int, buttons: list | None) -> None:
        row = self.store.db.execute("SELECT session, data FROM notes WHERE seq = ? AND kind = 'summary'",
                                    (seq,)).fetchone()
        if row is None:
            return
        data = {**json.loads(row[1]), "push": buttons}
        self.store.db.execute("UPDATE notes SET data = ? WHERE seq = ?", (json.dumps(data, ensure_ascii=False), seq))
        self.store._note("ping", row[0], {**data, "seq": seq})

    # --- the card -------------------------------------------------------------------------------------------------

    CARD = ("id", "session", "repo", "state", "look", "target", "custom", "title", "body", "ping", "result", "created",
            "at")

    def card(self, card_id: str) -> dict | None:
        row = self.store.db.execute(f"SELECT {', '.join(self.CARD)} FROM push_cards WHERE id = ?",
                                    (card_id,)).fetchone()
        if row is None:
            return None
        card = dict(zip(self.CARD, row))
        card["look"], card["result"], card["custom"] = json.loads(card["look"]), json.loads(card["result"]), \
            bool(card["custom"])
        return card

    def cards(self, session: str, states: tuple[str, ...] = (), repo: str | None = None) -> list[dict]:
        where = f" AND state IN ({', '.join('?' * len(states))})" if states else ""
        where += " AND repo = ?" if repo is not None else ""
        ids = self.store.db.execute(f"SELECT id FROM push_cards WHERE session = ?{where} ORDER BY created, rowid",
                                    (session, *states, *((repo,) if repo is not None else ()))).fetchall()
        return [self.card(card_id) for (card_id,) in ids]

    async def open(self, session_id: str, repo: str | None = None, *, telegram: bool) -> tuple[str, dict | None]:
        """«Запушить»: the card of the session's repository — its open one brought up to date, or a new one — and,
        from Telegram, a copy of it. (toast, the card as the app shows it, or None with why)."""
        if self.code is None:
            return NO_CODE, None
        session = self.store.session(session_id)
        if session is None:
            return "Нет такой сессии.", None
        if repo is None:
            mine = [r for r, _ in self.unpushed(session_id)] or \
                [r for (r,) in self.store.db.execute("SELECT repo FROM push_touched WHERE session = ? ORDER BY repo",
                                                     (session_id,))]
            repo = mine[0] if len(mine) == 1 else self.repo_of_cwd(session.get("cwd"))
            if repo is None:
                return ("Сессия работала в нескольких репозиториях — нажми «Запушить» нужного под её итогом."
                        if mine else "Сессия не работала ни в одном репозитории."), None
        if repo not in self.code.repos:
            return f"Нет такого репозитория: {repo}.", None
        if self.code.repos[repo].off:
            return f"Репозиторий {repo} выключен: {self.code.repos[repo].off}.", None
        async with self._repo_lock(repo):
            if self.cards(session_id, ("pushing",), repo):
                return "Пуш этого репозитория уже идёт.", None
            [card] = self.cards(session_id, ("shown",), repo)[-1:] or [None]
            card_id = card["id"] if card else secrets.token_hex(4)
            try:
                look = await asyncio.wait_for(asyncio.to_thread(self.code.look, repo, f"refs/retinue/cards/{card_id}"),
                                              LOOK_TIMEOUT_S)
            except TimeoutError:
                return f"{repo}: карточка не собралась за {int(LOOK_TIMEOUT_S)} с — попробуй позже.", None
            except codemod.Refused as exc:
                return f"{repo}: {exc}.", None
            except Exception as exc:  # `code.Unavailable`: git's words, never the token
                log.warning("repo %s: the card's look failed (%s: %s)", repo, type(exc).__name__, str(exc)[:200])
                return f"{repo} сейчас не читается: {line(str(exc), 200)}", None
            if self.cards(session_id, ("pushing",), repo):
                return "Пуш этого репозитория уже идёт.", None
            if look.ahead == 0 or look.unpushed == 0:
                said = f"{repo}: на ветке {shown(look.branch, 80)} " + (
                    f"нет коммитов сверх {look.base}." if look.ahead == 0 else "всё уже есть на удалённом.")
                if look.dirty:
                    said += f" {DIRTY[0].upper()}{DIRTY[1:]}."
                await asyncio.to_thread(self.code.forget, repo, f"refs/retinue/cards/{card_id}")
                return said, None
            now = self.platform.clock()
            data = _look(look)
            if card is None:
                card = self._new(card_id, session, repo, data, now)
            else:
                fresh = card["look"]["sha"] != look.sha
                moved = card["look"]["branch"] != look.branch
                card = {**card, "look": data,
                        "target": card["target"] if card["custom"] or not moved else
                        self._target(session_id, repo, data),
                        "title": card["title"] if not fresh else self._title(session_id, data),
                        "body": self._body(session_id, card_id, data), "result": {} if fresh else card["result"]}
                if not self._save(card, now):
                    return "Пуш этого репозитория уже идёт.", None
            self._show(card, now, send=telegram)
            return ("Карточка — ниже." if telegram else ""), self.face(card)

    def _new(self, card_id: str, session: dict, repo: str, look: dict, now: float) -> dict:
        latest = self.store.db.execute("SELECT MAX(seq) FROM notes WHERE kind = 'summary' AND session = ?",
                                       (session["id"],)).fetchone()[0]
        card = {"id": card_id, "session": session["id"], "repo": repo, "state": "shown", "look": look,
                "target": self._target(session["id"], repo, look), "custom": False,
                "title": self._title(session["id"], look), "body": self._body(session["id"], card_id, look),
                "ping": latest, "result": {}, "created": now, "at": now}
        self.store.db.execute(f"INSERT INTO push_cards ({', '.join(self.CARD)}) VALUES ({', '.join('?' * 13)})",
                              self._row(card))
        return card

    def _target(self, session: str, repo: str, look: dict) -> str:
        """The remote branch: the one this session's last push of the same local branch went to (a fast-forward of
        it updates its PR), else `retinue/<session>-<slug of the branch or of the first commit>`."""
        for card in reversed(self.cards(session, ("pushed",), repo)):
            if card["look"]["branch"] == look["branch"]:
                return card["target"]
        local = look["branch"]
        base = look["base"]
        words = slug(local) if local.casefold() not in {base.casefold(), "main", "master"} else ""
        words = words or slug(look["commits"][-1]["subject"] if look["commits"] else "") or "changes"
        return f"retinue/{session}-{words}"

    def _title(self, session: str, look: dict) -> str:
        if len(look["commits"]) == 1:
            return line(look["commits"][0]["subject"], TITLE_CHARS)
        title = (self.store.face_session(session) or {}).get("title") or ""
        return line(title, TITLE_CHARS) or line(look["commits"][-1]["subject"] if look["commits"] else look["branch"],
                                                TITLE_CHARS)

    @staticmethod
    def _body(session: str, card_id: str, look: dict) -> str:
        """The PR's body: each new commit's subject as code — the session's words never mention anyone, close an
        issue or load a picture there."""
        rows = [f"- `{inert(c['subject'])}` ({c['sha'][:7]})" for c in reversed(look["commits"][:30])]
        more = len(look["commits"]) - 30
        return "\n".join([*rows, *([f"- …и ещё {commits_word(more)}"] if more > 0 else []), "",
                          f"Retinue: сессия {session}, карточка {card_id}. Запушено кнопкой Владельца."])

    @staticmethod
    def _row(card: dict) -> tuple:
        return (card["id"], card["session"], card["repo"], card["state"], json.dumps(card["look"], ensure_ascii=False),
                card["target"], int(card["custom"]), card["title"], card["body"], card["ping"],
                json.dumps(card["result"], ensure_ascii=False), card["created"], card["at"])

    def _save(self, card: dict, now: float, states: tuple[str, ...] = ("shown",)) -> bool:
        cursor = self.store.db.execute(
            f"UPDATE push_cards SET state = ?, look = ?, target = ?, custom = ?, title = ?, body = ?, result = ?, at = ? "
            f"WHERE id = ? AND state IN ({', '.join('?' * len(states))})",
            (card["state"], json.dumps(card["look"], ensure_ascii=False), card["target"], int(card["custom"]),
             card["title"], card["body"], json.dumps(card["result"], ensure_ascii=False), now, card["id"], *states))
        card["at"] = now
        return cursor.rowcount == 1

    def _show(self, card: dict, now: float, *, send: bool = False) -> list[list[str]]:
        face = self.face(card)
        buttons = self._buttons(card, now)
        note = {"view": face["view"], "buttons": buttons}
        self.store._event("push", card["session"], card["id"], face)
        self.store._note("update", card["session"], note, approval=KEY + card["id"])
        if send:
            self.store._note("card", card["session"], note, approval=KEY + card["id"])
        self.store.db.commit()
        return buttons

    def _buttons(self, card: dict, now: float) -> list[list[str]]:
        """A shown card's «Отправить», «Открыть дифф», «Отмена»; none otherwise. A press is answered by the card's
        state, never by a spent button."""
        if card["state"] != "shown":
            return []
        key = KEY + card["id"]
        live = dict(self.store.live_buttons(key, now))
        return [[LABELS[a], live.get(a) or self.store.add_button(key, a, card["id"], card["created"] + TTL_S)]
                for a in (SEND, DIFF, CANCEL)]

    def face(self, card: dict) -> dict:
        """The card as the app shows it, and its `view` as Telegram does."""
        session = self.store.face_session(card["session"]) or {}
        sphere = session.get("sphere")
        place = next((f"{s.icon} {s.title}".strip() for s in self.platform.cfg.spheres if s.name == sphere), None) \
            if sphere else None
        place = place or sphere or session.get("track") or ""
        look, result = card["look"], card["result"]
        return {"id": card["id"], "session": card["session"], "repo": card["repo"], "state": card["state"],
                "place": place, "branch": look["branch"], "target": card["target"], "custom": card["custom"],
                "base": look["base"], "title": shown(card["title"], TITLE_CHARS),
                "commits": [{"sha": c["sha"], "subject": shown(c["subject"], 200),
                             "message": shown(c.get("message") or c["subject"], 8 * codemod.MAX_MESSAGE, lines=True),
                             "author": shown(c.get("author") or "", 300),
                             "committer": shown(c.get("committer") or "", 300),
                             "parents": c.get("parents") or [],
                             "files": [{"path": shown(f["path"], 300), "status": f["status"]}
                                       for f in c.get("files") or []]} for c in look["commits"]],
                "files": [{**f, "path": shown(f["path"], 300)} for f in look["files"]],
                "ahead": look["ahead"], "unpushed": look.get("unpushed", 0), "objects": look.get("objects", 0),
                "bytes": look.get("bytes", 0), "workflows": [shown(w, 300) for w in look["workflows"]],
                "dirty": [shown(d, 300) for d in look["dirty"]],
                "partial": look["partial"], "url": result.get("url") or "", "said": result.get("said") or "",
                "created": card["created"], "at": card["at"], "view": self._view(card, place)}

    def _view(self, card: dict, place: str) -> dict:
        look, state = card["look"], card["state"]
        head = [f"Запушить · {card['repo']} · {place} · сессия {card['session']}",
                f"Ветка: {shown(look['branch'], 80)} → {card['target']} (PR в {look['base']})",
                f"Заголовок PR: {shown(card['title'], TITLE_CHARS)}"]
        rows, size = [], 0
        listed = []  # what leaves, commit by commit: its subject, the rest of its message, its own files
        for c in look["commits"]:
            merge = " (слияние)" if len(c.get("parents") or []) > 1 else ""
            listed.append(f"• {c['sha'][:7]} {shown(c['subject'], 100)}{merge}")
            if c.get("author"):
                who = shown(c["author"], 100)
                listed.append(f"  автор: {who}" + ("" if c.get("committer") == c["author"] else
                                                    f"; коммитил: {shown(c.get('committer') or '', 100)}"))
            message = c.get("message") or ""
            rest = [row for row in message.split("\n")[1:] if row.strip()]
            listed += [f"  {shown(row, 100)}" for row in rest[:6]]
            if len(rest) > 6 or any(len(shown(row, 10_000)) > 100 for row in message.split("\n")) or \
                    len(shown(message.split("\n", 1)[0], 10_000)) > 100:
                listed.append("  (сообщение обрезано, полностью — в «Открыть дифф»)")
            listed += [f"  {f['status']} {shown(f['path'], 120)}" for f in c.get("files") or []]
        for n, row in enumerate(listed):
            if size + len(row) > LISTED_CHARS:
                rows.append(f"…и ещё {plural(len(listed) - n, 'строка', 'строки', 'строк')} — в приложении")
                break
            rows.append(row)
            size += len(row) + 1
        status = [f"Уходит: {commits_word(look.get('unpushed', look['ahead']))}, "
                  f"{plural(look.get('objects', 0), 'объект', 'объекта', 'объектов')}, {_size(look.get('bytes', 0))}. "
                  f"В PR: {commits_word(look['ahead'])}, {plural(len(look['files']), 'файл', 'файла', 'файлов')}."]
        if look["workflows"]:
            status.append(WORKFLOWS)
        if look["dirty"]:
            named = ", ".join(shown(short(d), 60) for d in look["dirty"][:3]) + (f" и ещё {len(look['dirty']) - 3}"
                                                                 if len(look["dirty"]) > 3 else "")
            status.append(f"В рабочей папке {DIRTY}: {named}.")
        if look["partial"]:
            status.append("Рабочая папка проверена не вся — она слишком большая.")
        if state == "shown":
            status.append(HINT)
        elif state == "pushing":
            status.append("Отправляю…")
        if card["result"].get("url"):
            status.append(f"PR: {card['result']['url']}")
        if card["result"].get("said"):
            status += card["result"]["said"].split("\n")
        kept, size = [], 0
        for n, row in enumerate(status):
            if size + len(row) > STATUS_CHARS:
                kept.append(f"…и ещё {plural(len(status) - n, 'строка', 'строки', 'строк')} — в приложении")
                break
            kept.append(row)
            size += len(row) + 1
        return {"head": head, "body": "\n".join(rows).strip(), "status": "\n".join(kept), "link": []}

    def _finish(self, card: dict, state: str, result: dict, now: float, states: tuple[str, ...] = ("shown",)) -> bool:
        card = {**card, "state": state, "result": result}
        if not self._save(card, now, states):
            return False
        self._show(card, now)
        if state not in LIVE and self.code is not None and state != "pushed":
            self._forget(card)
        return True

    def _forget(self, card: dict) -> None:
        try:
            asyncio.get_running_loop().run_in_executor(None, self.code.forget, card["repo"],
                                                       f"refs/retinue/cards/{card['id']}")
        except RuntimeError:  # no loop: a tick outside the platform's own clock
            pass

    # --- the owner's presses and replies ------------------------------------------------------------------------

    async def press(self, bound: dict) -> dict:
        if bound["approval"] == OPEN:
            session, _, repo = str(bound["digest"]).partition("/")
            toast, _ = await self.open(session, repo or None, telegram=True)
            return {"toast": toast}
        card = self.card(bound["approval"][len(KEY):])
        if card is None:
            return {"toast": GONE}
        action = bound["action"]
        if action == DIFF:
            return {"toast": "Дифф — файлом.", "document": {"name": f"push-{card['id']}.diff",
                                                           "text": await self.diff(card)}}
        if action == SEND:
            toast, card = await self.send(card)
        elif action == CANCEL:
            toast, card = self.cancel(card)
        else:
            return {"toast": GONE}
        if card is None:
            return {"toast": toast}
        return {"toast": toast, "view": self.face(card)["view"], "keep": self._buttons(card, self.platform.clock())}

    def reply(self, key: str, session_id: str, text: str) -> str | None:
        """The owner's reply to a card's message: one word is the remote branch, a phrase the PR's title. A card no
        longer shown takes it too, and says so — it never goes into the session. None: not a push card."""
        if not key.startswith(KEY):
            return None
        card = self.card(key[len(KEY):]) if key != OPEN else None
        if card is None or card["session"] != session_id or card["state"] != "shown":
            return REOPEN
        text = line(unmarked(text), 1000)
        if text and " " not in text and BRANCH.fullmatch(text):
            return self.set_target(card, text)[0]
        return self.set_title(card, text)[0]

    def set_target(self, card: dict, branch: str) -> tuple[str, dict | None]:
        if card["state"] != "shown":
            return GONE, None
        if self.code is not None and (why := self.code.branch_ok(card["repo"], branch)):
            return f"Ветка не подходит: {why}.", card
        card = {**card, "target": branch, "custom": True, "result": {}}
        now = self.platform.clock()
        if not self._save(card, now):
            return GONE, None
        self._show(card, now)
        return f"Ветка: {branch}", card

    def set_title(self, card: dict, text: str) -> tuple[str, dict | None]:
        if card["state"] != "shown":
            return GONE, None
        title = line(text, TITLE_CHARS)
        if not title:
            return "Пустой заголовок не беру.", card
        card = {**card, "title": title}
        now = self.platform.clock()
        if not self._save(card, now):
            return GONE, None
        self._show(card, now)
        return f"Заголовок PR: «{title}»", card

    def cancel(self, card: dict) -> tuple[str, dict | None]:
        if not self._finish(card, "cancelled", {"said": "Отменено. Коммиты остались в репозитории сессии."},
                            self.platform.clock()):
            return GONE, None
        return "Отменено.", self.card(card["id"])

    async def diff(self, card: dict) -> str:
        look = card["look"]
        if self.code is None:
            return ""
        try:
            return await asyncio.to_thread(self.code.diff, card["repo"], look["sha"], DIFF_LIMIT)
        except Exception as exc:
            return f"Дифф не получился: {type(exc).__name__}\n"

    async def send(self, card: dict) -> tuple[str, dict | None]:
        """«Отправить»: the card's commits, exactly as it showed them, pushed in the background — once, whichever place
        pressed it and however often."""
        if card["state"] == "pushing":
            return "Отправляю — подожди.", None
        if card["state"] == "pushed":
            return f"Уже отправлено{': ' + card['result']['url'] if card['result'].get('url') else '.'}", None
        if card["state"] != "shown":
            return GONE, None
        if self.code is None:
            return NO_CODE, None
        if card["look"]["workflows"]:
            return WORKFLOWS, None
        if why := self.code.branch_ok(card["repo"], card["target"]):
            return f"Не пушу: {why}.", None
        try:
            branch, sha = await asyncio.to_thread(self.code.head, card["repo"])
            there = await asyncio.to_thread(self.code.remote_sha, card["repo"], card["target"])
        except Exception as exc:
            return f"Репозиторий сессии или удалённый не читается: {line(str(exc), 200)}", None
        ours = {c["target"] for c in self.cards(card["session"], ("pushed",), card["repo"])}
        if there is not None and card["target"] not in ours:
            return (f"Ветка {card['target']} на удалённом уже есть и не от этой сессии — ответь на карточку другим "
                    "именем."), None
        if sha != card["look"]["sha"] or branch != card["look"]["branch"]:
            toast, fresh = await self.open(card["session"], card["repo"], telegram=False)
            return ("Ветка изменилась после карточки — проверь и нажми ещё раз." if fresh else toast), \
                self.card(card["id"])
        # The remote as it is now: what leaves is counted against it again, and must be exactly what the card showed.
        try:
            await asyncio.to_thread(self.code.fetch, card["repo"], codemod.FRESH_TIMEOUT_S)
            now_leaving = await asyncio.to_thread(self.code.leaving, card["repo"], f"refs/retinue/cards/{card['id']}",
                                                  sha)
        except Exception as exc:
            return f"Удалённый репозиторий не читается: {line(str(exc), 200)}", None
        if now_leaving != card["look"].get("digest"):
            toast, fresh = await self.open(card["session"], card["repo"], telegram=False)
            return ("Удалённый репозиторий изменился после карточки — обнови карточку: проверь и нажми ещё раз."
                    if fresh else toast), self.card(card["id"])
        now = self.platform.clock()
        busy = {**card, "state": "pushing", "result": {}}
        if not self._save(busy, now):
            return GONE, None
        self._show(busy, now)
        self._pushes[card["id"]] = asyncio.create_task(self._push(busy))
        return "Отправляю…", busy

    async def _push(self, card: dict) -> None:
        try:
            look, repo = card["look"], card["repo"]
            try:
                done = await asyncio.to_thread(self.code.push, repo, f"refs/retinue/cards/{card['id']}", look["sha"],
                                               card["target"], title=card["title"], body=card["body"])
            except Exception as exc:
                log.error("card %s: the push failed: %s", card["id"], type(exc).__name__)
                done = codemod.Pushed(False, said=f"Пуш не прошёл: {line(str(exc), 300)}")
            await self._pushed(card, done)
        finally:
            self._pushes.pop(card["id"], None)

    async def _pushed(self, card: dict, done: codemod.Pushed, states: tuple[str, ...] = ("pushing",)) -> None:
        """The push's end on the card: `pushed` with the PR, or `shown` again with why. Then the mirror learns the
        branch, so the ping's N goes down."""
        now = self.platform.clock()
        if not done.ok:
            self._finish({**card, "state": "shown"}, "shown", {"said": done.said}, now, states)
            return
        url, said = done.url, done.said
        if not url:
            url, said = await self.code.open_pr(card["repo"], card["target"], card["title"], card["body"])
            if not url and not said and self.code.repos[card["repo"]].host == "gitlab.com":
                said = "Merge request открыт на GitLab."
        result = {"url": url, "said": "\n".join(filter(None, [f"Запушено: {card['target']} · "
                                                               f"{commits_word(card['look']['ahead'])}.", said]))}
        if self._finish(card, "pushed", result, now, states):
            # The answer to his press as a message of its own: the card above is only rewritten, which he may not see.
            self.store._note("card", card["session"], {
                "view": {"head": [f"Запушено · {card['repo']} · сессия {card['session']}"],
                         "body": f"PR: {url}" if url else f"Ветка: {card['target']}",
                         "status": said if said and not url else "", "link": []},
                "buttons": []}, approval=f"{KEY}{card['id']}/done")
            self.store.db.commit()
        try:
            await asyncio.to_thread(self.code.fetch, card["repo"])
        except Exception as exc:
            log.warning("repo %s: the mirror's fetch after a push failed: %s", card["repo"], type(exc).__name__)
        await self.recount(card["repo"])
        self._forget(card)

    async def idle(self) -> None:
        """The pushes and recounts running now, finished (tests, shutdown)."""
        while self._pushes or self._counting:
            await asyncio.gather(*self._pushes.values(), *self._counting.values(), return_exceptions=True)

    def tick(self, now: float) -> None:
        stuck = [c for (c,) in self.store.db.execute("SELECT id FROM push_cards WHERE state = 'pushing'")
                 if c not in self._pushes]
        if stuck and self.code is not None and (self._recovering is None or self._recovering.done()):
            try:
                self._recovering = asyncio.get_running_loop().create_task(self.recover())
            except RuntimeError:
                pass
        for (card_id,) in self.store.db.execute("SELECT id FROM push_cards WHERE state = 'shown' AND created <= ?",
                                                (now - TTL_S,)).fetchall():
            if (card := self.card(card_id)) is not None:
                self._finish(card, "expired", {"said": "Истекла: решения не было семь дней."}, now)

    async def recover(self) -> None:
        """A card a restart left `pushing`: the remote's branch at the card's sha means the push landed — the PR is
        opened or found again; otherwise it is shown again for one more press. Never pushed without one."""
        if self.code is None:
            return
        left = [c for (card_id,) in self.store.db.execute("SELECT id FROM push_cards WHERE state = 'pushing'")
                if card_id not in self._pushes and (c := self.card(card_id)) is not None]
        for card in left:
            try:
                remote = await asyncio.to_thread(self.code.remote_sha, card["repo"], card["target"])
            except Exception as exc:
                log.warning("card %s: the remote was not read (%s); tried again on the clock", card["id"],
                            type(exc).__name__)
                continue
            if remote == card["look"]["sha"]:
                await self._pushed(card, codemod.Pushed(True))
            else:
                self._finish({**card, "state": "shown"}, "shown", {"said": RESTARTED}, self.platform.clock(),
                             ("pushing",))

    def face_cards(self, session: str) -> list[dict]:
        return [self.face(c) for c in self.cards(session)[-5:]]


def _look(look: codemod.Look) -> dict:
    return {"repo": look.repo, "branch": look.branch, "sha": look.sha, "base": look.base, "base_sha": look.base_sha,
            "merge_base": look.merge_base, "ahead": look.ahead, "unpushed": look.unpushed, "commits": look.commits,
            "files": look.files, "workflows": look.workflows, "dirty": look.dirty, "partial": look.partial,
            "objects": look.objects, "bytes": look.bytes, "digest": look.digest}


def inert(text: str) -> str:
    """A subject inside a Markdown code span: its own backticks out, one line."""
    return " ".join(shown(text, 120).replace("`", "'").split())


def _size(n: int) -> str:
    return f"{n} Б" if n < 1024 else f"{n / 1024:.0f} КБ" if n < 2**20 else f"{n / 2**20:.1f} МБ".replace(".", ",")
