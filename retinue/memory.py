"""The knowledge base on the router's side. The assistant edits files in a working copy with her own file tools;
after her turn the router — code, not the model — turns what changed into one commit, checks it (`kbcheck`) and
pushes it to the hub, or undoes the turn and says why. One writer: the router's queue runs her turns one at a time,
and every turn starts from the hub's current state.

The working copy's repository (`git_dir`) is not in her container: a hook or a config file she could write would
run here. Every git call names the repository and the tree explicitly and turns client hooks off.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import kbcheck
from .config import MemoryConfig

log = logging.getLogger("retinue.memory")

PUSH_TRIES = 3
SUBJECT_FILES = 3


@dataclass
class Settled:
    """What became of one turn's changes: a commit in the hub, or the reasons it was undone, or nothing at all."""
    turn: str = ""
    kind: str = ""
    foreign: list[str] = field(default_factory=list)
    commit: str | None = None
    refusals: list[str] = field(default_factory=list)
    subject: str = ""
    files: list[tuple[str, str]] = field(default_factory=list)
    owner_records: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


class Refused(Exception):
    pass


class Memory:
    def __init__(self, cfg: MemoryConfig, db: sqlite3.Connection) -> None:
        self.cfg = cfg
        self.tree, self.git_dir = Path(cfg.tree), Path(cfg.git_dir)
        self.policy = kbcheck.Policy.load(cfg.policy)
        self.db = db
        name, _, email = cfg.author.partition("<")
        # The hub belongs to the owner, not to the containers' user: git refuses a repository owned by another user
        # unless `safe.directory` names it in the global config — ours, a file inside the repository's folder.
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.git_dir), "LANG": "C.UTF-8",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_GLOBAL": str(self.git_dir / "router.gitconfig"),
                    "GIT_AUTHOR_NAME": name.strip(), "GIT_AUTHOR_EMAIL": email.rstrip(">").strip(),
                    "GIT_COMMITTER_NAME": name.strip(), "GIT_COMMITTER_EMAIL": email.rstrip(">").strip()}
        # No client hooks, no fsmonitor: nothing configured anywhere runs a command on our behalf.
        self.repo = ["git", f"--git-dir={self.git_dir}", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
        self.git = [*self.repo, f"--work-tree={self.tree}"]
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_commits (
                sha TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                turn TEXT NOT NULL,
                kind TEXT NOT NULL,              -- conversation | reminder | price | retry
                subject TEXT NOT NULL,
                files TEXT NOT NULL,             -- [[status, path], ...]
                owner_records TEXT NOT NULL,     -- the owner's own records it changed or deleted
                deleted TEXT NOT NULL,
                foreign_input TEXT NOT NULL,     -- forwarded | attachment | travel: someone else's text was read
                reverted TEXT                    -- the commit that took it back
            );
            """
        )
        db.commit()

    def _run(self, *args: str, check: bool = True, tree: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run([*(self.git if tree else self.repo), *args], env=self.env, capture_output=True,
                              text=True, check=check)

    def _out(self, *args: str) -> str:
        return self._run(*args).stdout.strip()

    def prepare(self) -> None:
        """The working copy, once: an empty repository outside the tree, the hub as origin, the sparse checkout."""
        self.tree.mkdir(parents=True, exist_ok=True)
        self.git_dir.mkdir(parents=True, exist_ok=True)
        Path(self.env["GIT_CONFIG_GLOBAL"]).write_text(f"[safe]\n\tdirectory = {self.cfg.hub}\n")
        if not (self.git_dir / "HEAD").is_file():
            self._run("init", "-q", "-b", self.policy.branch, tree=False)
            self._run("remote", "add", "origin", self.cfg.hub, tree=False)
        self._run("config", "core.sparseCheckout", "true", tree=False)
        self._run("config", "core.sparseCheckoutCone", "false", tree=False)
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "sparse-checkout").write_text("\n".join(self.cfg.checkout) + "\n")
        self.sync()
        if added := self.reconcile():
            log.warning("%d of her commits were in the hub but not in the table: added from the hub's log", added)

    def sync(self) -> None:
        """The tree is the hub's `main`, exactly: whatever a turn left behind is gone, and what the Mac pushed is in."""
        branch = self.policy.branch
        self._run("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        self._run("reset", "-q", "--hard", f"origin/{branch}")
        self._run("clean", "-ffdxq")
        self._purge()

    def _purge(self) -> None:
        """What `git clean` cannot see: a file or folder named `.git` anywhere in the tree (git never lists such a
        path) — a nested repository or plain hidden storage. The hub never holds one, so all of them go."""
        for folder, dirs, files in os.walk(self.tree, topdown=True):
            for name in [d for d in dirs if d == ".git"]:
                shutil.rmtree(os.path.join(folder, name), ignore_errors=True)
                dirs.remove(name)
            if ".git" in files:
                os.unlink(os.path.join(folder, ".git"))
        self._run("clean", "-ffdxq")

    def settle(self, turn: str, kind: str, foreign: list[str]) -> Settled:
        """One turn's changes: committed and in the hub, or undone with the reasons. Never raises for a refusal.
        Git only, so it may run in a worker thread; `record` puts a commit in the table afterwards."""
        if not self._out("status", "--porcelain", "--untracked-files=all"):
            return Settled(turn, kind, foreign)
        parent = self._out("rev-parse", "HEAD")
        self._run("add", "-A", "--sparse")
        files = [line.split("\t")[-1] for line in self._out("diff", "--cached", "--name-only", "-M", "HEAD").split("\n")]
        subject = "Ассистентка: " + ", ".join(files[:SUBJECT_FILES]) + (
            f" и ещё {len(files) - SUBJECT_FILES}" if len(files) > SUBJECT_FILES else "")
        trailers = [f"Retinue-Turn: {turn}", f"Retinue-Run: {kind}"]
        if foreign:
            trailers.append(f"Foreign-Input: {', '.join(foreign)}")
        message = f"{subject}\n\n" + "\n".join(trailers) + "\n"
        commit = self._commit(self._out("write-tree"), parent, message)
        settled = Settled(turn, kind, foreign, subject=subject)
        try:
            commit, verdict = self._deliver(commit, parent, message)
        except Refused as refused:
            self._keep(parent, commit, turn)
            self._drop()
            settled.refusals = list(refused.args[0])
            log.warning("turn %s not written to the base: %s", turn, "; ".join(settled.refusals))
            return settled
        settled.commit, settled.files = commit, verdict.files
        settled.owner_records, settled.deleted = verdict.owner_records, verdict.deleted
        return settled

    def record(self, settled: Settled, ts: float | None = None) -> None:
        """A commit of hers in the router's table, for the evening list. In the thread that owns the database."""
        if settled.commit:
            self.db.execute("INSERT OR IGNORE INTO memory_commits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                            (settled.commit, time.time() if ts is None else ts, settled.turn, settled.kind, settled.subject,
                             json.dumps(settled.files), json.dumps(settled.owner_records), json.dumps(settled.deleted),
                             json.dumps(settled.foreign)))
            self.db.commit()

    def revert(self, sha: str) -> tuple[bool, str]:
        """The owner pressed «Откатить» (everything in one thread: the router splits it, see `Core._revert`)."""
        subject, refusal = self.lookup(sha)
        if refusal:
            return False, refusal
        ok, text, commit = self.take_back(sha, subject)
        if ok:
            self.reverted(sha, commit)
        return ok, text

    def lookup(self, sha: str) -> tuple[str, str]:
        """(subject, "") of her commit `sha`, to take it back; or ("", why it cannot be)."""
        row = self.db.execute("SELECT subject, reverted FROM memory_commits WHERE sha = ?", (sha,)).fetchone()
        if row is None:
            return "", "Не откатилось: такого коммита ассистентки нет."
        return ("", "Этот коммит уже откачен.") if row[1] else (row[0], "")

    def reverted(self, sha: str, commit: str) -> None:
        self.db.execute("UPDATE memory_commits SET reverted = ? WHERE sha = ?", (commit, sha))
        self.db.commit()

    def take_back(self, sha: str, subject: str) -> tuple[bool, str, str]:
        """A commit that takes `sha` back, checked and pushed like any other. Git only."""
        self.sync()
        parent = self._out("rev-parse", "HEAD")
        merged = self._run("merge-tree", "--write-tree", "--name-only", f"--merge-base={sha}", parent, f"{sha}^",
                           check=False)
        if merged.returncode != 0:
            clashing = [line for line in merged.stdout.split("\n")[1:] if line and not line.startswith("Auto-merging")
                        and not line.startswith("CONFLICT")]
            return False, (f"Не откатилось: {', '.join(dict.fromkeys(clashing)) or 'файл'} менялся после этого "
                           f"коммита. Откати на Mac: git revert {sha[:12]}"), ""
        message = f"Откат: {subject}\n\nReverts: {sha}\n"
        commit = self._commit(merged.stdout.split("\n")[0], parent, message)
        try:
            commit, _ = self._deliver(commit, parent, message)
        except Refused as refused:
            self._drop()
            return False, "Не откатилось: " + "; ".join(refused.args[0]), ""
        return True, f"Откачено: {subject}.", commit

    def since(self, ts: float) -> list[dict]:
        """Her commits since `ts`, oldest first: what the evening list names."""
        rows = self.db.execute("SELECT sha, ts, subject, files, owner_records, deleted, foreign_input, reverted "
                               "FROM memory_commits WHERE ts >= ? ORDER BY ts", (ts,)).fetchall()
        return [{"sha": r[0], "ts": r[1], "subject": r[2], "files": json.loads(r[3]), "owner_records": json.loads(r[4]),
                 "deleted": json.loads(r[5]), "foreign": json.loads(r[6]), "reverted": r[7]} for r in rows]

    # --- inside ----------------------------------------------------------------------------------

    def _commit(self, tree: str, parent: str, message: str) -> str:
        return subprocess.run([*self.repo, "commit-tree", tree, "-p", parent], input=message, env=self.env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def _deliver(self, commit: str, parent: str, message: str) -> tuple[str, kbcheck.Verdict]:
        """Check and push; when the hub has moved meanwhile, put the change on top of it and try again."""
        branch = self.policy.branch
        for _ in range(PUSH_TRIES):
            verdict = kbcheck.check(self.repo, parent, commit, self.policy, kbcheck.ASSISTANT)
            if verdict.refusals:
                raise Refused(verdict.refusals)
            push = self._push(commit)
            if push.returncode == 0:
                # In the hub: whatever fails from here on, the change is committed, never "undone". The next sync
                # puts the tree right.
                try:
                    self._run("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
                    self._run("reset", "-q", "--hard", commit)
                except (OSError, subprocess.CalledProcessError):
                    log.exception("commit %s is in the hub; the working copy did not follow it", commit[:12])
                return commit, verdict
            if "[rejected]" not in push.stdout:  # the hub's own checks said no: their words are the reasons
                said = [line.removeprefix("remote: ").strip() for line in push.stderr.splitlines()
                        if line.startswith("remote: ") and "push не принят" not in line]
                raise Refused([line for line in said if line] or ["хаб не принял push"])
            self._run("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            upstream = self._out("rev-parse", f"origin/{branch}")
            merged = self._run("merge-tree", "--write-tree", "--name-only", f"--merge-base={parent}", upstream, commit,
                               check=False)
            if merged.returncode != 0:
                clashing = [line for line in merged.stdout.split("\n")[1:] if line and not line.startswith(
                    ("Auto-merging", "CONFLICT"))]
                raise Refused(["конфликт с правкой, которая пришла в хаб во время хода: "
                               + ", ".join(dict.fromkeys(clashing))])
            parent, commit = upstream, self._commit(merged.stdout.split("\n")[0], upstream, message)
        raise Refused(["хаб менялся быстрее, чем она успевала записать"])

    def _push(self, commit: str) -> subprocess.CompletedProcess:
        return self._run("push", "-q", "--porcelain", "origin", f"{commit}:refs/heads/{self.policy.branch}",
                         check=False)

    def reconcile(self, days: int = 7) -> int:
        """Her commits the hub has and the table does not — the router died between a push and `record`: read back
        from the hub's log by their `Retinue-Turn` trailer. Returns how many were added."""
        fields = ["%H", "%ct", "%s"] + [f"%(trailers:key={key},valueonly,separator=%x2C )"
                                        for key in ("Retinue-Turn", "Retinue-Run", "Foreign-Input")]
        out = self._run("log", f"--since={days}.days", "--format=" + "%x1f".join(fields) + "%x1e",
                        f"origin/{self.policy.branch}", tree=False).stdout
        known = {row[0] for row in self.db.execute("SELECT sha FROM memory_commits")}
        added = 0
        for entry in reversed([e.strip("\n") for e in out.split("\x1e") if e.strip()]):
            sha, ts, subject, turn, kind, foreign = (entry.split("\x1f") + [""] * 6)[:6]
            if not turn.strip() or sha in known:
                continue
            verdict = kbcheck.Verdict()
            for status, old_path, new_path, _ in kbcheck.changes(self.repo, f"{sha}^", sha):
                verdict.files.append((status, new_path))
                if status != "A":
                    before = kbcheck.header(kbcheck._show(self.repo, f"{sha}^", old_path))[0]
                    if self.policy.owner_mark and self.policy.owner_mark in before.get("written_by", ""):
                        verdict.owner_records.append(old_path)
                if status == "D":
                    verdict.deleted.append(old_path)
            self.record(Settled(turn.strip(), kind.strip() or "conversation",
                                [f.strip() for f in foreign.split(",") if f.strip()], sha, [], subject,
                                verdict.files, verdict.owner_records, verdict.deleted), float(ts))
            added += 1
        return added

    def _keep(self, parent: str, commit: str, turn: str) -> None:
        """What a refused turn wrote, as a patch next to the repository: the owner can still read it."""
        try:
            folder = self.git_dir / "refused"
            folder.mkdir(exist_ok=True)
            name = re.sub(r"[^0-9A-Za-z-]", "", turn)[:40] or "turn"
            patch = self._run("diff", "--binary", parent, commit, tree=False).stdout
            (folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.patch").write_text(patch)
        except Exception:
            log.exception("the refused change of turn %s was not kept", turn)

    def _drop(self) -> None:
        """Undo the turn: the tree is the hub's again — with what came from the Mac meanwhile."""
        try:
            self.sync()
        except subprocess.CalledProcessError:  # the hub is unreachable: at least nothing of the turn stays
            self._run("reset", "-q", "--hard", "HEAD")
            self._run("clean", "-ffdxq")
