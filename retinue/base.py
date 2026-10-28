"""The owner's knowledge base (the Base) in a lane: a clone of the hub's `main` at `/work/<tree>` of lane-private,
kept fresh by the platform, committed by the owner's button (docs/base.md).

The sessions edit the clone's files; the platform — code, not the model — brings the hub's changes in (`refresh`) and
turns the files the owner picked into one commit on top of the hub's `main`, checked and pushed with his rights
(`commit`). The clone's files are the lane's and therefore hostile input here:

- **Git never looks at the tree.** The clone's repository lives in the platform's own volume, every git call names it
  (`--git-dir`) and never a work tree: no `.gitattributes`, `.gitmodules`, hook or config in the tree is ever read by
  git, and git never writes into it. What git would do with the tree — hash a file, write a file, delete one, find
  what changed — is done here, in Python, through directory descriptors.
- **Nothing is followed.** Every folder on a path is opened with `O_NOFOLLOW`, every file with `O_NOFOLLOW |
  O_NONBLOCK` and checked from its descriptor to be a regular file: a link, a FIFO, a device or a socket is named as
  what it is, never read through, never waited on. A write is a new file in the folder renamed over the old name.
- **Nothing in it runs here.** A commit never carries a path the Base's check executes (`protect`: the scripts, the
  justfile, any `.py` or `.sh`) nor a hidden one (`.gitattributes`, `.claude/…`): the hub runs its lint, the owner's
  own script, in this process's container when the platform pushes.
- **Bounded.** Files over `max_file_mb` are not read (their digest is their size and time); the walk stops at
  `MAX_ENTRIES` entries, folders counted, and `MAX_DEPTH` folders; a diff side over `DIFF_MAX` is not diffed.

The platform's uid is not the router's, so the hub's pre-receive takes its push for the owner's (Р-Б): the author is
the owner from `platform.yaml`, and a trailer says the platform pushed it on his button.
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import hashlib
import itertools
import json
import logging
import os
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import kbcheck
from .config import BaseConfig

log = logging.getLogger("retinue.base")

BRANCH_REF = "refs/heads/main"
PUSH_TRIES = 4
MAX_ENTRIES = 50_000          # a tree with more entries is not walked further (said in the log)
MAX_DEPTH = 40
DIFF_MAX = 512 * 2**10        # a side of a diff bigger than this is named, not diffed: difflib is quadratic
RACY_S = 2.0                  # a file changed this recently is hashed again next time: its time may not move
TEMP_PREFIX = ".retinue-"     # the platform's own temporary files in the tree, renamed over the real name
GIT_TIMEOUT_S = 300           # a push waits for the hub's lint (its own timeout is 120 s)
IGNORED_DIRS = ("out", "inbox")   # step 1: files for the owner and his attachments land in the session's cwd
OTHER_SIDE = "менялся и здесь, и в Базе — правки не сливаются сами"

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)


@dataclass
class Change:
    path: str
    status: str   # "A" | "M" | "D"


@dataclass
class Committed:
    """What became of the owner's press. `files`: what the commit `sha` changed in the Base (None: no commit).
    `conflicts`: paths changed both here and in the hub that do not merge — they stay uncommitted. `refused` and
    `left_out`: one line per path, «<path>: <why>», in Russian; `refused` may also hold a line about the whole change
    (the Base's own check: «just check: …»). Left out is what never goes to the Base by design (ignored, outside it,
    already so in the hub); refused is a change the platform will not commit from the server."""
    sha: str | None
    files: list[Change] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)


@dataclass
class Refresh:
    head: str                 # the hub's commit the clone is now based on
    updated: list[str] = field(default_factory=list)   # files this refresh wrote, merged or deleted in the tree
    held: list[str] = field(default_factory=list)      # dirty here and changed in the hub: left as they are


class Unavailable(OSError):
    """The clone cannot be worked on now: its folder is missing, is not the one the lane has, or the hub is not there.
    An OSError, so a caller that guards file work already catches it."""


class Blocked(Exception):
    """One path cannot be written or read safely: a link on the way, a folder in its place, changed meanwhile."""


@dataclass
class _State:
    """A path in the tree as it is now."""
    kind: str                       # absent | file | big | link | dir | other | blocked
    data: bytes | None = None
    key: tuple = ()                 # (size, mtime_ns, ino) of a file: the write checks it did not change since
    sha: str | None = None


def clean_path(path: object) -> str | None:
    """A path inside the Base as `/`-separated segments, or None: no absolute path, `.`, `..`, empty segment, NUL or
    control character."""
    if not isinstance(path, str) or not path or path.startswith("/") or len(path) > 4096:
        return None
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts) or any(ord(c) < 32 or c == "\x7f" for c in path):
        return None
    return path


def _git_dir_name(part: str) -> bool:
    """A segment git itself would treat as its own folder on some file system: `.git`, `.GIT`, `.git.` and the like."""
    return part.rstrip(". ").casefold() == ".git" or part.casefold().startswith("git~")


class _Ignore:
    """What is not the Base's: the policy's `excluded` and the session conventions (`out/`, `inbox/`) — never
    listed, never committed — and the root `.gitignore` of the clone's HEAD (the owner's, from the hub), which hides
    untracked files only, as in git. `.gitignore` lines are read as globs: `!` lines are skipped, so it hides at most
    a little more than git would."""

    def __init__(self, excluded: list[str], gitignore: str = "") -> None:
        self.excluded = excluded
        self.patterns: list[tuple[str, bool]] = []      # (glob, folders only)
        for line in gitignore.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!")):
                continue
            only_dirs = line.endswith("/")
            line = line.rstrip("/")
            anchored = "/" in line
            line = line.lstrip("/")
            if line:
                self.patterns.append((line if anchored else f"**/{line}", only_dirs))

    def hard(self, path: str, is_dir: bool = False) -> bool:
        parts = path.split("/")
        if any(p in IGNORED_DIRS for p in (parts if is_dir else parts[:-1])):
            return True
        return kbcheck.any_match(path, self.excluded) or (is_dir and kbcheck.any_match(path + "/", self.excluded))

    def soft(self, path: str, is_dir: bool = False) -> bool:
        parts = path.split("/")
        for depth in range(1, len(parts) + 1):
            prefix, folder = "/".join(parts[:depth]), depth < len(parts) or is_dir
            if any(kbcheck.match(prefix, glob) for glob, only_dirs in self.patterns if folder or not only_dirs):
                return True
        return False


class BaseRepo:
    """The clone: its files at `cfg.mount` (the lane's `/work/<tree>`, read-write here), its repository at
    `cfg.git_dir` (the platform's volume). `changed`, `snapshot` and `diff` read and may run in any thread at any
    time; `refresh` and `commit` run one at a time (a lock) in a worker thread."""

    def __init__(self, cfg: BaseConfig, lane_view: str = "") -> None:
        self.cfg = cfg
        self.tree_rel = cfg.tree
        self.tree, self.git_dir, self.hub = Path(cfg.mount), Path(cfg.git_dir), cfg.hub
        self.lane_view = lane_view      # the same folder as the lane has it, through the platform's read-only mount
        self.max_file = cfg.max_file_mb * 2**20
        self._lock = threading.Lock()         # refresh and commit: one at a time
        self._ready = threading.Lock()        # the first look at the repository, from whichever thread comes first
        self._db = threading.Lock()
        self._cache: dict[str, tuple[tuple, str]] = {}
        self._head: str | None = None
        self._head_files: dict[str, tuple[str, str]] = {}     # path -> (mode, blob)
        self._head_dirs: frozenset[str] = frozenset()
        self._ignore = _Ignore([])
        self._policy: kbcheck.Policy | None = None
        self._sha = hashlib.sha1
        name, _, email = cfg.author.partition("<")
        tmp = self.git_dir / "tmp"
        # Only what git needs: none of the platform's secrets reach git, the hub's hook or the Base's lint it runs.
        self.env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(self.git_dir),
                    "LANG": "C.UTF-8", "TMPDIR": str(tmp), "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_CONFIG_GLOBAL": str(self.git_dir / "platform.gitconfig"),
                    "GIT_AUTHOR_NAME": name.strip(), "GIT_AUTHOR_EMAIL": email.rstrip(">").strip(),
                    "GIT_COMMITTER_NAME": name.strip(), "GIT_COMMITTER_EMAIL": email.rstrip(">").strip()}
        self.repo = ["git", f"--git-dir={self.git_dir}", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                     "-c", "core.attributesFile=/dev/null", "-c", "core.excludesFile=/dev/null"]
        self.db: sqlite3.Connection | None = None
        # Whether a run that can write the tree is open now: callable from any thread (the platform sets it). While
        # it says so, nothing but a commit's own paths is written into the tree.
        self.busy: Callable[[], bool] = lambda: False

    # --- the contract -------------------------------------------------------------------------------------------

    def changed(self) -> list[Change]:
        """Uncommitted against the clone's HEAD, by path: what the walk finds that HEAD has not (A), has otherwise
        (M — a link or a FIFO in a file's place too) or misses (D). Ignored and excluded paths are left out. Empty
        before the first refresh."""
        self._ensure()
        if self._head is None:
            return []
        found, head, held = self._walk(), self._head_files, self._held()
        out = []
        for path in sorted(found.keys() | head.keys()):
            now, was = found.get(path), head.get(path)
            if path in held and (now or "") == held[path]:
                continue  # the hub's change waits for an idle refresh; nothing of a session's is here
            if now is None:
                out.append(Change(path, "D"))
            elif was is None:
                out.append(Change(path, "A"))
            elif now != was[1]:
                out.append(Change(path, "M"))
        return out

    def snapshot(self) -> dict[str, str]:
        """Path → digest of every file of the tree that is not ignored: a file's git blob id; `link:`, `other:` or
        `big:` for what is not a readable regular file. Hashes come from a stat cache."""
        self._ensure()
        return self._walk()

    def diff(self, paths: list[str], limit: int) -> str:
        """A unified diff of `paths` against what the clone's edit started from (HEAD, or the base remembered for a
        held file), at most `limit` characters: the owner's «Открыть»."""
        self._ensure()
        held = self._held()
        out, used = [], 0
        for path in dict.fromkeys(p for p in map(clean_path, paths) if p):
            base = held.get(path, self._head_files.get(path, ("", ""))[1]) or None
            old = self._blob(base) if base else None
            state = self._read(path)
            new = state.data if state.kind == "file" else None
            if state.kind not in ("file", "absent"):
                text = f"diff {path}\n{_what(state.kind)}: не показывается\n"
            elif old == new:
                continue
            elif max(len(old or b""), len(new or b"")) > DIFF_MAX:
                text = f"diff {path}\nфайл большой ({max(len(old or b''), len(new or b'')) // 1024} КБ): дифф не показан\n"
            elif (old and b"\0" in old[:8000]) or (new and b"\0" in new[:8000]):
                text = f"Binary files a/{path} and b/{path} differ\n"
            else:
                text = "".join(difflib.unified_diff(
                    _lines(old), _lines(new), f"a/{path}" if old is not None else "/dev/null",
                    f"b/{path}" if new is not None else "/dev/null"))
            if used + len(text) > limit:
                out.append(text[:max(0, limit - used)] + "\n… дифф обрезан\n")
                break
            out.append(text)
            used += len(text)
        return "".join(out)

    async def refresh(self, wait: bool = True) -> Refresh | None:
        """Bring the clone up to the hub's `main`: a file clean here takes the hub's, a dirty one is merged with it
        (`git merge-file`) when that is clean and left as it is otherwise — its base remembered, so a later commit
        merges against it instead of undoing the hub's change. The first one clones. Nothing at all while `busy()`
        says a run can write the tree. `wait=False`: None at once when a commit or a refresh holds the clone."""
        return await asyncio.to_thread(self._locked, self._refresh, wait=wait)

    async def commit(self, paths: list[str], subject: str, body: str, expect: dict[str, str] | None = None,
                     session: str = "") -> Committed:
        """`paths` of the tree, as they are now, in one commit on top of the hub's `main`, pushed with the owner's
        rights; nothing else of the tree is touched. `expect`: path → the digest `snapshot()` gave when the owner saw
        the card (None or '' : no file there); a path that is not that any more is refused, the rest goes. `session`: named in
        the commit's trailer."""
        expect = {path: digest or "" for path, digest in (expect or {}).items()}  # None or '' : no file there
        return await asyncio.to_thread(self._locked, self._commit, paths, subject, body, session, expect)

    # --- the repository -----------------------------------------------------------------------------------------

    def _locked(self, work, *args, wait: bool = True):
        if not self._lock.acquire(blocking=wait):
            return None
        try:
            self._ensure()
            return work(*args)
        finally:
            self._lock.release()

    def _ensure(self) -> None:
        """The repository looked at once, and the tree checked to be the lane's on every call."""
        with self._ready:
            self._prepare()
        self._same_tree()

    def _run(self, *args: str, data: bytes | None = None, check: bool = True,
             env: dict | None = None) -> subprocess.CompletedProcess:
        run = subprocess.run([*self.repo, *args], input=data, capture_output=True, env={**self.env, **(env or {})},
                             timeout=GIT_TIMEOUT_S)
        if check and run.returncode != 0:
            raise subprocess.CalledProcessError(run.returncode, ["git", *args], run.stdout, run.stderr)
        return run

    def _out(self, *args: str) -> str:
        return self._run(*args).stdout.decode().strip()

    def _prepare(self) -> None:
        """Once per process: the repository (made if missing), the state, the HEAD this clone is on."""
        if self.db is not None:
            return
        (self.git_dir / "tmp").mkdir(parents=True, exist_ok=True)
        # The hub belongs to the owner, not to this uid: git refuses it unless `safe.directory` names it — in a global
        # config of our own, inside the repository's folder.
        Path(self.env["GIT_CONFIG_GLOBAL"]).write_text(f"[safe]\n\tdirectory = {self.hub}\n")
        if not (self.git_dir / "HEAD").is_file():
            self._run("init", "-q", "--bare", "-b", "main")
        self._run("config", "remote.origin.url", self.hub)
        self._run("config", "core.bare", "true")
        self._run("config", "gc.auto", "0")
        self._policy = kbcheck.Policy.load(self.cfg.policy or os.path.join(self.hub, "hooks", kbcheck.POLICY_FILE))
        if self._out("rev-parse", "--show-object-format") == "sha256":
            self._sha = hashlib.sha256
        # What never goes into a commit, written where git keeps it — git never reads the tree, so this says it to a
        # person looking at the repository; `_Ignore` is what applies it.
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "exclude").write_text(
            "".join(f"{d}/\n" for d in IGNORED_DIRS) + "".join(f"{p}\n" for p in self._policy.excluded))
        db = sqlite3.connect(self.git_dir / "base.sqlite", check_same_thread=False)
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS held (
                path TEXT PRIMARY KEY,
                base TEXT NOT NULL,      -- the blob the tree's edit started from ('' : the file was new here)
                since REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,      -- refresh | commit
                head TEXT NOT NULL,
                detail TEXT NOT NULL     -- json: what moved
            );
            """)
        db.commit()
        self.db = db
        head = self._run("rev-parse", "--verify", "-q", f"{BRANCH_REF}^{{commit}}", check=False)
        self._load_head(head.stdout.decode().strip() if head.returncode == 0 else None)

    def _same_tree(self) -> None:
        """The folder this process writes is the one the lane works in: the lane may move `/work/<tree>` away and make
        a new one, which this mount (bound at start) would not follow."""
        try:
            mine = os.stat(self.tree)
        except OSError:
            raise Unavailable(f"the clone's folder {self.tree} is not there") from None
        if not stat.S_ISDIR(mine.st_mode):
            raise Unavailable(f"{self.tree} is not a folder")
        if self.lane_view:
            try:
                theirs = os.lstat(self.lane_view)
            except OSError:
                raise Unavailable(f"the lane has no {self.lane_view}") from None
            if (theirs.st_dev, theirs.st_ino) != (mine.st_dev, mine.st_ino):
                raise Unavailable(f"{self.lane_view} is no longer the folder mounted at {self.tree}: restart the "
                                  "platform")

    def _load_head(self, head: str | None) -> None:
        files: dict[str, tuple[str, str]] = {}
        if head:
            raw = self._run("ls-tree", "-r", "-z", "--full-tree", head).stdout.decode("utf-8", "surrogateescape")
            for entry in filter(None, raw.split("\0")):
                meta, _, path = entry.partition("\t")
                mode, kind, sha = meta.split()
                if kind == "blob":
                    files[path] = (mode, sha)
        dirs = {"/".join(p.split("/")[:i]) for p in files for i in range(1, p.count("/") + 1)}
        gitignore = self._blob(files[".gitignore"][1]).decode("utf-8", "replace") if ".gitignore" in files else ""
        self._ignore = _Ignore(self._policy.excluded if self._policy else [], gitignore)
        self._head, self._head_files, self._head_dirs = head, files, frozenset(dirs)

    def _blob(self, sha: str) -> bytes:
        return self._run("cat-file", "blob", sha).stdout

    def _hub_head(self) -> str:
        run = subprocess.run(["git", f"--git-dir={self.hub}", "rev-parse", "--verify", "-q", f"{BRANCH_REF}^{{commit}}"],
                             capture_output=True, env=self.env, timeout=GIT_TIMEOUT_S)
        if run.returncode != 0:
            raise Unavailable(f"the hub {self.hub} has no {BRANCH_REF}")
        return run.stdout.decode().strip()

    def _fetch(self) -> str:
        self._run("fetch", "-q", "--no-tags", "origin", f"+{BRANCH_REF}:refs/remotes/origin/main")
        return self._out("rev-parse", "refs/remotes/origin/main^{commit}")

    def _held(self) -> dict[str, str]:
        if self.db is None:
            return {}
        with self._db:
            return {path: base for path, base in self.db.execute("SELECT path, base FROM held")}

    def _hold(self, path: str, base: str | None) -> None:
        """`base` None: the path is clean now, nothing to remember. A name that is not UTF-8 cannot be kept (nor
        committed): it stays as the tree has it."""
        if not _utf8(path):
            return
        with self._db:
            if base is None:
                self.db.execute("DELETE FROM held WHERE path = ?", (path,))
            else:
                self.db.execute("INSERT OR IGNORE INTO held VALUES (?, ?, ?)", (path, base, time.time()))
            self.db.commit()

    def _journal(self, kind: str, head: str, detail: dict) -> None:
        with self._db:
            self.db.execute("INSERT INTO journal (ts, kind, head, detail) VALUES (?, ?, ?, ?)",
                            (time.time(), kind, head, json.dumps(detail)))
            self.db.commit()
        if any(detail.values()):
            said = json.dumps(detail, ensure_ascii=False).encode("utf-8", "replace").decode()
            log.info("base %s %s: %s", kind, head[:12], said[:2000])

    # --- refresh ------------------------------------------------------------------------------------------------

    def _refresh(self) -> Refresh:
        """Under the lock: the hub's change in — unless a run that can write the tree is open (`busy`, asked here,
        under the lock, so no refresh starts beside a run). A run that starts meanwhile stops the writes at the next
        path (`_advance`)."""
        hub = self._hub_head()
        if self.busy():
            return Refresh(self._head or hub, [], sorted(self._held()))
        if hub == self._head:  # the cheap case: the hub has not moved; what an earlier busy refresh left is brought
            updated = self._settle_held()
            if updated:
                self._journal("refresh", hub, {"updated": updated, "held": []})
            return Refresh(hub, updated, sorted(self._held()))
        upstream = self._fetch()
        updated, held = self._advance(upstream)
        self._journal("refresh", upstream, {"updated": updated, "held": held})
        return Refresh(upstream, updated, sorted(self._held()))

    def _advance(self, new: str, mine: frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
        """Move the clone from its HEAD to `new`, path by path: clean here → the hub's; dirty → merged or held. One
        path that cannot be done never stops the rest. While a run can write the tree (`busy`), only `mine` (a
        commit's own paths) is written; every other path is held with its base, and an idle refresh brings it
        (`_settle_held`). Returns (updated, newly held)."""
        old = self._head
        held = self._held()
        updated, kept = [], []
        for path, old_sha, new_sha, new_mode in self._moved(old, new):
            try:
                outcome = self._bring(path, held.get(path, old_sha or ""), new_sha, new_mode,
                                      write=path in mine or not self.busy())
            except Exception as exc:  # one path, whatever happened to it
                log.warning("base: %s not refreshed: %s", path, type(exc).__name__)
                outcome = "held"
            if outcome == "updated":
                updated.append(path)
            if outcome == "held":
                if path not in held:
                    self._hold(path, old_sha or "")
                    kept.append(path)
            else:
                self._hold(path, None)
        self._run("update-ref", BRANCH_REF, new)
        self._load_head(new)
        if not self.busy():
            updated += self._settle_held()
        return updated, kept

    def _settle_held(self) -> list[str]:
        """Every held path tried again against HEAD: one put back to what the hub has is no longer held; one left
        clean (a refresh deferred while a run was open) takes the hub's; a dirty one is merged if it now merges.
        Returns those written."""
        written = []
        for path, base in self._held().items():
            there = self._head_files.get(path)
            try:
                outcome = self._bring(path, base, there[1] if there else None, there[0] if there else "100644",
                                      write=not self.busy())
            except Exception as exc:
                log.warning("base: held %s not settled: %s", path, type(exc).__name__)
                continue
            if outcome != "held":
                self._hold(path, None)
            if outcome == "updated":
                written.append(path)
        return written

    def _moved(self, old: str | None, new: str) -> list[tuple[str, str | None, str | None, str]]:
        """(path, blob in `old`, blob in `new`, mode in `new`) of every file that differs."""
        if old is None:
            raw = self._run("ls-tree", "-r", "-z", "--full-tree", new).stdout.decode("utf-8", "surrogateescape")
            out = []
            for entry in filter(None, raw.split("\0")):
                meta, _, path = entry.partition("\t")
                mode, kind, sha = meta.split()
                out.append((path, None, sha, mode))
            return out
        raw = self._run("diff-tree", "-r", "-z", "--no-renames", "--no-commit-id", old, new).stdout
        fields = raw.decode("utf-8", "surrogateescape").split("\0")
        out = []
        for meta, path in zip(fields[0::2], fields[1::2]):
            if not meta:
                continue
            _, new_mode, old_sha, new_sha, _ = meta.lstrip(":").split()
            zero = "0" * len(old_sha)
            out.append((path, None if old_sha == zero else old_sha, None if new_sha == zero else new_sha, new_mode))
        return out

    def _bring(self, path: str, base: str, new: str | None, mode: str, write: bool = True) -> str:
        """One path of the hub's change into the tree: 'updated', 'clean' (nothing to write) or 'held' — always
        held when it would take a write and `write` is false."""
        if any(_git_dir_name(p) for p in path.split("/")) or (new is not None and mode not in ("100644", "100755")):
            return "held"  # never in a hub kbcheck guards; never written here either
        state = self._read(path)
        if (state.kind == "file" and state.sha == new) or (state.kind == "absent" and new is None):
            return "clean"
        if not write:
            return "held"
        clean_here = (state.kind == "absent" and not base) or (state.kind == "file" and state.sha == base)
        if clean_here:
            if new is None:
                self._remove(path, state)
            else:
                self._put(path, self._blob(new), mode, state)
            return "updated"
        if state.kind == "file" and base and new:
            merged = self._merge(state.data, self._blob(base), self._blob(new))
            if merged is not None:
                self._put(path, merged, mode, state)
                return "updated"
        return "held"

    def _merge(self, ours: bytes, base: bytes, theirs: bytes) -> bytes | None:
        """`git merge-file` of three texts; None on a conflict or a binary file."""
        folder = self.git_dir / "tmp" / f"merge-{secrets.token_hex(6)}"
        folder.mkdir()
        try:
            for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
                (folder / name).write_bytes(data)
            run = self._run("merge-file", "-p", "-q", str(folder / "ours"), str(folder / "base"), str(folder / "theirs"),
                            check=False)
            return run.stdout if run.returncode == 0 else None
        finally:
            for child in folder.iterdir():
                child.unlink()
            folder.rmdir()

    # --- commit -------------------------------------------------------------------------------------------------

    def _commit(self, paths: list[str], subject: str, body: str, session: str, expect: dict[str, str]) -> Committed:
        done = Committed(None)
        subject = " ".join(str(subject).split())[:200]
        if not subject:
            done.refused.append("коммит: пустая тема")
            return done
        states = {path: self._read(path) for path in self._admit(paths, done)}
        for path, state in list(states.items()):  # what the owner saw on the card is what goes, or nothing of it
            if path in expect and expect[path] != self._digest_now(path, state):
                done.refused.append(f"{path}: файл изменился после карточки — открой её заново")
                del states[path]
        for path, state in list(states.items()):
            if why := UNFIT.get(state.kind):
                done.refused.append(f"{path}: {why.format(mb=self.cfg.max_file_mb)}")
                del states[path]
        held, admitted, refused = self._held(), list(done.left_out), list(done.refused)
        message = (f"{subject}\n\n{body.strip()}\n\n" if body.strip() else f"{subject}\n\n") + \
            f"Pushed-by: retinue-platform (кнопка Владельца{f', сессия {session}' if session else ''})\n"
        try:
            upstream, results = self._fetch(), {}
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise Unavailable(f"the hub {self.hub} did not answer a fetch") from None
        try:
            for _ in range(PUSH_TRIES):  # each try against the hub as it is now: what it said before is said again
                results, done.conflicts, idle, misfit = self._resolve(states, held, upstream)
                done.left_out, done.refused = admitted + idle, refused + misfit
                commit, files = self._build(results, upstream, message, done)
                if commit is None:
                    break
                push = self._run("push", "--porcelain", "-q", "origin", f"{commit}:{BRANCH_REF}", check=False)
                if push.returncode == 0:
                    done.sha, done.files = commit, files
                    break
                said = push.stderr.decode("utf-8", "replace")
                if "push не принят" in said:  # the hub's checks: their words are the reasons
                    lines = [line.removeprefix("remote: ").strip() for line in said.splitlines()
                             if line.startswith("remote: ") and "push не принят" not in line]
                    done.refused += [line for line in lines if line] or ["хаб не принял push"]
                    break
                if "[rejected]" not in push.stdout.decode("utf-8", "replace") and "lock" not in said:
                    last = next((line for line in reversed(said.splitlines()) if line.strip()), "")
                    log.error("base: the hub refused the push: %s", last[:300])
                    done.refused.append(f"хаб не принял push: {last.strip()[:200] or 'без объяснений'}")
                    break
                upstream = self._fetch()  # the hub moved meanwhile (the router pushes after the assistant's turns)
            else:
                done.refused.append("хаб менялся быстрее, чем успевал коммит — нажми ещё раз")
        except subprocess.CalledProcessError as exc:
            log.error("base: the commit was not built: git %s exited %s", exc.cmd[1:2], exc.returncode)
            done.refused.append("коммит не собрался: ошибка git на платформе")
        except subprocess.TimeoutExpired as exc:
            # The push may have landed: the next press finds these files «уже так в Базе» and commits nothing twice.
            log.error("base: git %s timed out after %s s", exc.cmd[1:2] if isinstance(exc.cmd, list) else "?",
                      exc.timeout)
            done.refused.append("хаб не ответил, проверь и нажми ещё раз")
        if done.sha:
            for path, result in results.items():  # a merged file: the tree gets what the Base now has
                if result is not None and result[1] != states[path].data:
                    try:
                        self._put(path, result[1], result[0], states[path])
                    except (Blocked, OSError):
                        pass  # changed meanwhile: the move below merges it
        try:
            updated, kept = self._advance(done.sha or upstream, frozenset(results) if done.sha else frozenset())
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.error("base: the clone did not follow %s: %s", (done.sha or upstream)[:12], type(exc).__name__)
            updated, kept = [], []
        self._journal("commit", done.sha or upstream, {
            "commit": done.sha, "files": [[c.status, c.path] for c in done.files], "conflicts": done.conflicts,
            "refused": done.refused, "updated": updated, "held": kept})
        return done

    def _admit(self, paths: list[str], done: Committed) -> list[str]:
        """The paths a commit may carry at all; the rest said in `done`."""
        wanted = []
        for given in dict.fromkeys(paths):
            path = clean_path(given)
            if path is None:
                done.left_out.append(f"{str(given)[:200]}: не путь внутри Базы")
            elif not _utf8(path):
                done.refused.append(f"{path.encode('utf-8', 'replace').decode()}: имя не в UTF-8")
            elif "\\" in path or any(_git_dir_name(p) for p in path.split("/")):
                done.refused.append(f"{path}: служебное имя git — в Базу не идёт")
            elif self._ignore.hard(path):
                done.left_out.append(f"{path}: исключено — в Базу не идёт (out/, inbox/ или политика хаба)")
            elif kbcheck.hidden(path):
                done.refused.append(f"{path}: скрытый путь — правится только с Mac")
            elif kbcheck.any_match(path, self.cfg.protect) or kbcheck.norm(path) in {
                    kbcheck.norm(arg) for arg in self._policy.check}:  # what the hub's check names, whatever `protect` says
                done.refused.append(f"{path}: этот файл исполняется при проверке Базы — правится только с Mac")
            elif path not in self._head_files and self._ignore.soft(path):
                done.left_out.append(f"{path}: в .gitignore Базы")
            else:
                wanted.append(path)
        return wanted

    def _resolve(self, states: dict[str, _State], held: dict[str, str], upstream: str):
        """Each path against the hub's `main`: {path: (mode, bytes) or None for a deletion}, the conflicting paths,
        lines for those with nothing to commit, and lines for those that would turn a file of the Base into a folder
        or a folder into a file (`justfile/x.md` over the protected `justfile`: the index would drop it silently)."""
        theirs_files, theirs_dirs = self._listing(upstream)
        results, conflicts, idle, misfit = {}, [], [], []
        for path, state in states.items():
            parts = path.split("/")
            above = next(("/".join(parts[:i]) for i in range(1, len(parts)) if "/".join(parts[:i]) in theirs_files), None)
            if path in theirs_dirs:
                misfit.append(f"{path}: на этом месте в Базе папка — в Базу не идёт")
                continue
            if above is not None:
                misfit.append(f"{path}: на месте папки {above} в Базе файл — в Базу не идёт")
                continue
            base = held.get(path, self._head_files.get(path, ("", ""))[1]) or None
            mode, theirs = theirs_files.get(path, ("100644", None))
            ours = state.sha if state.kind == "file" else None
            if ours == theirs:
                idle.append(f"{path}: уже так в Базе")
            elif ours == base:
                idle.append(f"{path}: здесь не менялся")
            elif theirs == base:
                results[path] = (mode, state.data) if ours else None
            elif ours and base and theirs and (merged := self._merge(state.data, self._blob(base),
                                                                      self._blob(theirs))) is not None:
                results[path] = (mode, merged)
            else:
                conflicts.append(path)
        return results, conflicts, idle, misfit

    def _listing(self, rev: str) -> tuple[dict[str, tuple[str, str]], set[str]]:
        """Every file of `rev` (path → (mode, blob)) and every folder."""
        raw = self._run("ls-tree", "-r", "-t", "-z", "--full-tree", rev).stdout.decode("utf-8", "surrogateescape")
        files, dirs = {}, set()
        for entry in filter(None, raw.split("\0")):
            meta, _, path = entry.partition("\t")
            mode, kind, sha = meta.split()
            if kind == "tree":
                dirs.add(path)
            else:
                files[path] = (mode, sha)
        return files, dirs

    def _build(self, results: dict, upstream: str, message: str, done: Committed) -> tuple[str | None, list[Change]]:
        """The commit of `results` on top of `upstream`, checked the hub's way with the owner's role (the lint is the
        hub's to run): a path it refuses goes to `refused` and the rest is built again."""
        while results:
            index = self.git_dir / f"commit-{secrets.token_hex(4)}.index"
            env = {"GIT_INDEX_FILE": str(index)}
            try:
                self._run("read-tree", upstream, env=env)
                lines = []
                for path, result in results.items():
                    if result is None:
                        lines.append(f"0 {'0' * self._sha().digest_size * 2}\t{path}")
                    else:
                        sha = self._run("hash-object", "-w", "--no-filters", "--stdin", data=result[1]).stdout
                        lines.append(f"{result[0]} {sha.decode().strip()}\t{path}")
                self._run("update-index", "-z", "--index-info", data="\0".join(lines).encode() + b"\0", env=env)
                tree = self._out_env("write-tree", env=env)
            finally:
                index.unlink(missing_ok=True)
            if tree == self._out("rev-parse", f"{upstream}^{{tree}}"):
                return None, []
            stray = sorted({path for path, *_ in self._moved(upstream, tree)} - set(results))
            if stray:  # the index changed something no one chose: nothing goes
                log.error("base: a commit would also change %s", ", ".join(stray)[:500])
                done.refused.append("коммит задел бы и другие файлы Базы: " + ", ".join(stray[:5]) +
                                    (f" и ещё {len(stray) - 5}" if len(stray) > 5 else "") + " — не собран")
                return None, []
            commit = self._run("commit-tree", tree, "-p", upstream, data=message.encode()).stdout.decode().strip()
            verdict = kbcheck.check(self.repo, upstream, commit, dataclasses.replace(self._policy, check=[]),
                                    kbcheck.OWNER)
            if not verdict.refusals:
                return commit, [Change(path, "A" if old is None else "D" if new is None else "M")
                                for path, old, new, _ in self._moved(upstream, commit)]
            mine = {path for path in results if any(r.startswith(f"{path}: ") for r in verdict.refusals)}
            if not mine:
                done.refused += verdict.refusals
                return None, []
            done.refused += [r for r in verdict.refusals if any(r.startswith(f"{p}: ") for p in mine)]
            results = {p: r for p, r in results.items() if p not in mine}
        return None, []

    def _out_env(self, *args: str, env: dict) -> str:
        return self._run(*args, env=env).stdout.decode().strip()

    # --- the tree, through descriptors --------------------------------------------------------------------------

    def _root(self) -> int:
        try:
            return os.open(self.tree, _DIR)
        except OSError:
            raise Unavailable(f"the clone's folder {self.tree} does not open") from None

    def _walk(self) -> dict[str, str]:
        """Every non-ignored entry of the tree that is not a folder: path → digest. Folders are entered without
        following anything and only on the tree's own file system."""
        found: dict[str, str] = {}
        root = self._root()
        try:
            device = os.fstat(root).st_dev
            self._walk_dir(root, "", 0, device, found, [0])
        finally:
            os.close(root)
        return found

    def _walk_dir(self, fd: int, rel: str, depth: int, device: int, found: dict[str, str], seen: list[int]) -> None:
        head, head_dirs, ignore = self._head_files, self._head_dirs, self._ignore
        with os.scandir(fd) as entries:
            items = sorted(itertools.islice(entries, MAX_ENTRIES + 1), key=lambda e: e.name)
        for entry in items:
            seen[0] += 1  # files and folders alike: a tree of empty folders is no cheaper to walk
            if seen[0] > MAX_ENTRIES:
                if seen[0] == MAX_ENTRIES + 1:
                    log.warning("base: more than %d entries in the tree; the rest is not looked at", MAX_ENTRIES)
                return
            name = entry.name
            if _git_dir_name(name) or name.startswith(TEMP_PREFIX):
                continue
            path = rel + name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if ignore.hard(path, True) or (ignore.soft(path, True) and path not in head_dirs):
                    continue
                if depth >= MAX_DEPTH or st.st_dev != device:
                    found[path] = "other:folder"
                    continue
                try:
                    sub = os.open(name, _DIR, dir_fd=fd)
                except OSError:
                    continue  # swapped for a link or gone since the listing
                try:
                    if os.fstat(sub).st_dev == device:
                        self._walk_dir(sub, path + "/", depth + 1, device, found, seen)
                finally:
                    os.close(sub)
                continue
            if ignore.hard(path) or (path not in head and ignore.soft(path)):
                continue
            found[path] = self._entry(fd, name, path, st)

    def _entry(self, fd: int, name: str, path: str, st: os.stat_result) -> str:
        """The digest of one entry that is not a folder, as `snapshot` gives it."""
        if stat.S_ISLNK(st.st_mode):
            try:
                return "link:" + hashlib.sha256(os.fsencode(os.readlink(name, dir_fd=fd))).hexdigest()
            except OSError:
                return "link:"
        if not stat.S_ISREG(st.st_mode):
            return f"other:{stat.S_IFMT(st.st_mode):o}"
        return self._digest(fd, name, path, st)

    def _digest_now(self, path: str, state: _State) -> str:
        """`snapshot`'s digest of one path now ('' : no file there): from what was just read, or looked at again."""
        if state.kind == "file":
            return state.sha
        if state.kind in ("absent", "dir", "blocked"):
            return ""
        try:
            fds, name = self._dirs(path, make=False)
        except (FileNotFoundError, Blocked):
            return ""
        try:
            st = os.stat(name, dir_fd=fds[-1], follow_symlinks=False)
            return "" if stat.S_ISDIR(st.st_mode) else self._entry(fds[-1], name, path, st)
        except OSError:
            return ""
        finally:
            for fd in fds:
                os.close(fd)

    def _digest(self, fd: int, name: str, path: str, st: os.stat_result) -> str:
        key = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
        cached = self._cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        if st.st_size > self.max_file:  # by its stat, never read: a commit refuses it anyway
            return f"big:{st.st_size}:{st.st_mtime_ns}"
        try:
            sha = self._hash_file(fd, name, st.st_ino)
        except (OSError, Blocked):
            return "other:unreadable"
        if time.time() - st.st_mtime_ns / 1e9 > RACY_S:
            self._cache[path] = (key, sha)
        return sha

    def _hash_file(self, fd: int, name: str, ino: int) -> str:
        file = os.open(name, _FILE, dir_fd=fd)
        try:
            st = os.fstat(file)
            if not stat.S_ISREG(st.st_mode) or st.st_ino != ino:
                raise Blocked(name)
            digest = self._sha(b"blob %d\0" % st.st_size)
            left = st.st_size
            while left > 0:
                chunk = os.read(file, min(left, 1 << 20))
                if not chunk:
                    break
                digest.update(chunk)
                left -= len(chunk)
            return digest.hexdigest()
        finally:
            os.close(file)

    def _blob_id(self, data: bytes) -> str:
        return self._sha(b"blob %d\0" % len(data) + data).hexdigest()

    def _dirs(self, path: str, make: bool) -> tuple[list[int], str]:
        """Descriptors of the root and each folder on `path` (made when `make`), and the last segment. Raises Blocked
        when a segment is a link or not a folder, FileNotFoundError when one is missing and not to be made."""
        parts = path.split("/")
        fds = [self._root()]
        try:
            for part in parts[:-1]:
                try:
                    sub = os.open(part, _DIR, dir_fd=fds[-1])
                except FileNotFoundError:
                    if not make:
                        raise
                    os.mkdir(part, 0o775, dir_fd=fds[-1])
                    try:
                        sub = os.open(part, _DIR, dir_fd=fds[-1])
                    except OSError:
                        raise Blocked(path) from None
                    try:  # whatever the umask: the shared group's, writable by it (docs/base.md)
                        os.fchmod(sub, 0o2775)
                    except OSError:
                        pass
                except OSError:  # a link (ELOOP), a file (ENOTDIR): never followed, never written through
                    raise Blocked(path) from None
                fds.append(sub)
        except BaseException:
            for fd in fds:
                os.close(fd)
            raise
        return fds, parts[-1]

    def _read(self, path: str) -> _State:
        try:
            fds, name = self._dirs(path, make=False)
        except FileNotFoundError:
            return _State("absent")
        except Blocked:
            return _State("blocked")
        try:
            try:
                st = os.stat(name, dir_fd=fds[-1], follow_symlinks=False)
            except FileNotFoundError:
                return _State("absent")
            if stat.S_ISLNK(st.st_mode):
                return _State("link")
            if stat.S_ISDIR(st.st_mode):
                return _State("dir")
            if not stat.S_ISREG(st.st_mode):
                return _State("other")
            if st.st_size > self.max_file:
                return _State("big", key=(st.st_size, st.st_mtime_ns, st.st_ino))
            try:
                file = os.open(name, _FILE, dir_fd=fds[-1])
            except OSError:
                return _State("blocked")
            try:
                fst = os.fstat(file)
                if not stat.S_ISREG(fst.st_mode) or fst.st_size > self.max_file:
                    return _State("other")
                chunks, left = [], fst.st_size + 1
                while left > 0 and (chunk := os.read(file, min(left, 1 << 20))):
                    chunks.append(chunk)
                    left -= len(chunk)
                data = b"".join(chunks)
            finally:
                os.close(file)
            if len(data) > self.max_file:
                return _State("big")
            return _State("file", data, (fst.st_size, fst.st_mtime_ns, fst.st_ino), self._blob_id(data))
        finally:
            for fd in fds:
                os.close(fd)

    def _unchanged(self, fd: int, name: str, expect: _State) -> None:
        """The path is still what was read: a write never clobbers an edit made since."""
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            if expect.kind == "absent":
                return
            raise Blocked(name) from None
        if expect.kind != "file" or not stat.S_ISREG(st.st_mode) or \
                (st.st_size, st.st_mtime_ns, st.st_ino) != expect.key:
            raise Blocked(name)

    def _put(self, path: str, data: bytes, mode: str, expect: _State) -> None:
        """`data` at `path`: a new file in its folder, renamed over the name — group-writable, so the lane keeps
        editing what the platform wrote (docs/base.md)."""
        fds, name = self._dirs(path, make=True)
        try:
            self._unchanged(fds[-1], name, expect)
            temp = f"{TEMP_PREFIX}{secrets.token_hex(6)}.tmp"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=fds[-1])
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fchmod(fd, 0o775 if mode == "100755" else 0o664)
            finally:
                os.close(fd)
            try:
                os.replace(temp, name, src_dir_fd=fds[-1], dst_dir_fd=fds[-1])
            except OSError:
                os.unlink(temp, dir_fd=fds[-1])
                raise Blocked(path) from None
            self._cache.pop(path, None)
        finally:
            for fd in fds:
                os.close(fd)

    def _remove(self, path: str, expect: _State) -> None:
        """The file at `path` gone, and the folders it leaves empty."""
        try:
            fds, name = self._dirs(path, make=False)
        except FileNotFoundError:
            return
        try:
            self._unchanged(fds[-1], name, expect)
            if expect.kind == "file":
                os.unlink(name, dir_fd=fds[-1])
            parts = path.split("/")[:-1]
            for i in range(len(parts) - 1, -1, -1):
                try:
                    os.rmdir(parts[i], dir_fd=fds[i])
                except OSError:
                    break
            self._cache.pop(path, None)
        finally:
            for fd in fds:
                os.close(fd)


UNFIT = {"link": "симлинк — в Базе только обычные файлы", "other": "не обычный файл (FIFO, сокет, устройство)",
         "dir": "папка, а не файл", "blocked": "путь проходит через симлинк или файл", "big": "больше {mb} МБ"}


def _utf8(path: str) -> bool:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _lines(data: bytes | None) -> list[str]:
    if data is None:
        return []
    text = data.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n\\ No newline at end of file\n"
    return lines


def _what(kind: str) -> str:
    return {"link": "симлинк", "other": "не обычный файл", "dir": "папка", "big": "большой файл",
            "blocked": "путь через симлинк"}.get(kind, kind)


def under(cwd: str | None, tree: str) -> bool:
    """Whether a session's directory (relative to the lane's /work) is in the clone."""
    if cwd is None:
        return False
    cwd, tree = cwd.strip("/"), tree.strip("/")
    return cwd == tree or cwd.startswith(tree + "/")

