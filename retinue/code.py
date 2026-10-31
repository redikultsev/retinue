"""The owner's repositories in the room (docs/code.md): a clean mirror of each here, fetched with his token; the
room's working clone of it at `/work/<root>/<name>`, whose `origin` is that mirror read-only; and what his button
«Push» needs — what the session's branch holds, and the push of exactly that as a branch and a pull request.

The room's repository is the session's, so here it is **hostile input**. Git never opens it as a repository: no
config, hook, `HEAD`, ref, index, attribute or alternate of it is ever read by git. What the platform needs of it is
read in Python, one descriptor at a time with `O_NOFOLLOW` (`_Room`): the branch `HEAD` names and that branch's tip,
the work tree's files to tell what is not committed. Its objects are **copied**, read through descriptors pinned
with `O_NOFOLLOW`, into a repository of the platform's own (`_store`: its own `HEAD`, `config` and one ref, the
mirror as its alternate): each loose object checked against its name as it is inflated, a pack only when it holds
something the mirror lacks; nothing of the room's `objects/info`, commit graph, bitmaps or multi-pack index. git
reads only that copy: `git fetch` from it into the platform's **outbox** (one per mirror, the mirror as its
alternate) moves the session's new objects, checked (`fetch.fsckObjects`), under a private ref. A room repository
with `objects/info/alternates`, `http-alternates`, a `commondir`, a `.git` that is a file, or a link anywhere among its
objects or refs is refused. From then on everything is read from the outbox, which the platform alone writes.

**Everything that leaves is on the card** (`_leaving`): every commit the remote has nowhere — all parents, not the
first only — with its whole message and its own files; a new root commit (a history the remote never had) is
refused; and the objects a push would carry (`rev-list --objects <tip> --not <the remote's refs>`) must each be a
shown commit, its tree or an object its own change brings — any other refuses the push. Their bytes are counted.
git's output, time and memory are bounded (`_bounded`, RLIMIT_AS, small pack windows).

The token never enters the room, a URL, an argument, a file or an environment: git asks for it through a
credential helper that reads it once from a pipe whose descriptor only that git call holds (`_git`). Its https
goes through the platform's own proxy (`http.proxy`). Redirects are
not followed; the only protocols are the remote's and the outbox's own file transport. The push URL is the one in
`platform.yaml`; the remote ref is always `refs/heads/<branch>`, never the default branch, never forced, never a tag.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import unicodedata
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from .config import BRANCH, CodeConfig, CodeRepo

log = logging.getLogger("retinue.code")

GIT_TIMEOUT_S = 300           # one git call: a first mirror fetch of a big repository, a push
FRESH_TIMEOUT_S = 60          # the fetch before a run: past it the run goes on the mirror as it is
API_TIMEOUT_S = 30
MAX_COMMITS = 100             # new commits one push may carry: each is shown with its message and files
MAX_ROOM_ENTRIES = 200_000    # the walk of a room repository's objects and refs
MAX_ROOM_BYTES = 4 * 2**30    # its objects, in all
MAX_REF_BYTES = 8 * 2**20     # `packed-refs`
MAX_HASHED = 256 * 2**20      # the work tree's bytes hashed for one look; past it the rest is said unchecked
MAX_FILE = 64 * 2**20         # a tracked file bigger than this is compared by size only
MAX_UNTRACKED = 50            # untracked entries named on a card
MAX_DIRS = 20_000             # tracked folders listed for untracked entries
MAX_STARS = 16                # an ignore pattern with more `*` (after a run counts as one) is not used
MAX_IGNORE_LINES = 2_000      # of one ignore file; past them the look is partial
MAX_IGNORE_FILES = 200        # ignore files read for one look
LOOK_BUDGET_S = 20.0          # the work tree's part of a look; past it the rest is said unchecked
MAX_FOLDER_ENTRIES = 100_000  # entries of one folder looked at for untracked ones
COPY_TRIES = 2                # a pack copy cut by a crash (`copying` left) is tried again this many times in all
MAX_OUTPUT = 32 * 2**20       # git's output read by the platform; past it the call is killed and refused
MAX_NEW_OBJECTS = 100_000     # objects a push may carry
MAX_PUSH_BYTES = 200 * 2**20  # their size in all
MAX_PACK_COPY = 512 * 2**20   # the room's packs copied for one look (a pack all in the mirror is not copied)
MAX_LOOSE = 64 * 2**20        # one loose object, compressed; its content is checked against its name as it is copied
GIT_MEMORY_KB = 600 * 1024    # the address space of each git process here (RLIMIT_AS), on Linux: below the
#                               platform's 768 MiB, and one look or copy at a time (`_heavy`)
WORKFLOWS = ".github/workflows/"
IGNORED_DIRS = ("out", "inbox")   # the session's files for the owner and his attachments: never a commit's
LFS = b"version https://git-lfs.github.com/spec/v1"
SHA = re.compile(r"[0-9a-f]{40}")
GITHUB_API = "https://api.github.com"

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)


class Unavailable(OSError):
    """The mirror, the outbox or the remote cannot be worked on now; said to the owner as it is."""


class Refused(Exception):
    """The room's repository will not be read or pushed as it is: the reason, in Russian, for the card."""


class Absent(Refused):
    """Nothing to read yet — no clone, no commit on the branch: not a reason the owner needs on a ping."""


@dataclass
class Look:
    """What the session's branch holds, as the outbox has it: the card's contents. Everything a push of it would carry
    is here: each new commit with its whole message, its parents and its own files, and the bytes in all."""
    repo: str
    branch: str                       # the room's branch (its HEAD)
    sha: str                          # its tip, fetched into the outbox
    base: str                         # the remote's default branch
    base_sha: str
    merge_base: str
    ahead: int                        # commits on the branch not on the default one: what a PR would hold
    unpushed: int                     # commits not on any branch of the remote: what a push carries, the button's N
    commits: list[dict] = field(default_factory=list)   # the new ones, newest first: [{sha, subject, message, parents,
    #                                                      files: [{path, status}]}]
    files: list[dict] = field(default_factory=list)     # [{path, status, add, del}] merge-base → tip: the PR's whole
    workflows: list[str] = field(default_factory=list)  # files under .github/workflows/ any new commit touches
    dirty: list[str] = field(default_factory=list)      # tracked files that differ from the tip, untracked entries
    partial: bool = False             # the work tree was not checked whole (size, entry or time bounds)
    objects: int = 0                  # the objects a push carries
    bytes: int = 0                    # and their size
    digest: str = ""                  # of the commits and objects that leave: checked again at «Send»


@dataclass
class Commit:
    """A commit as its own object says it (`_commit`)."""
    tree: str
    parents: list[str]
    author: str
    committer: str
    message: str


@dataclass
class Pushed:
    ok: bool                          # the remote has refs/heads/<branch> at the card's sha
    url: str = ""                     # the pull or merge request
    said: str = ""                    # what went wrong, or what to know


def plain(text: str, secret: str = "") -> str:
    """A line of git's or an API's output for a log or a card: the token never, whatever said it; control
    characters out; cut."""
    if secret:
        text = text.replace(secret, "***")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text).strip()[-600:]


# --- the room's repository, read without git ----------------------------------------------------------------------

