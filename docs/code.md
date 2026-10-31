# Your repositories in the room

Sessions on the server work in your repositories as on your Mac: each is an ordinary git clone at
`/work/code/<name>` of `lane-private`, where a session runs `git status`, `log`, `diff`, `blame`, makes branches and
commits, `git pull`s fresh code, runs the tests and installs packages. What leaves the server is yours to decide:
**«Push · N»** under the session's summary opens a card, and «Send» pushes the session's branch to the
remote as a new branch and opens a pull request (GitHub) or a merge request (GitLab). Never the default branch, never
a force push, never a tag. Your git token stays with the platform; no lane ever holds one.

```
GitHub / GitLab ◄── platform-egress (CONNECT: github.com, api.github.com, gitlab.com) ◄── platform (10007)
                                                                                   │ fetch: with your token
            /srv/retinue/code/mirrors/<name>.git   (bare; the remote's branches and tags)
              │ read-write in the platform (/code/mirrors)          │ read-only in lane-private (/lane/mirrors)
              │                                                     ▼
              │                                   /work/code/<name> = git clone /lane/mirrors/<name>.git
              │                                     (the lane host, once; origin is the mirror: pull needs no token)
              ▼
 /platform/code/<name>.git  (platform-data; the mirror its alternate) ◄─ the session's branch, fetched for a card
              └── push <sha>:refs/heads/<branch> to the URL in platform.yaml, with your token ──► the PR / MR
```

