# Your Base in the server's sessions

Sessions on the server can work in your knowledge base (the Base) as on your Mac: they see all of it, read and edit
its records, and what they changed is committed — by the platform itself after a run, for what the assistant may
write, with «Undo»; by your button for the rest. What the Mac's agent did by hand — `git pull` at the start, "commit
and push?" at the end — is the platform's job here: it pulls the hub before each run, commits with your name, and
pushes to the hub itself. That lane is the room, which also reads the
web ([platform.md](platform.md#the-room): what that gives up and what stands in the way).

```
your Mac ── push/pull over the VPN ──┐
                                      ▼
            /srv/retinue/memory/hub.git   (bare; pre-receive = kbcheck.py; docs/memory.md)
              ▲ push (your role: the platform is not uid 10001)        ▲ the router's pushes (the assistant)
              │                                                        │
 platform (10007): /platform/base.git ── the clone's repository, never in a lane
              │ refresh: the hub's changes into clean files · commit: your button
              ▼ read-write
 /srv/retinue/base/tree ══ lane-private (10005): /work/base ── the sessions edit files; no .git there
```

## Where things are

| What | Host | lane-private | platform |
|---|---|---|---|
| The clone's files | `/srv/retinue/base/tree` | `/work/base` (rw) | `/lanes/private/base` (rw, over the read-only lane view) |
| The clone's repository and state | — | — | `/platform/base.git` (volume `platform-data`): objects, `HEAD`, `base.sqlite` |
| The hub | `/srv/retinue/memory/hub.git` | — | `/hub` read-only, `objects/` and `refs/` read-write |

`platform.yaml`:

```yaml
base:
  author: "Your Name <you@example.com>"   # the commits the button makes are yours
  tree: base                              # the clone's folder under the lane's /work
  lane: private                           # refused if the lane is retired
  # hub: /hub, mount: <work_mount>/<tree>, git_dir: /platform/base.git, policy: <hub>/hooks/kb-policy.json,
  # protect: [scripts/**, justfile, Justfile, '**/*.py', '**/*.sh'], max_file_mb: 10, every_s: 60
tracks:
  - {name: base, lane: private, kind: private, cwd: base}
spheres:
  - {name: travel, title: Travel, icon: "✈️", track: base, cwd: base/travel}   # cwd: optional
```

The example `platform.yaml` gives spheres no `cwd`: their sessions start in the root of the Base. A sphere's `cwd`
(inside its track's), when you set one, wins over the track's: a session of `travel` runs in `/work/base/travel` —
«в Пространстве» — and the repository's rules reach it as on the Mac: `base/CLAUDE.md` and its `@AGENTS.md`
([instructions.md](instructions.md)). Every consumer of a session's directory — the run's `cwd`, `out/` and its
attribution, the face's diff, a fork — uses the directory stored with the session (`sessions.cwd`), not the track's.
A fork runs where its parent ran; it is refused («the session's track is now … in another folder») only when the configuration
would put a new session of that sphere elsewhere.

The lane is told about the Base by `deploy/lanes/instructions/base.md` (baked into lane-private's image): the platform
pulls before each turn and commits by «To the Base»; never run `git` in `/work/base` (it has no repository), never offer
"commit and push?"; name the changed records in one line of the summary; `python3 scripts/lint.py` is the
Base's check (`just` is not in the image).

## Who writes the tree, and why this way

Both sides write the same files: the lane (uid 10005) edits them, the platform (uid 10007) puts the hub's version
over a clean file and its merge over a dirty one, and the lane must keep editing what the platform wrote. They share
a group, `retinue-base` (gid 10020 — no service's uid or gid; the retired lane-web's user is 10008):

- **The folder** `/srv/retinue/base/tree` is root's, group `retinue-base`, mode `2775` (`setup.sh`). Root's, so no
  session can take the group's write away from it or move it: it is a bind mount in both containers (a mount point
  cannot be renamed — `EBUSY`). Setgid, so every file and folder made inside takes the group.
- **Both containers** have the group (`group_add`), the platform also `retinue-memory` (10001) for the hub.
- **The lane's files** are group-writable: the lane host sets `umask 002` before it starts any CLI.
- **The platform's writes** are a new file in the folder (`.retinue-<random>.tmp`, `0664`, or `0775` for a file the
  hub has executable), renamed over the name; the folders it makes are `2775` whatever the umask.

Why a host folder and not a `volume.subpath` of `lane-private-work`: a subpath must exist before the platform starts
(setup would have to reach into Docker's volume), and a session could rename it and make a new one, which the
platform's mount — bound at start — would not follow. A bind mount cannot be moved from inside, setup makes it with
one `install -d`, and the nightly backup covers `/srv/retinue` (uncommitted edits included). As compose mounts it,
the platform writes the clone through the lane view's own path (`/lanes/private/base`), so there is nothing to
compare; only when `mount` is set to another path does every call check that both are one folder (`st_dev`/`st_ino`)
and refuse with «restart the platform» when not.

**Mount order.** The platform's read-write bind sits inside its read-only mount of `lane-private-work`, so its mount
point `base` must already exist in that volume — `lane-private` makes it when it starts with its own bind at
`/work/base`. Compose starts `lane-private` first (`depends_on`); `up --no-deps platform` on a volume `lane-private`
never mounted the Base into fails with `EROFS` — start `lane-private` once first.

What a session can still do: `chmod` its own files or folders so the platform cannot replace them. Such a file is
then held (below), never forced; the folder stays the group's, and the platform's own files are not the lane's to
`chmod`.

## The tree is hostile input

The clone's files come from a model in a lane. In the platform, nothing in them may run a command, make it follow a
link out, or make it wait:

- **Git never looks at the tree.** The repository is the platform's (`--git-dir` on every call, never a work tree),
  with `core.hooksPath=/dev/null`, `core.fsmonitor=false`, `core.attributesFile=/dev/null`, `GIT_CONFIG_NOSYSTEM`,
  `GIT_ATTR_NOSYSTEM` and a global config of its own that only names the hub as safe. So a `.git/` folder, a hook, a
  config, a `.gitattributes` (filters, drivers) or a `.gitmodules` in the tree is never read by git: hashing a file,
  writing one, deleting one, finding what changed is done in Python (`retinue/base.py`). A commit is built in a
  temporary index from the hub's `main` (`read-tree`), the chosen files as blobs (`hash-object --no-filters
  --stdin`), `write-tree`, `commit-tree`.
- **Nothing is followed.** Every folder on a path is opened with `O_NOFOLLOW | O_DIRECTORY` from its parent's
  descriptor, every file with `O_NOFOLLOW | O_NONBLOCK` and checked from its descriptor to be a regular file (inode
  unchanged since the listing). A link, a FIFO, a socket or a device is named as what it is (`changed` lists it, a
  commit refuses it), never read through and never waited on. The walk stays on the tree's file system, stops at 40
  folders deep and 50 000 entries, folders counted; a file over `max_file_mb` (10) is neither read nor committed —
  its digest is its size and time — and a diff side over 512 KiB is named, not diffed.
- **Nothing in it runs here.** When the platform pushes, the hub's pre-receive runs in the platform's container —
  including the Base's own lint (`python3 -I scripts/lint.py`, from a copy of the new tree). So a commit never carries
  a path that check runs, or code at all (`protect`: `scripts/**`, `justfile`, `**/*.py`, `**/*.sh`; and any path
  the policy's `check` command names, whatever `protect` says), nor a hidden
  one (`.gitattributes`, `.claude/…`), nor a name git would take for its own (`.git`, `.GIT.`, `git~1`): those are
  refused («edited only from the Mac»). The lint the hub runs is therefore always the one your Mac pushed. Git, the hook
  and the lint get an environment of `PATH`, `HOME`, `LANG`, `TMPDIR` and git's own variables only — none of the
  platform's secrets.
- **Writes check what they replace.** A write or a delete in the tree first checks the path is still what was read
  (size, mtime, inode); otherwise the path is held, never clobbered.

## Refresh

`refresh()` brings the clone up to the hub's `main`. It looks at the hub's ref first and stops there when the hub
has not moved. Otherwise it fetches, and for every file the hub changed since the clone's `HEAD`:

- **clean here** (as `HEAD` has it) → the hub's version is written (or the file deleted, and the folders it leaves
  empty);
- **already as the hub has it** → nothing;
- **dirty here** → `git merge-file` (ours = the tree, base = `HEAD`, theirs = the hub); a clean merge is written;
- **otherwise held**: the tree's file stays as it is, and its base (the blob the session's edit started from) is
  kept in `base.sqlite`, so a later commit merges against that base and cannot silently undo the hub's change (a lost
  update). A held file put back to the hub's version (or deleted where the hub has none) is no longer held.

One path that cannot be done — a conflict, a link on the way, a folder in a file's place — is held and never stops
the rest. Then `HEAD` moves to the hub's commit. Each refresh that moved is journaled in `base.sqlite` (`journal`)
and in the log.

When: before each run of a session whose directory is in the clone; at the platform's start (the first clones the
hub into the empty folder); and every `every_s` (60) seconds. **Never beside a run**: under its lock, a refresh asks
whether any run on the clone's lane is open — every session there can write `/work/base`, the Base's own or not — and
does nothing if so; a run that starts meanwhile stops its writes at the next path. The pre-run refresh never waits
either: while a commit or another refresh holds the clone (a push waits for the hub's lint; the first clone takes a
while) the run starts at once on the clone as it is, as it does when the refresh fails. No run starts while `drain`
waits on the Base.

## Commit

`commit(paths, subject, body, expect=…, session=…, role=…)` — on your press ([the button](#the-button)), or the
platform's own after a run with the assistant's `role` ([below](#the-platforms-own-commit)):

1. **Which paths may go at all.** Outside the Base (`..`, absolute, control characters) and what never goes to it
   (`out/`, `inbox/`, the policy's `excluded`, untracked files under the Base's `.gitignore`) → `left_out`. Hidden,
   `.git`-ish, protected, not UTF-8, a link, a FIFO, a folder, too big → `refused`. So is a path that is no longer
   what you saw: `expect` maps a path to the digest `snapshot()` gave when the card was made (`''` : no file), and a
   path whose digest is now another is refused («the file changed after the card»), under the lock, before anything
   is staged.
2. **Against the hub's `main` now**, path by path: base = the held base or `HEAD`'s blob. The hub has not changed it
   → the tree's version; the tree did not change it, or already has the hub's → `left_out` («not changed here», «already
   so in the Base»); both changed → `git merge-file`, a conflict → `conflicts` (that path stays uncommitted, the rest
   goes).
3. **Built without touching anything else**: a temporary index from the hub's `main` plus only those paths;
   `commit-tree` with the hub's head as parent; author and committer = `author` from the config; a trailer
   `Pushed-by: retinue-platform (the owner's button, session <id>)`. Other sessions' uncommitted files are not looked at.
4. **Checked the hub's way first** (`kbcheck.check`, the owner's role, without the lint): a path it refuses (an `id`
   renamed, a symlink, a name that collides on a Mac) goes to `refused` with its reason and the rest is built again.
5. **Pushed**, up to four times: when the hub moved meanwhile (the router pushes after the assistant's turns, the
   Mac at any time) it fetches and does 2–4 again on top. The hub's own refusal (its lint) comes back as its lines
   in `refused`, nothing pushed.
6. **The clone follows**: a merged file is written into the tree, then `HEAD` moves to the pushed commit the way a
   refresh does, so the committed paths are clean and the hub's other changes are in. While a run on the lane is
   open, only the committed paths are written: the hub's other changes are held with their base (not listed by
   `changed`, never committed back over the hub's) and come in with the next idle refresh.

A path that would turn a file of the Base into a folder or a folder into a file is refused («the Base has a file where
the folder justfile would be», «the Base has a folder in this place»): the index would otherwise drop the file under it silently —
`justfile/x.md` would delete the protected `justfile`. And after `write-tree` the commit is compared with the hub's
`main`: if it changes any path no one chose, nothing is built. A push that times out is refused («the hub did not answer —
check and press again»); if it did land, the next press finds those files «already so in the Base» and commits nothing
twice.

`Committed`: `sha` (None: no commit), `files` (`Change(path, A|M|D)` as committed), `conflicts` (paths),
`refused` and `left_out` («<path>: <why>», Russian; `refused` may also hold a line about the whole change, «just
check: …»). One commit at a time: `refresh` and `commit` share a lock and run in a worker thread; `changed`,
`snapshot` and `diff` read and may run at any time. Every call raises `Unavailable` (an `OSError`) when the clone's
folder is missing or is not the lane's, and `commit` when the hub does not answer.

Because the platform is not uid 10001, the hub takes its push for yours: your rules, not the assistant's —
which is why the platform itself refuses what it must never carry from the server. Like the router, the platform
could write the hub's objects and refs past the hook: it is code you trust; the model never sees the hub.

## What the button reads

`retinue/base.py`, `BaseRepo`:

- `tree_rel` — the clone's folder under the lane's `/work`;
- `changed() -> list[Change]` — uncommitted against the clone's `HEAD`; ignored and excluded paths left out;
- `snapshot() -> dict[path, digest]` — every file not ignored: its blob id (stat-cached), `link:…`, `other:…`, `big:…`;
- `diff(paths, limit) -> str` — a unified diff against what the edit started from, at most `limit` characters;
- `await refresh(wait=True) -> Refresh(head, updated, held)` — None with `wait=False` when the clone is held;
- `busy` — a thread-safe callable the platform sets: whether a run on the lane is open;
- `await commit(paths, subject, body, expect=None, session="") -> Committed`;
- `Platform.base` — the `BaseRepo`, set in `Platform.__init__`, None when `base:` is off. The refresh before a run
  sits in `Platform._start` after the fork check and before the run is recorded and sent.

## The button

The clone, its refresh and the commit are above (`retinue/base.py`). The button adds **who changed what** and
**your press**, «To the Base» (`retinue/tobase.py`). Nothing the assistant may not write is committed without your
press, and the press is yours only: through the bridge (the router's token) or the app (its login) — never from a lane.

- **Who changed what.** Each run on the Base's lane is watched — a session in the clone, and one that is not (a
  notes session at `/work` can Edit `/work/base/…` as well); never a side question, which only reads — in windows: the tree's snapshot (path → digest) when the run starts, and again
  at each of its marks — a `result` (a turn ended; a run can hold several) and its end. What changed between them is
  recorded for the session in SQLite (`base_files`), cross-checked with the paths its own file tools named in that
  window: one only the tools name (a snapshot that missed a stat) is recorded too; one only the snapshot names (a Bash
  edit, or another run in the same window) as well. **Two runs that overlap both get a file changed in the overlap**,
  and the card marks it ⇄: you decide whose it is. Only a path the clone says is uncommitted is kept: a file committed
  by any button, or refreshed to the hub's content, leaves every session's list — checked at every mark, every run's
  start, after a commit and once a minute. The marks are taken on the poll after their lines, and before a new run on
  the lane starts; every window is a row, so a restart goes on where it was. A run whose first snapshot failed is
  still watched through its file tools. «Uncommitted» is `changed()`'s word: a held path (the hub's change waiting
  for an idle refresh, the file untouched here) is not in it, so it is on no list and is nobody's either.
- **Nobody's files.** An uncommitted file no session claims (changed before this, or by something no window saw) is
  «nobody's», and never hidden: every card lists such files at its end, turned off, for you to turn on; the feed has a
  «Decide» row «Nobody's Base files: N» (its button opens the card of the latest session on the lane).
- **The card.** «To the Base · N» under the ping sends a card: «To the Base · <sphere or track> · session <id>», «Subject: …», the
  files (☑/☐, A/M/D, +/− lines; what does not fit in 2800 characters is «…and N more files: not committed from Telegram — they
  are in the app», and the copy's «Commit» commits only the files it lists), how many files
  other sessions have uncommitted and in how many sessions — never which — and buttons: a toggle per file (the first
  12; the rest in the app), «Commit», «Open diff» (the diff of the files turned on, a `.diff` file of at most
  1 MB), «Cancel». The status lines are cut at 700 characters, so a copy stays under 4000. **A reply to the card
  sets the subject** (a spoken one loses its «(by voice)»; a reply to a card no longer open is taken and answered
  «This card is no longer active — open «To the Base» again», never sent into the session); by default it is
  `<Space of the first file>: <session title>`, the Base's own style. Pressed again, «To the Base» sends a fresh copy of the
  same card: every copy and the app show one card (the app's event `base`; a change in either place rewrites the
  other).
- **«Commit»** commits the files turned on, each only as the card showed it (its digest when the card opened:
  a file changed since brings the card back with what changed, and nothing goes), and only while the session is not
  running. The card moves to «Committing…» once — a second press anywhere gets «Committing — wait.», one after «Already
  committed.» — and the Base's `commit()` runs in the background (subject yours; body `Retinue: session <id>,
  card <id>.`; `expect`, each path's digest as the card showed it, which the Base checks again under its own lock
  and refuses a file that moved). No run on the Base's lane starts while a commit is in flight: it stays queued
  («waiting for room on the lane») and goes when the commit ends — so the commit never stages a file a run is writing, and
  neither the drain nor `busy` waits on the other. The card is rewritten with «Committed: <sha7> · N files» or «Nothing committed», and what
  was refused or conflicted, and why; those files stay on the session's list and on the next ping's button. A card a
  restart left «Committing…» is settled from the clone when the platform is back: the files no longer uncommitted went
  in, the rest are shown again for one more press («A platform restart cut the commit off — press «Commit»
  again.»); while the Base cannot be read, the clock tries again each minute.
- With no Base configured, rows left from before show nowhere: no count in the app, no feed row, no ping button.
- A card goes **stale** when the session's next ping comes (the button is on that one), is **cancelled** by «Cancel»
  (its files stay on the list), **expires** after 7 days like an approval. In the app it can also be **discarded**:
  «Discard…», asked once, puts each file turned on back as the clone's HEAD has it — an added file deleted, an edited
  or deleted one restored, a file the hub moved meanwhile taking the hub's version — only if it is as the card showed it
  and only while the session is not running (`BaseRepo.discard`, under the same lock as a commit; nothing is committed).
  What did not go back stays, with why. Its buttons are never spent: the card's
  state answers each press.

The feed has a «Decide» row per session with Base files uncommitted («To the Base: N files»), in the app with the button
in place ([face.md](face.md#the-feed)); `/wait` words it as [platform.md](platform.md) says.

## The platform's own commit

Your rule for the Mac's agent is "commit and push yourself when it is safe"; on the server the platform does that
(`tobase.ToBase._autocommit`), within the rights the hub already gives the assistant, who pushes by herself:

- **When.** A session whose run ended waits until no run is open on the Base's lane and every ended run's last window
  is marked — until then a file's owner is not known: another run may claim it too. Then, one session after another, while runs on the lane wait for it
  (`before_start`, as for a press). A restart in between leaves those files to the button.
- **What.** The session's files no other session claims (no ⇄, no «nobody's»), added or edited (never a deletion),
  and not held back by your «Cancel» on one of its cards (until the file is edited again), as the snapshot has them — committed by `BaseRepo.commit(…, role="assistant")`: `kbcheck` with the assistant's rules
  (her `writable` folders, a Showcase only below its marker), and never a Record whose `written_by` is yours. A path
  those refuse stays uncommitted; the hub's own lint runs as for any push.
- **What you see.** A card (`auto`) kept only when something went: «Committed by itself after the turn: <sha7> · N
  files», the commit's own diff, and **«Undo · N in the Base»** on the turn's ping and «In the Base · N» under its
  summary in the app. The trailer says `Pushed-by: retinue-platform (the platform's own commit after a turn, session
  <id>)`. What it left keeps «To the Base · N».
- **«Undo»** (`BaseRepo.revert`): for 7 days, while the session is not running, a new commit on top of the hub's
  `main` — history is never rewritten — puts each file back as it was before that commit, only if neither the hub nor
  the clone changed it since («changed here since», «changed in the Base since»: left as it is, never merged). The card
  ends «Undone»; the ping loses its button.

Why the assistant's rights and not yours: a session in the room reads the web, so what it writes is committed no wider
than what the assistant — the other agent that reads the outside world — may push unasked; anything wider is your
press.

## Reading the Base in the app

The app's «Spaces» ([face.md](face.md#in-the-app)) read the Base through `GET /api/spaces…` on the face's API,
behind its login (`retinue/spaces.py`). They read **what the Base committed**, never the tree: every read is git on the
platform's own repository (`/platform/base.git`, through `BaseRepo.repo` and `BaseRepo.env` — no hook, no attributes,
no config of the tree) at its `main`, the hub's commit the clone is on as of the last refresh. A session's draft in
`/work/base` is not shown until it is committed; a link, a FIFO or a 10 GB file a session left in the tree is never
opened, because the tree is never opened. The answers carry `head`, that commit, and are cached by it: a new commit
(a refresh, a press of «To the Base») drops the cache.

- **A Space** is a top-level folder whose `AGENTS.md` is a regular file of the commit and whose name is
  `[a-z0-9][a-z0-9_-]*` (64 at most): so not `.cache`, not a folder of no Showcase, not a Showcase that is a link.
- **A Showcase** is split at the line that starts with `<!-- Витрина:`. Above it: the rules (the title, its first
  `#` heading, left out), shown folded. Below: the lines by heading (`##` to `######`, not inside a code block), each
  a top-level list item with what is indented under it, or a paragraph. A heading with «Открыт… вопрос…» holds the Open
  questions, one that starts with «Задач» the Tasks, «Запис» the Records (not shown: the app lists the Records from the
  commit). Their counts are list items; «Пока … нет» there is a placeholder, not a line. No marker: all of it is rules.
- **The grid** (`GET /api/spaces`) reads only each Showcase; a Space's Records are read when that Space is opened,
  and each only for its head — a Record up to 64 KB in one batch, a bigger one only its first 16 KB.
- **The Records** of a Space are its `knowledge/`, `profile/`, `journal/`, `artifacts/` files, with one level of
  subfolders, each with what its head says of `id, title, type, status, date, verified, stale_after, updated, from,
  until, practice` (the Journal newest first, the rest by title); at most 3000 a Space, 8 MB read for them.
- **A Record** is its head, parsed as safe YAML (`yaml.safe_load` of at most 16 KB; dates as ISO text; a head of more
  than 2000 values — an alias bomb — or one that does not parse is no head, and the whole file is the body, with
  `bad_head`), and its body as markdown. Its relative links become the app's routes — `[x](../profile/p.md)` →
  `/spaces/<space>/r/profile/p.md`, a Space's `AGENTS.md` or folder → `/spaces/<space>` — when the target is a file
  of the commit the path rules allow; web and mail links stay; any other link (to `.cache`, a script, a missing
  file, an absolute path, an anchor, `javascript:`, `data:`) is dropped to its text. A reference-style definition
  (`[x]: path`) is resolved the same way, or removed, so its uses stay as text. Code is left as it is. Images and other files are not
  served.
- **The path rules**, for a Record asked for and a link alike: `*.md`, at most two folders below its Space
  (`knowledge/acme/x.md`), no segment that starts with a dot, no `..`, `.` or empty segment, no control character
  or backslash, at most 512 characters; a regular file of the commit (mode `100644`/`100755` — never a link, `120000`,
  nor a submodule); at most 512 KB (bigger: 413, and listed with `big`). A Showcase is read up to 256 KB.
- With no Base configured the API answers 404 «The Base is not connected.»; while the repository cannot be read (not
  cloned yet, git failing), 503.

## Turning it on

Needs the hub (`MEMORY=1`, [memory.md](memory.md)) and the platform (`PLATFORM=1`). On the server:

```bash
cd /opt/retinue && git pull
sudo TELEGRAM_OWNER_ID=<id> bash deploy/setup.sh            # the group retinue-base, /srv/retinue/base/tree
sudoedit /srv/retinue/platform/platform.yaml                 # `base:`, the track `base`, the spheres' track/cwd
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build lane-private platform
docker compose -p retinue logs --tail 50 platform | grep -i base   # «base refresh <sha>: …» — the first clone
```

`lane-private` is rebuilt for its instructions (`base.md`) and its group; the platform clones into the empty folder
at start. Check from the lane: `docker compose -p retinue exec lane-private ls /work/base` lists the Base, and
`… exec lane-private sh -c 'touch /work/base/x && rm /work/base/x'` works.

## When something goes wrong

- **The hub is unreachable or empty** — refresh logs `Unavailable`, runs go on with the clone as it is, a press says
  the Base is not available.
- **The push is refused for a reason that is not a moved hub** (a permission on `objects/`, say) — `refused` says the
  hub's last line; the log has it in full. Check the platform's `group_add` and the hub's `objects/`, `refs/` modes
  (`setup.sh` with `MEMORY=1`).
- **A held file** — it stays as the session left it; the owner's card shows it as a conflict when pressed. Either put
  it back to the hub's version (it stops being held at the next refresh) or edit it on the Mac and pull.
- **The platform's repository is lost** (`platform-data` gone) — the next start makes a new one; files the tree has
  that differ from the hub are held with no base and can only be committed after being reconciled by hand.
- **The lane's folder and the platform's differ** (only with a `mount` of its own) — every call refuses with «restart
  the platform».
- **`up` fails with `EROFS` on `/lanes/private/base`** — the platform was started before `lane-private` ever mounted
  the Base: `docker compose … up -d lane-private`, then the platform.