class _Room:
    """`/work/<root>/<name>` of the room as the platform's read-only mount shows it, opened folder by folder with
    O_NOFOLLOW. Holds the descriptors of its work tree and of its `.git/objects` until closed."""

    def __init__(self, room: Path, name: str) -> None:
        self.fds: list[int] = []
        try:
            top = self._open(os.open(room.parent, _DIR & ~os.O_NOFOLLOW), "")   # the lane's /work: a mount point
            here = self._open(top, room.name, "the code folder")
            self.tree = self._open(here, name, f"the folder {name}")
            self.git = self._open(self.tree, ".git", ".git (not a folder: the work tree of another repository?)")
            self.objects = self._open(self.git, "objects", ".git/objects")
            for parent, bad in ((self.git, "commondir"), (self.objects, "info/alternates"),
                                (self.objects, "info/http-alternates")):
                if _exists(parent, bad):
                    raise Refused(f"the repository has .git/{'objects/' if parent != self.git else ''}{bad}: it is not "
                                  "pushed so — objects could come from somewhere else")
        except BaseException:
            self.close()
            raise

    def _open(self, parent: int, name: str, what: str = "") -> int:
        if not name:
            self.fds.append(parent)
            return parent
        try:
            fd = os.open(name, _DIR, dir_fd=parent)
        except FileNotFoundError:
            raise Absent(f"{what}: missing") from None
        except OSError as exc:  # ELOOP / ENOTDIR: a link or a file where a folder must be
            raise Refused(f"{what}: {'a link' if exc.errno in (40, 62) else 'not a folder'}") from None
        self.fds.append(fd)
        return fd

    def close(self) -> None:
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []

    def head(self) -> tuple[str, str]:
        """(branch, tip): the branch `.git/HEAD` names and its commit, from the loose ref or `packed-refs`."""
        text = _read(self.git, "HEAD", 4096)
        if text is None:
            raise Refused("no .git/HEAD")
        text = text.decode("ascii", "replace").strip()
        if not text.startswith("ref: refs/heads/"):
            raise Refused("the session is not on a branch (detached HEAD) — ask it to create a branch and commit")
        branch = text.removeprefix("ref: refs/heads/")
        if not BRANCH.fullmatch(branch):
            raise Refused(f"a branch with an unusual name: {branch[:80]!r}")
        loose = _read(self.git, f"refs/heads/{branch}", 256)
        if loose is not None:
            sha = loose.decode("ascii", "replace").strip()
        else:
            sha = ""
            for line in (_read(self.git, "packed-refs", MAX_REF_BYTES) or b"").decode("ascii", "replace").splitlines():
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
                    sha = parts[0]
                    break
        if not sha:
            raise Absent(f"the branch {branch} has no commits")
        if not SHA.fullmatch(sha):
            raise Refused(f"branch {branch}: wrong ref format (sha256 repositories are not supported)")
        return branch, sha

    def check(self) -> None:
        """No link and nothing but folders and files among `.git/objects` and `.git/refs`, within the bounds."""
        entries, size = 0, 0
        stack: list[tuple[int, str, int]] = []
        try:
            for top in ("objects", "refs"):
                if top == "refs":
                    if not _exists(self.git, "refs"):
                        continue
                    stack.append((self._sub(self.git, "refs", "refs"), "refs", 0))
                else:
                    stack.append((os.dup(self.objects), "objects", 0))
                while stack:
                    fd, where, depth = stack.pop()
                    try:
                        with os.scandir(fd) as found:
                            for entry in found:
                                entries += 1
                                if entries > MAX_ROOM_ENTRIES:
                                    raise Refused(f".git has over {MAX_ROOM_ENTRIES} entries — not read so")
                                info = entry.stat(follow_symlinks=False)
                                if stat.S_ISDIR(info.st_mode) and depth < 8:
                                    stack.append((self._sub(fd, entry.name, f"{where}/{entry.name}"),
                                                  f"{where}/{entry.name}", depth + 1))
                                elif stat.S_ISREG(info.st_mode):
                                    size += info.st_size
                                else:
                                    raise Refused(f".git/{where}/{entry.name[:60]}: "
                                                  f"{'a link' if stat.S_ISLNK(info.st_mode) else 'not a file'} — not "
                                                  "read so")
                    finally:
                        os.close(fd)
        finally:
            for fd, _, _ in stack:
                os.close(fd)
        if size > MAX_ROOM_BYTES:
            raise Refused(f"the repository's objects are over {MAX_ROOM_BYTES >> 30} GB")

    @staticmethod
    def _sub(parent: int, name: str, where: str) -> int:
        """A folder inside `.git`, never through a link (a swap after the listing is caught here too)."""
        try:
            return os.open(name, _DIR, dir_fd=parent)
        except OSError:
            raise Refused(f".git/{where[:80]}: a link or not a folder — not read so") from None

    def info_exclude(self) -> str:
        return (_read(self.git, "info/exclude", 1 << 20) or b"").decode("utf-8", "replace")


def _exists(parent: int, path: str) -> bool:
    try:
        os.stat(path, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        return False
    return True


def _walk(parent: int, path: str) -> int:
    """The folder at `path` under `parent`, every part opened with O_NOFOLLOW; the caller closes it."""
    fd = os.dup(parent)
    try:
        for part in [p for p in path.split("/") if p]:
            nxt = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read(parent: int, path: str, cap: int) -> bytes | None:
    """A regular file under `parent`, nothing followed, at most `cap` bytes; None when there is none (or a link)."""
    folder, _, name = path.rpartition("/")
    try:
        fd_dir = _walk(parent, folder)
    except OSError:
        return None
    try:
        fd = os.open(name, _FILE, dir_fd=fd_dir)
    except OSError:
        return None
    finally:
        os.close(fd_dir)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None
        with os.fdopen(os.dup(fd), "rb") as f:
            return f.read(cap + 1)[:cap]
    finally:
        os.close(fd)


class _Ignores:
    """`.gitignore` the way the card needs it: the root's, `.git/info/exclude` and each tracked folder's, by name for
    a plain pattern and by path for one with a `/`. `!` lines are skipped (a little more is hidden than git would).

    The patterns are the session's, so matching them is bounded: a run of `*` counts as one, a pattern with more than
    MAX_STARS of them or longer than 200 characters is not used (the look says it is partial), and a glob is matched
    segment by segment (`fnmatch`, which takes no exponential path) with `**` as a table over segments — never one
    backtracking regular expression over the whole path."""

    def __init__(self) -> None:
        self.rules: dict[str, list[tuple[list[str], bool, bool]]] = {}  # folder -> (segments, anchored, folders only)
        self.partial = False

    def add(self, folder: str, text: str) -> None:
        rules = self.rules.setdefault(folder, [])
        for raw in text.splitlines()[:MAX_IGNORE_LINES]:
            raw = raw.strip()
            if not raw or raw.startswith(("#", "!")):
                continue
            only_dirs = raw.endswith("/")
            raw = re.sub(r"\*{3,}", "**", raw.rstrip("/"))
            if len(raw) > 200 or raw.count("*") > MAX_STARS:
                self.partial = True
                continue
            anchored = "/" in raw
            segments = [seg if seg == "**" else re.sub(r"\*+", "*", seg) for seg in raw.strip("/").split("/") if seg]
            if segments:
                rules.append((segments, anchored, only_dirs))
        if len(text.splitlines()) > MAX_IGNORE_LINES:
            self.partial = True

    def ignored(self, path: str, is_dir: bool) -> bool:
        parts = path.split("/")
        for depth in range(len(parts)):
            folder = "/".join(parts[:depth])
            rest = parts[depth:]
            for segments, anchored, only_dirs in self.rules.get(folder, ()):
                if only_dirs and not is_dir:
                    continue
                if (_glob(rest, segments) or _glob(rest, [*segments, "**"])) if anchored else \
                        fnmatch.fnmatchcase(parts[-1], segments[-1]):
                    return True
        return False


def _glob(path: list[str], pattern: list[str]) -> bool:
    """`path` (segments) matched by `pattern` (segments, `**` any number of them): a table, O(len(path) × len(pattern))
    `fnmatch` calls."""
    reach = {0}
    for seg in pattern:
        if seg == "**":
            reach = set(range(min(reach), len(path) + 1)) if reach else set()
        else:
            reach = {i + 1 for i in reach if i < len(path) and fnmatch.fnmatchcase(path[i], seg)}
        if not reach:
            return False
    return len(path) in reach


MAX_OBJECT = 512 * 2**20       # a loose object's inflated size


def _loose_sha(data: bytes) -> str:
    """The name a loose object's bytes deserve: its inflated content hashed, inflated in steps (never more than
    MAX_OBJECT), so a crafted one cannot make the platform hold a bomb."""
    inflate, digest, size = zlib.decompressobj(), hashlib.sha1(), 0
    chunk = data
    try:
        while chunk:
            out = inflate.decompress(chunk, 1 << 20)
            size += len(out)
            if size > MAX_OBJECT:
                return ""
            digest.update(out)
            chunk = inflate.unconsumed_tail
        rest = inflate.flush()
    except zlib.error:
        return ""
    if size + len(rest) > MAX_OBJECT:
        return ""
    digest.update(rest)
    return digest.hexdigest()


MAX_MESSAGE = 16 * 1024        # a commit message, in bytes: a longer one is refused, never shown cut
MAX_IDENTITY = 200             # an author or committer line, in bytes: the card shows it whole, so a longer one is refused
HEADERS = (b"tree", b"parent", b"author", b"committer", b"encoding")
INVISIBLE = {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp", "Mn"}   # and every space but U+0020 (`shown`)


def _commit(sha: str, raw: bytes) -> Commit:
    """A commit object's own bytes, read strictly in git's order: exactly one `tree`, any `parent`s, exactly one
    `author`, exactly one `committer`, at most one `encoding` (UTF-8), then the blank line and one message in UTF-8.
    Anything else — a header the card cannot show, a repeat (fsck lets a second author/committer pair through after
    the first `committer`), a line out of order — refuses it: what the card shows is the whole object."""
    head, sep, message = raw.partition(b"\n\n")
    rows = head.split(b"\n")
    order = [b"tree", *([b"parent"] * sum(1 for r in rows if r.startswith(b"parent "))), b"author", b"committer"]
    if len(rows) == len(order) + 1:
        order.append(b"encoding")
    keys = [row.partition(b" ")[0] for row in rows]
    if keys != order or any(row.startswith(b" ") for row in rows):
        odd = next((k for k, want in zip(keys + [b""] * len(order), order + [b""] * len(keys)) if k != want), b"")
        name = shown(odd.decode("utf-8", "replace"), 40) or "order"
        raise Refused(f"commit {sha[:7]}: header «{name}» is out of place, extra or repeated (a signature, "
                      "an embedded tag, a second author) — you would not check it, not pushing")
    values = [row.partition(b" ")[2] for row in rows]
    if len(values) > len(order) - 1 and keys[-1] == b"encoding" and \
            values[-1].strip().lower() not in (b"utf-8", b"utf8"):
        raise Refused(f"commit {sha[:7]}: encoding is not UTF-8 — not pushing")
    if any(len(v) > MAX_IDENTITY for k, v in zip(keys, values) if k in (b"author", b"committer")):
        raise Refused(f"commit {sha[:7]}: author or committer longer than {MAX_IDENTITY} bytes — not pushing")
    texts = [v.decode("utf-8", "replace") for v in values]
    n = len(order) - (2 if keys[-1] == b"encoding" else 1)   # the committer's place
    tree, parents, author, committer = texts[0], texts[1:n - 1], _identity(texts[n - 1]), _identity(texts[n])
    if len(message) > MAX_MESSAGE:
        raise Refused(f"commit {sha[:7]}: message over {MAX_MESSAGE >> 10} KB — not pushing")
    try:
        text = message.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"commit {sha[:7]}: message is not UTF-8 — not pushing") from None
    if not (sep and SHA.fullmatch(tree) and all(SHA.fullmatch(x) for x in parents)):
        raise Refused(f"commit {sha[:7]} is not built by git's rules — not pushing")
    return Commit(tree, parents, author, committer, text.rstrip("\n"))


def _identity(value: str) -> str:
    """`Name <email> 1700000000 +0000` → `Name <email>, 2023-11-14 22:13 +0000`."""
    who, _, when = value.rpartition("> ")
    stamp, _, zone = when.partition(" ")
    try:
        at = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(stamp) + (1 if zone[:1] != "-" else -1) *
                                                         (int(zone[1:3]) * 3600 + int(zone[3:5]) * 60)))
    except (ValueError, OverflowError):
        return value
    return f"{who}>, {at} {zone}"