`platform-egress` lets through `api.elevenlabs.io` too, for voice in the app ([platform.md](platform.md#voice)).

## Where things are

| What | Host | platform | lane-private |
|---|---|---|---|
| The mirrors | `/srv/retinue/code/mirrors` (10007, 0755) | `/code/mirrors` (rw) | `/lane/mirrors` (ro) |
| The tokens | `/srv/retinue/code/tokens.env` (root, 0600) | its environment only (`env_file`) | — |
| The clones | volume `lane-private-work` | `/lanes/private/code` (ro) | `/work/code` (rw) |
| What cards fetched | volume `platform-data` | `/platform/code/<name>.git` | — |
| The way out | `/srv/retinue/egress/platform-hosts.txt` | `platform-egress:3128` | — |

`platform.yaml`:

```yaml
code:
  lane: private                        # the room: the lane whose /work holds the clones
  track: code                          # `/new code <repo> [task]` starts a session in /work/code/<repo>
  proxy: http://platform-egress:3128   # the platform's way out, for git and the API only
  # author: "Name <email>"             # the clones' git identity; default: base.author
  # root: code, every_s: 300, mirrors: /code/mirrors, lane_mirrors: /lane/mirrors, outbox: /platform/code
  repos:
    - {name: retinue, url: "https://github.com/you/retinue.git", token: you}
    - {name: acme-api, url: "https://github.com/acme-corp/api.git", token: acme_corp}
    - {name: example-app, url: "https://gitlab.com/example-group/example-app.git", token: example_gitlab}
```

`name` is the clone's folder (a-z, 0-9, `-`, `_`); `url` is https on github.com or gitlab.com and nothing else (the
platform's proxy reaches only those); `token` names the token, `RETINUE_GIT_TOKEN_<TOKEN>`; `default_branch` is
optional (otherwise the remote's HEAD). A repository whose token is missing is **off**: it is not fetched or pushed,
a card says why («no token RETINUE_GIT_TOKEN_…»), and nothing else stops.

## Tokens

One token per **resource owner**: a fine-grained GitHub token belongs to one user or one organisation, so your own
repositories share one (`you`), and each organisation has its own (`acme_corp`, `example_gitlab`, …).

**GitHub, fine-grained** (Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate):
resource owner — you or the organisation (it must allow fine-grained tokens, and may have to approve the request);
repository access — *Only select repositories*, the ones in `platform.yaml`; repository permissions — **Contents:
Read and write**, **Pull requests: Read and write**, **Metadata: Read-only** (always on); nothing else — in
particular not *Workflows*, so a branch that changes `.github/workflows/` cannot be pushed (the card says so before
any push). An expiry of 90 days is sensible: when it lapses, the mirror's fetch fails in the log and the card says
the repository does not read; put the new one in the same way. Protect your default branches on GitHub (require a
pull request): the platform never pushes there, and then GitHub refuses it too.

**GitLab, project access token** (the project → Settings → Access tokens): role **Developer**, scopes
**`read_repository`** and **`write_repository`**. No `api`: the merge request is made by the push itself (push options
`merge_request.create`, `.target`, `.title`, `.description`), and its link comes back in the push's answer.

**Putting one in.** On the server, in your SSH terminal — never in a chat, an issue or a session:

```bash
sudo bash /opt/retinue/deploy/code/set-git-token.sh you     # reads it with echo off
pass show <entry> | sudo bash /opt/retinue/deploy/code/set-git-token.sh you   # or from a pipe
```

The script writes `RETINUE_GIT_TOKEN_YOU=…` into `/srv/retinue/code/tokens.env` (root's, 0600; the one line
replaced), prints only the name and the length, and the restart of the platform. compose hands the file to the
platform alone.

**Where the token goes, and where it never does.** The platform reads it from its environment at start and takes it
out of that environment then (`os.environ`), so no child it starts inherits it. For a git call it writes
`username=…\npassword=<token>` into a pipe and gives that git call the pipe's descriptor; git's credential helper (the
platform's own `platform.gitconfig`) reads it once, on GitHub's or GitLab's 401. So the token is never in a URL, an
argument (`/proc/<pid>/cmdline`), an environment (`/proc/<pid>/environ`), a config file or the mirror; redirects are
not followed; `transfer.credentialsInUrl=die`; and every line of git's output passes `code.plain`, which masks it
should a remote echo it. The helper answers only the protocol and host of the repository's own URL — never another
host, never a proxy's challenge. A pull request is opened with the token in that request's `Authorization` header only.
Nothing of it enters the room: the clones fetch from the mirror, a path, with no credential at all. Every https call
of git and of the API goes through `platform-egress` (`http.proxy` in the platform's git config, the `proxy` of
`code:`, required with repositories: the config refuses to load without it); the platform has no other way
out.

What this does not cover, plainly: the platform's container holds the token the way it holds the subscription token —
in the start-up environment Docker gave it, which `/proc/<pid>/environ` of the platform (not dumpable) and of
Docker's init (PID 1, the platform's user) keep as it was, and in the platform's memory. Anything that runs code as
the platform's user inside its container — the parse worker that reads your pictures (`inbox`), if a crafted file took
it over — could read PID 1's environment. That worker is the same exposure for the subscription token today; it is
not redesigned here.

## The room's clones

The platform fetches each mirror at its start, every `every_s` (300) seconds, before a run whose folder is in that
repository (at most 60 s; past that the run goes on the mirror as it is), and after a push. A repository whose first
mirror is still being fetched holds its first run in the queue («waiting for room on the lane») until the mirror is there.

The lane host makes `/work/code/<name>` on the first run of any session in the room after the mirror appears:
`git clone --no-hardlinks --template=` of `/lane/mirrors/<name>.git` into a temporary folder beside it, your name as
`user.name`/`user.email`, `/out/` and `/inbox/` in its `.git/info/exclude`, then renamed into place; a `/work/code`
that is a link is refused, and anything already under the name is the session's and left alone. `origin` is the
mirror's path: `git pull` and `git fetch` in the room need no credential and reach nothing else. The lane image's
`/etc/gitconfig` (root's) trusts `/lane/mirrors/*`, which belong to the platform's user.

`/new code <name> [task]` starts a session in `/work/code/<name>`, so the repository's `CLAUDE.md` and `AGENTS.md`
reach it as on the Mac ([instructions.md](instructions.md)). A session started elsewhere (the Base's root) is told by
the room's `code.md` to read them before it works in a repository, and to commit on a branch, never `git push`.

## The button

**Who worked where.** A session works in a repository when its folder is in it, or when any of its calls names the
repository's path (`/work/code/<name>` or `code/<name>` in a Bash command, an Edit…): the platform reads its lines as
they come (`push_touched`). When a turn or a run ends, each repository a session worked in is looked at: its branch,
its tip, and how many commits on it no branch of the remote has yet (`push_repos`). The repository has one clone in
the room, so this count is the repository's; every session that worked in it shows it.

**The ping.** The session's latest ping gets «Push · N» (one per repository, «Push retinue · N» when there
are several) beside its replies and «To the Base», in Telegram and in the app; in its menu and as a «Decide» row of the
feed too (`/wait` says «Press «Push» under its result or open it in the app»).

**The card.** «Push · <repo> · <sphere or track> · session <id>», «Branch: <the room's branch> → <the remote
branch> (PR into <default>)», «PR title: …»; then **everything a push carries**: every commit the remote has on no
branch, newest first, merges too, each with its message and its own files (the app shows the whole message), and
«Outgoing: N commits, M objects, K MB. In the PR: …» — the PR's net change with +/− is in the app; what in the work tree is
not committed («ask the session to commit» — the platform never commits for it); and «Send» · «Open diff»
(every leaving commit, oldest first, with its whole message and its own diff — plain text, at most 1 MB) · «Cancel».
A path, a subject or a message is shown with every invisible character as its code (⟦U+202E⟧, a newline in a name as
⟦U+000A⟧) and cut, so no name can fake a line of the card. The remote branch is
`retinue/<session>-<slug>` (the room's branch's name, or its first commit's subject) — or the branch this session's
last push of the same room branch went to, so a second push updates its PR. **A reply to the card** of one word sets
the remote branch (checked: a plain name, not `main`/`master`/the default, not `refs/…`); a phrase sets the PR's
title. In the app both are fields. A card goes stale at the session's next ping, expires after 7 days, «Cancel»
closes it.

**«Send».** Refused at once when the commits change `.github/workflows/`. Otherwise the room's branch is read
again: moved since the card → the card comes back with what it holds now («The branch changed after the card — check it
and press again»). Then `shown → pushing` once (a second press anywhere: «Sending — wait.»), and in the
background `git push --porcelain <url from platform.yaml> <card's sha>:refs/heads/<branch>` — no `+`, no tags,
`--no-verify` — from the platform's own repository. A remote branch already elsewhere is never forced: «The branch
… on the remote is already different — reply to the card with another branch name». Pushed → the pull request (GitHub: opened by the API,
or the open one for that branch found; GitLab: made by the push), the card `pushed` with «PR: <link>» and a message
of its own, «Pushed · <repo> · session <id>» with the link; the mirror fetched so N goes down. A refusal brings the card back `shown` with git's words (never the token).

**A restart.** A card left `pushing` is settled when the platform is back: the remote's branch at the card's sha (one
`ls-remote`) means the push landed — the PR is opened or found again, once; otherwise the card is shown again with
«A platform restart cut the push off — press «Send» again». Nothing is pushed without a press. While the
remote cannot be read, the clock tries again every minute. Runs in the room go on during a push: it never touches the
tree.

## The room's repository is hostile

Everything under `/work/code/<name>` — its `.git` too — is the session's, and the session reads the web. In the
platform git never opens it as a repository (`retinue/code.py`):

- **Read in Python, nothing followed.** The branch is `.git/HEAD`'s `ref: refs/heads/<branch>` (a detached HEAD is
  refused) and its tip the loose ref or the `packed-refs` line, each opened one folder at a time with `O_NOFOLLOW`.
  The work tree's state for the card is hashed in Python against the tip's tree (tracked files; untracked ones under
  the tracked folders, read through the `.gitignore` files and `info/exclude`; a Git LFS pointer is not a change).
  So the room's config (`url.*.insteadOf`, `remote.*.pushurl`, `credential.*`, `core.sshCommand`, `core.fsmonitor`,
  `include.*`, `extensions.partialClone`…), hooks, index, attributes and filters are never read by git. Names,
  subjects, messages and identities are shown with every control, format, combining character and every space but
  U+0020 as its code (⟦U+…⟧).
- **Objects copied, never read in place.** The platform keeps a repository of its own per mirror
  (`/platform/code/<name>.room`: its `HEAD`, `config`, one ref — the tip read above — and the mirror as its alternate)
  and copies into it, through descriptors opened with `O_NOFOLLOW`, the room's loose objects — each inflated in
  bounded steps and hashed against its name as it is read, a mismatch refused — and the room's packs that hold
  anything the mirror lacks (checked by their `.idx`; the clone's own pack is all in the mirror and never copied; a
  pack after the session's `git gc` is copied once, at most 512 MiB) — streamed in 1 MiB steps into a temporary file
  of the platform's, never held in memory; its verdict (`copying`) is written before the copy, so one a crash cut is
  tried once more after the restart and then refused, never in a loop. A refusal the room causes (a malformed index,
  a pack that moved while read, over the cap) is kept for that pack with its reason; a fault of the platform's (the
  disk, git) keeps nothing, and the next look tries again. One copy at a time across all repositories. A count the
  room refuses (a detached HEAD, a hostile layout, such a pack) leaves «⚠ Push: why not» on the ping — its
  press says the card's own line — and «Cannot push — <repo>: <reason>» in the app; no clone or no commit yet
  says nothing. Nothing of the room's `objects/info`, commit
  graph, bitmaps or multi-pack index comes over, and git runs with them off. `git fetch` from that copy into the
  platform's repository for this mirror moves the session's new objects, verified (`fetch.fsckObjects`), under
  `refs/retinue/cards/<id>`; `GIT_NO_LAZY_FETCH`, only the `file` protocol. git never opens a path of the room.
- **Everything that leaves is on the card.** The commits a push carries are `rev-list <tip> --not <the remote's
  branches and tags>` — all parents, not the first only — each read from **its own object** (`cat-file --batch`,
  length-delimited: no character of a message can split or fake one) and shown with its author, committer, whole
  message and own files. The object is read strictly in git's order — one `tree`, the `parent`s, one `author`, one
  `committer`, at most one UTF-8 `encoding`, the blank line, the message — and anything else refuses the push: a
  signature (`gpgsig`), an embedded tag (`mergetag`), a field of its own, a repeat (git's fsck lets a second
  author/committer pair through after the first, which a card reading the last one would never show) — you could not
  check them anyway. A message over 16 KiB or not in UTF-8 is refused too, never shown cut: the app shows each message
  whole; Telegram shows six lines and says «message cut, the whole of it is in «Open diff»», whose text is built
  from the same objects with every invisible character as its code. A new root
  commit (a history the remote never had, an orphan merged with `-s ours`) is refused; at most 100 commits. Then the
  objects the push would carry (`rev-list --objects` the same way) must each be a shown commit, its tree, or a tree or
  blob its own change brings (`diff-tree` against each parent); any other refuses the push («objects that are not in the
  changes of the commits shown»). At most 100 000 objects and 200 MiB. A commit that adds a file and the next that
  deletes it, an empty commit with a long message: all shown, whatever the net diff says. «Send» fetches the
  mirror and counts again: if what would leave against the remote as it is now is not exactly what the card showed
  (its digest), the card comes back («The remote repository changed after the card — the card is updated»).
- **What could bring other objects in is refused** before git sees it: `objects/info/alternates`,
  `objects/info/http-alternates`, `commondir`, a `.git` that is a file or a link, a link or anything but a folder or a
  file among the objects and refs, a branch with no common commit with the default one.
- **Bounded.** 200 000 entries and 4 GiB of objects walked; 256 MiB hashed and 20 s per look of the work tree (past
  either the card says it was not checked whole); ignore files: 200 of them, 2 000 lines each, a pattern with more than
  16 `*` not used — and matched segment by segment, never as one backtracking expression, so a session's `.gitignore`
  cannot stall a look; 50 untracked entries named. Each git call: 300 s, 32 MiB of output read (past it git is killed
  and the call refused), and on Linux 600 MiB of address space (RLIMIT_AS: below the platform's 768 MiB, with one
  look or copy at a time), small pack windows, one thread, `core.bigFileThreshold=1m`. Git LFS pointers are checked in
  one batch. The work tree's walk checks its deadline at every entry (100 000 entries of a folder at most). A ping's
  count runs in the background, one per repository, and a card's look holds only that repository: one slow look never
  holds back another session's ping or the lanes' journals; past 180 s the owner is told. That stops the waiting, not
  the work: the worker thread goes on, holding that repository's lock, until git's own bounds end it (300 s a call,
  the output cap) — another look of the same repository meanwhile also gives up at 180 s.
- **The push's target is the platform's.** The URL is `platform.yaml`'s, the ref `refs/heads/<branch>` checked by the
  platform and by `git check-ref-format`; the sha is the card's, checked to be under its ref.

`tests/test_code.py` shows a room config with `insteadOf`/`pushurl`/`include`/`fsmonitor`/a credential helper/a
promisor `ext::` remote and every hook redirecting nothing and running nothing; alternates, `http-alternates`,
`commondir`, a `.git` file, links at the objects, a pack, `.git`, the folder, `refs` or a ref all refused; a revert,
an empty commit's message and a merged orphan shown or refused; a tampered loose object refused; a `.gitignore` built
to backtrack matched in time; every remote call through a recording proxy; a crafted header (`x-foo`, `gpgsig`,
`mergetag`, a foreign encoding) refused; a message with `\x1e`/`\x1f` read whole; a pack copy a crash cut refused
once, not retried; the helper silent for any other host; a remote that changed after the card sending it back. `tests/test_code_container.py` runs the
same path in a Linux container as on the server — the platform and the room as two users, the mirrors one folder
mounted twice (read-only for the room), the lane's git ≥ 2.46, the room's `git pull` from a mirror it does not own
(skipped without Docker).

**What is given up, plainly.** Every session in the room sees every mirror and every clone: a work repository is as
visible to a notes session as your own. git in the platform still parses object bytes the session wrote (copied and
hash-checked first; a parser bug is the residue). A session running at the moment of your press can change what the
copy reads, but not what leaves unseen: the copy is the platform's, and every object of the push is accounted for by
a commit the card shows. The token can push
to any branch of the repositories it covers, `main` too — the platform never does; branch protection on GitHub makes
GitHub refuse it as well.

## Turning it on

Needs the platform (`PLATFORM=1`). On the server:

```bash
cd /opt/retinue && git pull
sudo TELEGRAM_OWNER_ID=<id> bash deploy/setup.sh            # /srv/retinue/code/{mirrors,tokens.env}, egress/platform-hosts.txt
sudo bash deploy/code/set-git-token.sh you                   # once per token key; typed with echo off
sudoedit /srv/retinue/platform/platform.yaml                 # the `code:` section (example: deploy/platform/platform.example.yaml)
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build \
  platform-egress lane-private platform                      # the proxy first; lane-private for its gitconfig, rules, mount
docker compose -p retinue logs --tail 50 platform | grep -i code   # no «mirror fetch failed»
```

`lane-private` is rebuilt (its `/etc/gitconfig`, `code.md`) and gets the mirrors' mount; recreating it cuts a running
turn, which goes on by itself ([platform.md](platform.md)). Checks:

```bash
docker compose -p retinue exec platform ls /code/mirrors                  # <name>.git for each repository on
docker compose -p retinue exec lane-private git -C /lane/mirrors/retinue.git log -1 --oneline
docker compose -p retinue exec platform-egress tail -5 /var/log/squid/access.log   # TCP_TUNNEL github.com:443
docker compose -p retinue exec lane-private sh -c 'printenv | grep -c GIT_TOKEN'     # 0: no token in the room
```

Then `/new code retinue show git status and git log -3`: no card for either; ask it to make a branch and commit a
small change; the ping has «Push · 1»; the card lists the commit; «Send» answers «PR: https://github.com/…».
A `git push` in that session comes as a courier card that points to the button.

## When something goes wrong

- **«no token RETINUE_GIT_TOKEN_…»** — the key in `platform.yaml` has no line in `tokens.env`: run the script with
  that key, restart the platform.
- **The mirror's fetch fails** (`code <name>: mirror fetch failed: …` in the log; the card «cannot be read») — an expired
  or narrowed token (Authentication failed), a repository renamed (redirects are not followed: put the new URL in
  `platform.yaml`), the proxy down (`up -d platform-egress`).
- **«repository … is not cloned yet»** — the run reached the lane before the mirror existed and the first fetch
  failed; fix the fetch, then write to the session again.
- **A push refused** — the card says git's line: a branch name the remote has elsewhere (reply with another), a
  protected branch rule, the token without Contents write on that repository.