def shown(text: str, cap: int, lines: bool = False) -> str:
    """The session's words (a path, a subject, a message, an identity) as a card shows them: every invisible
    character — a newline too, unless `lines`; a tab; a combining mark; any space but U+0020 — written as its code
    ⟦U+…⟧, so no name can fake a line, a direction or a gap; cut."""
    keep = "\n" if lines else ""
    out = "".join(f"⟦U+{ord(c):04X}⟧" if c not in keep and (c == "\t" or unicodedata.category(c) in INVISIBLE or
                                                             (unicodedata.category(c) == "Zs" and c != " ")) else c
                  for c in str(text))
    return out if len(out) <= cap else out[:cap - 1] + "…"


def _idx_names(idx: Path, out: Path) -> int:
    """The object names a pack's `.idx` (version 2) lists, written one per line into `out`, read in steps; how many.
    A malformed index is refused."""
    with open(idx, "rb") as f, open(out, "w") as names:
        head = f.read(8 + 1024)
        if head[:8] != b"\xfftOc\x00\x00\x00\x02" or len(head) < 8 + 1024:
            raise Refused("the pack index has the wrong version — not read so")
        count = int.from_bytes(head[8 + 255 * 4:8 + 1024], "big")
        left = count
        while left:
            chunk = f.read(20 * min(left, 4096))
            if len(chunk) < 20 or len(chunk) % 20:
                raise Refused("the pack index is truncated — not read so")
            names.write("".join(f"{chunk[i:i + 20].hex()}\n" for i in range(0, len(chunk), 20)))
            left -= len(chunk) // 20
    return count


def _copy(parent: int, name: str, into: Path, size: int) -> None:
    """A regular file under `parent` (O_NOFOLLOW) copied in steps into `into`: exactly `size` bytes, or refused."""
    try:
        fd = os.open(name, _FILE, dir_fd=parent)
    except OSError:
        raise Refused(f".git/objects/pack/{name[:13]}…: did not open") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise Refused(f".git/objects/pack/{name[:13]}…: changed while being read")
        copied = 0
        with open(into, "wb") as out:
            while chunk := os.read(fd, 1 << 20):
                copied += len(chunk)
                if copied > size:
                    break
                out.write(chunk)
        if copied != size:
            raise Refused(f".git/objects/pack/{name[:13]}…: changed while being read")
    finally:
        os.close(fd)


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _bounded(argv: list[str], env: dict, fds: list[int], timeout: float, cap: int,
             stdin: bytes | Path | None, cut: bool = False) -> tuple[int, bytes, bytes]:
    """(code, stdout, stderr) of a process whose stdout is read up to `cap` bytes and stderr up to 64 KiB: past the
    cap (OverflowError) or the time (TimeoutError) it is killed. Nothing it says is held whole in memory past that."""
    source = open(stdin, "rb") if isinstance(stdin, Path) else None  # noqa: SIM115 — closed below
    try:
        proc = subprocess.Popen(argv, env=env, stdin=source or (subprocess.PIPE if stdin is not None else
                                                                subprocess.DEVNULL),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=fds, umask=0o022)
    finally:
        if source is not None:
            source.close()
    chunks: dict[str, list[bytes]] = {"out": [], "err": []}
    over = threading.Event()

    def drain(stream, key: str, limit: int) -> None:
        size = 0
        while chunk := stream.read(1 << 16):
            size += len(chunk)
            if size > limit:
                if key == "out":
                    over.set()
                    proc.kill()
                    break
                continue  # stderr past its cap: read and dropped
            chunks[key].append(chunk)

    readers = [threading.Thread(target=drain, args=(proc.stdout, "out", cap), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, "err", 64 * 1024), daemon=True)]
    for reader in readers:
        reader.start()
    if isinstance(stdin, bytes):
        def feed() -> None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except OSError:
                pass
        threading.Thread(target=feed, daemon=True).start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise TimeoutError from None
    finally:
        for reader in readers:
            reader.join(5)
    if over.is_set() and not cut:
        raise OverflowError
    return (0 if over.is_set() else rc), b"".join(chunks["out"])[:cap], b"".join(chunks["err"])


# --- the platform's repositories --------------------------------------------------------------------------------

class CodeRepos:
    """The mirrors (`cfg.mirrors`, read-only in the room), the outboxes (`cfg.outbox`, the platform's alone) and the
    room's clones as the platform's read-only mount shows them (`<work_mount>/<root>`). `fetch` and the outbox's
    work run in worker threads, one per repository at a time; `look` and `push` never write the room."""

    def __init__(self, cfg: CodeConfig, work_mount: str) -> None:
        self.cfg = cfg
        self.repos = {repo.name: repo for repo in cfg.repos}
        self.room = Path(work_mount) / cfg.root
        self.mirrors, self.outbox = Path(cfg.mirrors), Path(cfg.outbox)
        self._mirror_locks = {name: threading.Lock() for name in self.repos}
        self._outbox_locks = {name: threading.Lock() for name in self.repos}
        self.failed: dict[str, str] = {}      # the last fetch's failure, said where the repository is listed
        self._hashes: dict[tuple, str] = {}   # (repo, path, size, mtime_ns, ino, ctime_ns) -> blob sha
        self._defaults: dict[str, str] = {}
        self._heavy = threading.Lock()        # one copy of the room's objects at a time, whichever repository
        self._config = self.outbox / "platform.gitconfig"

    # --- git, as the platform runs it -------------------------------------------------------------------------

    def _gitconfig(self) -> Path:
        """The platform's own global config for every git call here: nothing of the system's, no hook, no fsmonitor,
        no attributes, no redirect, a credential helper that reads the token from the call's own pipe, and only the
        protocols in use. Written once, and again when it differs."""
        self.outbox.mkdir(parents=True, exist_ok=True)
        text = (
            "[core]\n\thooksPath = /dev/null\n\tfsmonitor = false\n\tattributesFile = /dev/null\n"
            "\texcludesFile = /dev/null\n\taskPass =\n"
            "[http]\n\tfollowRedirects = false\n"
            "[transfer]\n\tcredentialsInUrl = die\n"
            "[credential]\n\thelper =\n"
            "\thelper = \"!f() { test \\\"$1\\\" = get || return 0; p=; h=; while IFS= read -r l; do case \\\"$l\\\" in protocol=*) p=${l#protocol=};; host=*) h=${l#host=};; esac; done; test -n \\\"$RETINUE_CRED_FD\\\" && test \\\"$p\\\" = \\\"$RETINUE_CRED_PROTOCOL\\\" && test \\\"$h\\\" = \\\"$RETINUE_CRED_HOST\\\" && cat /dev/fd/$RETINUE_CRED_FD; }; f\"\n"
            "[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n[init]\n\tdefaultBranch = main\n"
            "[advice]\n\tdetachedHead = false\n[push]\n\tfollowTags = false\n\tdefault = nothing\n"
            "[protocol]\n\tallow = never\n"
            # Bounded memory in a 768 MiB container: small windows on the packs, one thread, no big file in memory.
            "[core]\n\tbigFileThreshold = 1m\n\tpackedGitLimit = 256m\n\tpackedGitWindowSize = 32m\n"
            "\tdeltaBaseCacheLimit = 32m\n\tcommitGraph = false\n\tmultiPackIndex = false\n"
            "[pack]\n\twindowMemory = 64m\n\tthreads = 1\n\tuseBitmaps = false\n"
            "[fetch]\n\twriteCommitGraph = false\n"
            # The platform's only way out (platform-egress): for its https calls; the file transport never uses it.
            + (f"[http]\n\tproxy = {self.cfg.proxy}\n" if self.cfg.proxy else ""))
        if not self._config.exists() or self._config.read_text() != text:
            tmp = self._config.with_suffix(f".{secrets.token_hex(4)}.tmp")
            tmp.write_text(text)
            os.replace(tmp, self._config)
        return self._config

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        tmp = self.outbox / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(self.outbox),
               "LANG": "C.UTF-8", "TMPDIR": str(tmp), "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": str(self._gitconfig()), "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
               "GIT_NO_LAZY_FETCH": "1", "GCM_INTERACTIVE": "never"}
        return {**env, **(extra or {})}

    def _git(self, *args: str, protocols: tuple[str, ...] = (), auth: CodeRepo | None = None,
             timeout: float = GIT_TIMEOUT_S, check: bool = True, text: bool = True, cap: int = MAX_OUTPUT,
             stdin: bytes | Path | None = None, cut: bool = False) -> subprocess.CompletedProcess:
        """One git call: the protocols it may use (`-c protocol.<p>.allow=always`), the token of `auth` through the
        helper's pipe. Its output is read up to `cap` bytes — past them git is killed and the call refused — and never
        carries the token (`plain`). On Linux each git runs with its address space bounded (RLIMIT_AS)."""
        extra, keep = {}, []
        read = None
        if auth is not None and auth.secret:
            user = "x-access-token" if auth.host == "github.com" else "oauth2"
            read, write = os.pipe()
            try:
                os.write(write, f"username={user}\npassword={auth.secret}\n".encode())
            finally:
                os.close(write)
            extra["RETINUE_CRED_FD"] = str(read)
            # The helper answers this host of this protocol alone — never another, never a proxy's challenge.
            extra["RETINUE_CRED_PROTOCOL"] = auth.url.split("://", 1)[0]
            extra["RETINUE_CRED_HOST"] = auth.url.split("://", 1)[1].split("/", 1)[0].rpartition("@")[2]
            keep.append(read)
        argv = ["git", *(a for p in protocols for a in ("-c", f"protocol.{p}.allow=always")), *args]
        if sys.platform == "linux":
            argv = ["sh", "-c", f'ulimit -v {GIT_MEMORY_KB} && exec "$@"', "git", *argv]
        what = f"git {args[1] if args[0].startswith('--git-dir') else args[0]}"
        try:
            rc, out, err = _bounded(argv, self._env(extra), keep, timeout, cap, stdin, cut)
        except TimeoutError:
            raise Unavailable(f"{what}: no answer in {int(timeout)} s") from None
        except OverflowError:
            raise Unavailable(f"{what}: answer over {cap >> 20} MB — not read so") from None
        finally:
            if read is not None:
                os.close(read)
        done = subprocess.CompletedProcess(argv, rc, out.decode("utf-8", "replace") if text else out,
                                           err.decode("utf-8", "replace") if text else err)
        if check and rc != 0:
            raise Unavailable(plain(err.decode("utf-8", "replace"), auth.secret if auth else "") or f"git: code {rc}")
        return done

    def _scheme(self, repo: CodeRepo) -> tuple[str, ...]:
        return (repo.url.split(":", 1)[0],)

    # --- mirrors ----------------------------------------------------------------------------------------------

    def mirror(self, name: str) -> Path:
        return self.mirrors / f"{name}.git"

    def lane_mirror(self, name: str) -> str:
        return f"{self.cfg.lane_mirrors.rstrip('/')}/{name}.git"

    def ready(self, name: str) -> bool:
        """The mirror has been fetched at least once: the room can clone it."""
        return (self.mirror(name) / "HEAD").is_file() and (self.mirror(name) / ".retinue-fetched").is_file()

    def fetch(self, name: str, timeout: float = GIT_TIMEOUT_S, wait: bool = True) -> bool:
        """The remote's branches and tags into the mirror, pruned, its HEAD the default branch. False when another
        fetch holds it and `wait` is off. Raises `Unavailable` with git's words (the token never among them)."""
        repo = self.repos[name]
        if repo.off:
            raise Unavailable(repo.off)
        lock = self._mirror_locks[name]
        if not lock.acquire(blocking=wait):
            return False
        try:
            mirror = self.mirror(name)
            if not (mirror / "HEAD").is_file():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                self._git("init", "-q", "--bare", str(mirror))
            git_dir = f"--git-dir={mirror}"
            try:
                default = repo.default_branch or self._remote_head(repo, timeout)
                self._git(git_dir, "fetch", "--quiet", "--prune", "--no-tags", "--no-write-fetch-head",
                          "--no-recurse-submodules", repo.url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
                          protocols=self._scheme(repo), auth=repo, timeout=timeout)
            except Unavailable as exc:
                self.failed[name] = str(exc)
                raise
            if default:
                self._git(git_dir, "symbolic-ref", "HEAD", f"refs/heads/{default}")
                self._defaults[name] = default
            (mirror / ".retinue-fetched").write_text(json.dumps({"default": default}))
            self.failed.pop(name, None)
            return True
        finally:
            lock.release()

    def _remote_head(self, repo: CodeRepo, timeout: float) -> str:
        """The branch the remote's HEAD points at (`ls-remote --symref`), '' when it says none."""
        out = self._git("ls-remote", "--symref", repo.url, "HEAD", protocols=self._scheme(repo), auth=repo,
                        timeout=timeout).stdout
        found = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", out, re.M)
        return found.group(1) if found and BRANCH.fullmatch(found.group(1)) else ""

    def default_branch(self, name: str) -> str:
        """The remote's default branch: the config's, or what the last fetch found."""
        repo = self.repos[name]
        if repo.default_branch:
            return repo.default_branch
        if name not in self._defaults:
            try:
                self._defaults[name] = json.loads((self.mirror(name) / ".retinue-fetched").read_text())["default"]
            except (OSError, ValueError, KeyError, TypeError):
                return "main"
        return self._defaults[name] or "main"

    def _rev(self, git_dir: Path, rev: str) -> str | None:
        done = self._git(f"--git-dir={git_dir}", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}",
                         check=False)
        return done.stdout.strip() if done.returncode == 0 and SHA.fullmatch(done.stdout.strip()) else None

    # --- the outbox ---------------------------------------------------------------------------------------------

    def _outbox(self, name: str) -> Path:
        """The platform's own repository for this mirror: what cards fetched from the room, under private refs; the
        mirror its alternate, so it holds only the session's new objects."""
        path = self.outbox / f"{name}.git"
        if not (path / "HEAD").is_file():
            self._git("init", "-q", "--bare", str(path))
        alternates = path / "objects" / "info" / "alternates"
        wanted = f"{self.mirror(name) / 'objects'}\n"
        if not alternates.exists() or alternates.read_text() != wanted:
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(wanted)
        return path

    def head(self, name: str) -> tuple[str, str]:
        """(branch, tip) of the room's clone, read without git."""
        room = _Room(self.room, name)
        try:
            return room.head()
        finally:
            room.close()

    def _store(self, name: str, room: _Room, sha: str) -> Path:
        """The platform's copy of the room's objects (`<outbox>/<name>.room`): a repository of its own — its `HEAD`,
        `config`, the one ref `tip` at `sha`, the mirror as its alternate — into which the room's loose objects are
        copied, each checked against its name (inflated and hashed as it is read), and the room's packs that hold
        anything the mirror lacks (a pack all in the mirror — the clone's own — is never copied). Everything is read
        through the room's pinned descriptors with O_NOFOLLOW, bounded; nothing of the room's `objects/info`, commit
        graph, bitmaps or multi-pack index comes over. git reads only this copy."""
        store = self.outbox / f"{name}.room"
        objects = store / "objects"
        for folder in (store / "refs" / "heads", objects / "pack", objects / "info"):
            folder.mkdir(parents=True, exist_ok=True)
        (store / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
        (store / "HEAD").write_text("ref: refs/heads/tip\n")
        (objects / "info" / "alternates").write_text(f"{self.mirror(name) / 'objects'}\n")
        (store / "refs" / "heads" / "tip").write_text(sha + "\n")
        budget = [MAX_PACK_COPY]
        with os.scandir(room.objects) as entries:
            found = [(e.name, e.is_dir(follow_symlinks=False)) for e in entries]
        loose = 0
        for entry, is_dir in found:
            if is_dir and re.fullmatch(r"[0-9a-f]{2}", entry):
                loose += self._copy_loose(room, entry, objects / entry, budget)
        if loose > 100_000:
            raise Refused("the session's repository has over 100,000 loose objects — let it run git gc")
        if any(e == "pack" and d for e, d in found):
            self._copy_packs(name, room, objects / "pack", budget)
        return store

    def _copy_loose(self, room: _Room, prefix: str, into: Path, budget: list[int]) -> int:
        fd = room._sub(room.objects, prefix, f"objects/{prefix}")
        try:
            with os.scandir(fd) as entries:
                names = [e.name for e in entries if re.fullmatch(r"[0-9a-f]{38}", e.name)]
            into.mkdir(exist_ok=True)
            for leaf in names:
                if (into / leaf).exists():
                    continue
                data = _read(fd, leaf, MAX_LOOSE)
                if data is None:
                    raise Refused(f".git/objects/{prefix}/{leaf[:8]}…: not a file or over {MAX_LOOSE >> 20} MB")
                budget[0] -= len(data)
                if budget[0] < 0:
                    raise Refused(f"the session's repository has over {MAX_PACK_COPY >> 20} MB of new objects")
                if _loose_sha(data) != prefix + leaf:
                    raise Refused(f".git/objects/{prefix}/{leaf[:8]}…: the content does not match the name")
                tmp = into / f".{leaf}.{secrets.token_hex(4)}"
                tmp.write_bytes(data)
                os.replace(tmp, into / leaf)
            return len(names)
        finally:
            os.close(fd)

    def _copy_packs(self, name: str, room: _Room, into: Path, budget: list[int]) -> None:
        """The room's packs that hold anything the mirror lacks, copied into `into`. Streamed — never a pack held in
        memory — into a temporary file of the platform's, then renamed. Each pack's verdict is written before the
        expensive step (`copying`), so a copy a crash cut is refused the next time, never tried again in a loop; a
        pack the room changes (its size, time or inode) is looked at afresh."""
        fd = room._sub(room.objects, "pack", "objects/pack")
        state = into.parent.parent / "packs.json"
        try:
            with os.scandir(fd) as entries:
                listed = {e.name: e.stat(follow_symlinks=False) for e in entries}
            kept = set()
            verdicts = self._verdicts(name)
            for pack, info in sorted(listed.items()):
                found = re.fullmatch(r"(pack-[0-9a-f]{40})\.pack", pack)
                if not found or f"{found.group(1)}.idx" not in listed:
                    continue
                stem = found.group(1)
                idx_info = listed[f"{stem}.idx"]
                if not (stat.S_ISREG(info.st_mode) and stat.S_ISREG(idx_info.st_mode)):
                    raise Refused(f".git/objects/pack/{stem[:13]}…: not a file")
                key = [info.st_size, info.st_mtime_ns, info.st_ino, idx_info.st_size]
                known = verdicts.get(stem)
                if known and known.get("key") == key:
                    if known.get("state") == "mirror":
                        continue
                    if known.get("state") == "copied" and (into / pack).exists() and (into / f"{stem}.idx").exists():
                        kept.add(stem)
                        continue
                    if known.get("state") == "failed":  # the room's own fault: the same pack, the same answer
                        raise Refused(known.get("why") or f"pack {stem[5:12]} is not copied")
                    if known.get("state") == "copying" and known.get("tries", 1) >= COPY_TRIES:
                        raise Refused(f"pack {stem[5:12]} failed to copy {COPY_TRIES} times in a row (the platform went down "
                                      "mid-copy) — not pushing; let the session run git gc")
                if idx_info.st_size > 128 * 2**20:
                    raise Refused(f".git/objects/pack/{stem[:13]}….idx is over 128 MB — not read so")
                tries = known.get("tries", 1) + 1 if known and known.get("key") == key and \
                    known.get("state") == "copying" else 1
                verdicts[stem] = {"key": key, "state": "copying", "tries": tries}
                state.write_text(json.dumps(verdicts))
                tmp_idx = into / f".{stem}.idx.{secrets.token_hex(4)}"
                tmp_pack = into / f".{stem}.pack.{secrets.token_hex(4)}"
                names = into / f".{stem}.names.{secrets.token_hex(4)}"
                try:
                    _copy(fd, f"{stem}.idx", tmp_idx, idx_info.st_size)
                    count = _idx_names(tmp_idx, names)
                    if self._all_in_mirror(name, names, count):
                        verdicts[stem] = {"key": key, "state": "mirror"}
                        state.write_text(json.dumps(verdicts))
                        continue
                    budget[0] -= info.st_size + idx_info.st_size
                    if budget[0] < 0:
                        raise Refused(f"the session's repository has over {MAX_PACK_COPY >> 20} MB of new objects (a pack "
                                      "after git gc?) — not pushing")
                    _copy(fd, pack, tmp_pack, info.st_size)
                    os.replace(tmp_pack, into / pack)
                    os.replace(tmp_idx, into / f"{stem}.idx")
                except Refused as exc:  # the room's: a malformed index, a pack that moved, over the cap — for good
                    verdicts[stem] = {"key": key, "state": "failed", "why": str(exc)}
                    state.write_text(json.dumps(verdicts))
                    raise
                except Exception:  # the platform's (git, the disk): nothing kept, the next look tries again
                    verdicts.pop(stem, None)
                    state.write_text(json.dumps(verdicts))
                    raise
                finally:
                    for tmp in (tmp_idx, tmp_pack, names):
                        tmp.unlink(missing_ok=True)
                verdicts[stem] = {"key": key, "state": "copied"}
                state.write_text(json.dumps(verdicts))
                kept.add(stem)
            for old in into.glob("pack-*"):  # what the room no longer has goes from the copy too
                if old.name.split(".")[0] not in kept:
                    old.unlink(missing_ok=True)
        finally:
            os.close(fd)

    def _verdicts(self, name: str) -> dict:
        try:
            data = json.loads((self.outbox / f"{name}.room" / "packs.json").read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _all_in_mirror(self, name: str, names: Path, count: int) -> bool:
        """Every object named in `names` (one per line) is in the mirror. A present one costs two bytes of output, so
        the first missing one is always within the bounded answer."""
        if not count:
            return True
        out = self._git(f"--git-dir={self.mirror(name)}", "cat-file", "--batch-check=.", stdin=names,
                        cap=2 * count + 4096, cut=True).stdout
        return " missing" not in out

    def _take(self, name: str, ref: str) -> tuple[str, str, _Room]:
        """The room's branch fetched into the outbox under `ref` from the platform's copy of its objects (`_store`).
        Returns (branch, tip, the room still open) — the caller closes it."""
        room = _Room(self.room, name)
        try:
            branch, sha = room.head()
            room.check()
            outbox = self._outbox(name)
            with self._heavy:
                store = self._store(name, room, sha)
                self._git(f"--git-dir={outbox}", "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true",
                          "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                          str(store), f"+refs/heads/tip:{ref}", protocols=("file",))
            if self._rev(outbox, ref) != sha:
                raise Refused("the branch read is not the one in the session's repository")
            return branch, sha, room
        except BaseException:
            room.close()
            raise

    def look(self, name: str, ref: str, *, tree: bool = True) -> Look:
        """The room's branch as a card shows it, fetched under `ref` of the outbox: every commit a push would carry,
        with its whole message and its own files, checked to account for every object that would leave. `tree`: also
        what in the work tree is not committed. Raises `Refused` (the room's repository as it is) or `Unavailable`."""
        if not self.ready(name):
            raise Unavailable("the mirror has not been fetched from the remote yet")
        with self._outbox_locks[name]:
            branch, sha, room = self._take(name, ref)
            try:
                outbox = self._outbox(name)
                base = self.default_branch(name)
                base_sha = self._rev(self.mirror(name), f"refs/heads/{base}")
                if base_sha is None:
                    raise Unavailable(f"the mirror has no branch {base}")
                git = f"--git-dir={outbox}"
                done = self._git(git, "merge-base", base_sha, sha, check=False)
                merge_base = done.stdout.strip()
                if done.returncode != 0 or not SHA.fullmatch(merge_base):
                    raise Refused(f"branch {branch} does not grow from {base} — they have no common commit")
                ahead = int(self._git(git, "rev-list", "--count", f"{base_sha}..{sha}").stdout.strip() or 0)
                commits, objects, size, digest = self._leaving(git, sha)
                files = self._files(git, merge_base, sha)
                touched = {f["path"] for c in commits for f in c["files"]} | {f["path"] for f in files}
                look = Look(name, branch, sha, base, base_sha, merge_base, ahead, len(commits), commits, files,
                            sorted(p for p in touched if p.startswith(WORKFLOWS)), objects=objects, bytes=size,
                            digest=digest)
                if tree:
                    look.dirty, look.partial = self._dirty(name, git, sha, room)
                return look
            finally:
                room.close()

    def _new_commits(self, git: str, sha: str) -> list[list[str]]:
        """[[commit, *parents]] the remote has on no branch, newest first; refused past MAX_COMMITS or with a root."""
        rows = self._git(git, "rev-list", "--parents", f"--max-count={MAX_COMMITS + 1}", sha, "--not",
                         "--alternate-refs").stdout.split("\n")
        new = [row.split() for row in rows if row.strip()]
        if len(new) > MAX_COMMITS:
            raise Refused(f"over {MAX_COMMITS} new commits — not pushing, split the work")
        if roots := [c[0] for c in new if len(c) == 1]:
            raise Refused(f"new root commit {roots[0][:7]}: the history is not from the remote repository — not "
                          "pushing")
        return new

    def _raw(self, git: str, shas: list[str]) -> dict[str, Commit]:
        """Each commit read from its own object (`cat-file --batch`, length-delimited — nothing in a message can
        split it): its tree, parents, author, committer and message. A header the owner cannot check — a signature
        (`gpgsig`), an embedded tag (`mergetag`), anything else but `tree`, `parent`, `author`, `committer` and a UTF-8
        `encoding` — refuses it, as does a message over MAX_MESSAGE or not in UTF-8."""
        if not shas:
            return {}
        sizes = self._git(git, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                          stdin="".join(f"{c}\n" for c in shas).encode()).stdout.split("\n")
        for row in sizes:
            parts = row.split()
            if len(parts) == 3 and (parts[1] != "commit" or int(parts[2]) > MAX_MESSAGE + 64 * 1024):
                raise Refused(f"commit {parts[0][:7]} is over {(MAX_MESSAGE >> 10) + 64} KB — not pushing")
        out = self._git(git, "cat-file", "--batch", text=False, stdin="".join(f"{c}\n" for c in shas).encode(),
                        cap=len(shas) * (MAX_MESSAGE + 64 * 1024 + 128)).stdout
        commits, at = {}, 0
        while at < len(out):
            end = out.index(b"\n", at)
            head = out[at:end].decode("ascii", "replace").split()
            if len(head) != 3 or head[1] != "commit" or not head[2].isdigit():
                raise Unavailable("git cat-file answered wrong")
            size = int(head[2])
            commits[head[0]] = _commit(head[0], out[end + 1:end + 1 + size])
            at = end + 1 + size + 1
        return commits

    def _leaving(self, git: str, sha: str) -> tuple[list[dict], int, int, str]:
        """(the new commits, newest first, each read from its own object with its message, author, committer,
        parents and own files; the objects a push carries; their bytes; a digest of exactly what leaves) — refused
        when a new commit is a root (a history the remote never had), has a header the card cannot show, when there
        are more than MAX_COMMITS, or when any object would leave that no shown commit's own change accounts for."""
        new = self._new_commits(git, sha)
        if not new:
            return [], 0, 0, ""
        raw = self._raw(git, [c[0] for c in new])
        accounted: set[str] = set()
        commits = []
        for commit, *parents in new:
            body = raw.get(commit)
            if body is None or body.parents != parents:
                raise Refused(f"commit {commit[:7]} reads differently from its parents — not pushing")
            accounted |= {commit, body.tree}
            files: list[dict] = []
            for n, parent in enumerate(parents):
                rows = self._git(git, "diff-tree", "-r", "-t", "-z", "--no-renames", "--no-abbrev", "--raw", parent,
                                 commit).stdout.split("\0")
                for meta, path in zip(rows[0::2], rows[1::2]):
                    fields = meta.lstrip(":").split()
                    if len(fields) != 5:
                        continue
                    if SHA.fullmatch(fields[3]) and fields[3] != "0" * 40:
                        accounted.add(fields[3])
                    if n == 0 and fields[1] != "040000" and fields[0] != "040000":
                        files.append({"path": path, "status": fields[4][:1]})
            commits.append({"sha": commit, "subject": body.message.split("\n", 1)[0][:200], "message": body.message,
                            "author": body.author, "committer": body.committer, "parents": parents, "files": files})
        listed = self._git(git, "rev-list", "--objects", "--no-object-names", sha, "--not", "--alternate-refs",
                           cap=48 * (MAX_NEW_OBJECTS + 1)).stdout.split()
        if len(listed) > MAX_NEW_OBJECTS:
            raise Refused(f"the push has over {MAX_NEW_OBJECTS} objects — not pushing")
        if unaccounted := set(listed) - accounted:
            raise Refused(f"the push has {len(unaccounted)} objects that are not in the changes of the commits shown — "
                          "not pushing")
        sizes = self._git(git, "cat-file", "--batch-check=%(objectsize)",
                          stdin="".join(f"{o}\n" for o in listed).encode()).stdout.split()
        size = sum(int(n) for n in sizes if n.isdigit())
        if size > MAX_PUSH_BYTES:
            raise Refused(f"the push is {size >> 20} MB — over {MAX_PUSH_BYTES >> 20} MB, not pushing")
        digest = hashlib.sha256("\n".join([*(c[0] for c in new), "", *sorted(listed)]).encode()).hexdigest()
        return commits, len(listed), size, digest

    def leaving(self, name: str, ref: str, sha: str) -> str:
        """The digest of what a push of `sha` (under `ref`) would carry against the mirror as it is now: «Send»
        compares it with the card's after fetching the mirror, so nothing the card did not list can leave."""
        with self._outbox_locks[name]:
            outbox = self._outbox(name)
            if self._rev(outbox, ref) != sha:
                raise Refused("the platform lost the card's commits — open the card again")
            return self._leaving(f"--git-dir={outbox}", sha)[3]

    def count(self, name: str, ref: str) -> tuple[str, str, int]:
        """(branch, tip, unpushed) for a ping's button: the branch fetched under `ref`, nothing more read."""
        if not self.ready(name):
            raise Unavailable("the mirror has not been fetched yet")
        with self._outbox_locks[name]:
            branch, sha, room = self._take(name, ref)
            room.close()
            outbox = self._outbox(name)
            n = int(self._git(f"--git-dir={outbox}", "rev-list", "--count", sha, "--not", "--alternate-refs")
                    .stdout.strip() or 0)
            return branch, sha, n

    def _files(self, git: str, merge_base: str, sha: str) -> list[dict]:
        names = self._git(git, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                          merge_base, sha).stdout.split("\0")
        counts = self._git(git, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--numstat", "-z",
                           merge_base, sha).stdout.split("\0")
        nums: dict[str, tuple[int | None, int | None]] = {}
        for row in counts:
            add, _, rest = row.partition("\t")
            rem, _, path = rest.partition("\t")
            if path:
                nums[path] = (int(add) if add.isdigit() else None, int(rem) if rem.isdigit() else None)
        files = []
        for status, path in zip(names[0::2], names[1::2]):
            if status and path:
                add, rem = nums.get(path, (None, None))
                files.append({"path": path, "status": status[:1], "add": add, "del": rem})
        return files

    def diff(self, name: str, sha: str, limit: int) -> str:
        """What a push of `sha` carries, commit by commit, oldest first: each with its author, committer, parents and
        its whole message from its own object (every invisible character as its code), then its own diff (a merge's
        combined one), from the outbox; at most `limit` characters."""
        if not SHA.fullmatch(sha):
            return ""
        parts, size = [], 0
        with self._outbox_locks[name]:
            git = f"--git-dir={self._outbox(name)}"
            new = self._new_commits(git, sha)
            raw = self._raw(git, [c[0] for c in new])
            for commit, *parents in reversed(new):
                body = raw[commit]
                text = "\n".join([f"commit {commit}", f"Parents: {' '.join(parents)}",
                                  f"Author: {shown(body.author, 400)}", f"Committer: {shown(body.committer, 400)}", "",
                                  *(f"    {row}" for row in shown(body.message, MAX_MESSAGE * 8, lines=True)
                                    .split("\n")), "", ""])
                patch = self._git(git, "diff-tree", "-p", "--no-commit-id", "--no-color", "--no-ext-diff",
                                  "--no-textconv", "--no-renames", *(("--cc", commit) if len(parents) > 1 else
                                                                    (parents[0], commit)),
                                  text=False, cap=limit, cut=True).stdout.decode("utf-8", "replace")
                parts.append(text + patch + "\n")
                size += len(parts[-1])
                if size >= limit:
                    break
        return "".join(parts)[:limit]

    def _dirty(self, name: str, git: str, sha: str, room: _Room) -> tuple[list[str], bool]:
        """What in the room's work tree is not as its tip has it: tracked files that differ (hashed in Python, a stat
        cache between looks; a Git LFS pointer is never taken for a change — checked in one batch), then untracked
        entries git would list (folders with no tracked file named once, as `git status` does), the ignore files read
        as `_Ignores` says. Bounded by bytes hashed and by LOOK_BUDGET_S: past either, the rest is said unchecked."""
        deadline = time.monotonic() + LOOK_BUDGET_S
        listing = self._git(git, "ls-tree", "-r", "-z", "-l", "--full-tree", sha).stdout.split("\0")
        tracked: dict[str, tuple[str, str, int | None]] = {}
        for row in listing:
            meta, _, path = row.partition("\t")
            parts = meta.split()
            if len(parts) == 4 and path:
                tracked[path] = (parts[0], parts[2], int(parts[3]) if parts[3].isdigit() else None)
        dirty, suspects, hashed, partial = [], {}, 0, False
        for path, (mode, blob, size) in tracked.items():
            if mode == "160000" or path.split("/")[0] in IGNORED_DIRS:
                continue  # a submodule: its own repository
            if time.monotonic() > deadline:
                partial = True
                break
            state = self._state(name, room.tree, path, mode, blob, MAX_HASHED - hashed)
            if state is None:
                partial = True
                continue
            changed, cost = state
            hashed += cost
            if changed and size is not None and size <= 1024 and mode != "120000":
                suspects[path] = blob
            elif changed:
                dirty.append(path)
        pointers = self._lfs(name, set(suspects.values()))
        dirty += [path for path, blob in suspects.items() if blob not in pointers]
        untracked, more = self._untracked(room, tracked, deadline)
        return sorted(dirty) + untracked, partial or more

    def _state(self, name: str, tree: int, path: str, mode: str, blob: str, budget: int) -> tuple[bool, int] | None:
        """(changed, bytes hashed) of one tracked path; None when the budget is spent."""
        folder, _, leaf = path.rpartition("/")
        try:
            fd_dir = _walk(tree, folder)
        except OSError:
            return True, 0  # its folder is gone or became a link
        try:
            info = os.stat(leaf, dir_fd=fd_dir, follow_symlinks=False)
            if mode == "120000":
                if not stat.S_ISLNK(info.st_mode):
                    return True, 0
                return blob_sha(os.readlink(leaf, dir_fd=fd_dir).encode("utf-8", "surrogateescape")) != blob, 0
            if not stat.S_ISREG(info.st_mode):
                return True, 0
            key = (name, path, info.st_size, info.st_mtime_ns, info.st_ino, info.st_ctime_ns)
            if key in self._hashes:
                return self._hashes[key] != blob, 0
            if info.st_size > MAX_FILE:
                return None
            if info.st_size > budget:
                return None
            data = _read(fd_dir, leaf, MAX_FILE)
            if data is None:
                return True, 0
            got = blob_sha(data)
            self._hashes[key] = got
            if len(self._hashes) > 200_000:
                self._hashes.clear()
            return got != blob, len(data)
        except OSError:
            return True, 0
        finally:
            os.close(fd_dir)

    def _lfs(self, name: str, blobs: set[str]) -> set[str]:
        """Which of these small blobs are Git LFS pointers (the real file is in the tree: not a change). One
        `cat-file --batch` for all of them, its output bounded."""
        if not blobs:
            return set()
        out = self._git(f"--git-dir={self._outbox(name)}", "cat-file", "--batch", text=False,
                        stdin="".join(f"{b}\n" for b in sorted(blobs)).encode(), cap=1200 * len(blobs) + 4096,
                        check=False).stdout
        found, at = set(), 0
        while at < len(out):
            end = out.find(b"\n", at)
            if end < 0:
                break
            head = out[at:end].split()
            at = end + 1
            if len(head) == 3 and head[2].isdigit():
                size = int(head[2])
                if out[at:at + size].startswith(LFS):
                    found.add(head[0].decode())
                at += size + 1
        return found

    def _untracked(self, room: _Room, tracked: dict, deadline: float) -> tuple[list[str], bool]:
        folders: set[str] = {""}
        for path in tracked:
            parts = path.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                folders.add("/".join(parts[:depth]))
        if len(folders) > MAX_DIRS:
            return [], True
        ignores = _Ignores()
        ignores.add("", room.info_exclude())
        read = 0
        for folder in sorted(folders):
            if read >= MAX_IGNORE_FILES:
                ignores.partial = True
                break
            text = _read(room.tree, f"{folder}/.gitignore" if folder else ".gitignore", 256 * 1024)
            if text is not None:
                read += 1
                ignores.add(folder, text.decode("utf-8", "replace"))
        found = []
        for folder in sorted(folders):
            if time.monotonic() > deadline:
                return found, True
            if folder.split("/")[0] in IGNORED_DIRS:
                continue
            try:
                fd = _walk(room.tree, folder)
            except OSError:
                continue
            try:
                with os.scandir(fd) as entries:
                    for seen, entry in enumerate(entries):
                        # Every entry checks the time: one folder of a million ignored files stalls nothing.
                        if time.monotonic() > deadline or seen >= MAX_FOLDER_ENTRIES:
                            return sorted(found), True
                        path = f"{folder}/{entry.name}" if folder else entry.name
                        if path == ".git" or (not folder and entry.name in IGNORED_DIRS) or path in tracked or \
                                path in folders:
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        if ignores.ignored(path, is_dir):
                            continue
                        found.append(path + ("/" if is_dir else ""))
                        if len(found) >= MAX_UNTRACKED:
                            return sorted(found), True
            except OSError:
                continue
            finally:
                os.close(fd)
        return sorted(found), ignores.partial

    def forget(self, name: str, ref: str) -> None:
        """A card's private ref, dropped once the card is done."""
        if name not in self.repos or not ref.startswith("refs/retinue/"):
            return
        with self._outbox_locks[name]:
            path = self.outbox / f"{name}.git"
            if (path / "HEAD").is_file():
                self._git(f"--git-dir={path}", "update-ref", "-d", ref, check=False)

    # --- the push -----------------------------------------------------------------------------------------------

    def branch_ok(self, name: str, branch: str) -> str:
        """'' when `branch` may be pushed to: a plain name the remote takes, never its default branch."""
        if not BRANCH.fullmatch(branch or "") or branch.startswith(("refs/", "heads/", "tags/", "remotes/")):
            return "a branch name: Latin letters, digits and . _ / -, no .. or spaces, not refs/…"
        default = self.default_branch(name)
        if branch.casefold() in {default.casefold(), "main", "master", "head"}:
            return f"not pushing to {branch} directly — only a new branch and a PR"
        done = self._git("check-ref-format", f"refs/heads/{branch}", check=False)
        return "" if done.returncode == 0 else "git will not take such a branch name"

    def remote_sha(self, name: str, branch: str) -> str | None:
        """The remote's refs/heads/<branch> now (`ls-remote`): None when it has none."""
        repo = self.repos[name]
        out = self._git("ls-remote", "--refs", repo.url, f"refs/heads/{branch}", protocols=self._scheme(repo),
                        auth=repo, timeout=FRESH_TIMEOUT_S).stdout
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref == f"refs/heads/{branch}" and SHA.fullmatch(sha):
                return sha
        return None

    def push(self, name: str, ref: str, sha: str, branch: str, *, title: str = "", body: str = "") -> Pushed:
        """`sha` (the card's, under `ref` of the outbox) to `refs/heads/<branch>` of the remote in platform.yaml:
        never forced, never a tag, never the default branch. On GitLab the merge request comes with the push."""
        repo = self.repos[name]
        if repo.off:
            return Pushed(False, said=f"The repository is off: {repo.off}.")
        if why := self.branch_ok(name, branch):
            return Pushed(False, said=f"Not pushing: {why}.")
        with self._outbox_locks[name]:
            outbox = self.outbox / f"{name}.git"
            if self._rev(outbox, ref) != sha:
                return Pushed(False, said="The platform lost the card's commits — open the card again.")
            options: list[str] = []
            if repo.host == "gitlab.com":
                base = self.default_branch(name)
                options = ["-o", "merge_request.create", "-o", f"merge_request.target={base}",
                           "-o", f"merge_request.title={_one_line(title, 250)}",
                           "-o", f"merge_request.description={_one_line(body, 1000)}"]
            done = self._git(f"--git-dir={outbox}", "push", "--porcelain", "--no-verify", "--no-follow-tags",
                             *options, repo.url, f"{sha}:refs/heads/{branch}", protocols=self._scheme(repo),
                             auth=repo, check=False)
        out, err = plain(done.stdout, repo.secret), plain(done.stderr, repo.secret)
        mark = next((row.split("\t", 1)[0] for row in done.stdout.splitlines()
                     if f":refs/heads/{branch}" in row), "")
        if done.returncode == 0 and mark in ("*", " ", "="):
            url = ""
            if repo.host == "gitlab.com":
                found = re.search(r"https://gitlab\.com/\S+/-/merge_requests/\d+", done.stderr)
                url = found.group(0) if found else ""
            return Pushed(True, url=url)
        if mark == "!" or "non-fast-forward" in out or "fetch first" in out:
            return Pushed(False, said=f"The branch {branch} on the remote is already different — reply to the card with "
                                      "another branch name.")
        if "workflow" in err and "scope" in err:
            return Pushed(False, said="The token does not let edits to .github/workflows through — you push those yourself.")
        log.warning("push of %s to %s refused: %s", name, branch, err or out)
        return Pushed(False, said=f"The push failed: {(err or out or f'code {done.returncode}')[-300:]}")

    async def open_pr(self, name: str, branch: str, title: str, body: str) -> tuple[str, str]:
        """(url, said) of a pull request from `branch` into the default branch on GitHub: opened, or the open one
        that is already there. The token goes in this request's header only."""
        repo = self.repos[name]
        if repo.host != "github.com" and not repo.api:
            return "", ""
        api = (repo.api or GITHUB_API).rstrip("/")
        headers = {"Authorization": f"Bearer {repo.secret}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "retinue-platform"}
        base = self.default_branch(name)
        owner = repo.path.split("/", 1)[0]
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_S)
        proxy = self.cfg.proxy or None
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as http:
                async with http.post(f"{api}/repos/{repo.path}/pulls", headers=headers, proxy=proxy,
                                     json={"title": title, "body": body, "head": branch, "base": base}) as res:
                    data = await _json(res)
                    if res.status == 201 and isinstance(data.get("html_url"), str):
                        return data["html_url"], ""
                    exists = res.status == 422 and "already exists" in json.dumps(data)
                    if not exists:
                        said = plain(str(data.get("message") or res.reason or ""), repo.secret)[:200]
                        return "", f"The PR did not open: GitHub answered {res.status} {said}".strip()
                async with http.get(f"{api}/repos/{repo.path}/pulls", headers=headers, proxy=proxy,
                                    params={"head": f"{owner}:{branch}", "state": "open"}) as res:
                    data = await _json(res, list)
                    found = next((p.get("html_url") for p in data if isinstance(p, dict)), None)
                    return (found, "") if isinstance(found, str) else ("", "The PR already exists, but GitHub gave no link.")
        except (aiohttp.ClientError, TimeoutError) as exc:
            return "", f"GitHub did not answer ({type(exc).__name__}) — the branch is pushed, open the PR on GitHub."


def _one_line(text: str, cap: int) -> str:
    """A push option's value: git refuses a newline in one."""
    return re.sub(r"\s+", " ", text).strip()[:cap]


async def _json(res: aiohttp.ClientResponse, kind: type = dict):
    try:
        data = await res.json(content_type=None)
    except (ValueError, aiohttp.ContentTypeError):
        return kind()
    return data if isinstance(data, kind) else kind()
