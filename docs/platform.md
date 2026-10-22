# The platform: Claude Code sessions in lanes

Besides the assistant, Retinue can run Claude Code sessions on your server — an unmodified `claude -p` of a pinned
version, each in a **lane** container of its own — driven from the same Telegram chat. Every tool call your rules do
not decide comes to you as a card; what would leave the system (a push, a pull request, a release, a publish, a
connection to another machine, a letter) is never granted at all: it comes as a courier card, and you run it
yourself. The assistant is untouched: plain text in the chat still goes to her, and her archive never sees a
session.

```
Telegram ⇄ router (the only bot poller; holds the token)
             │  replies to a session's messages, /new <track>, the cards' buttons
             ▼  platform-router (internal)
           platform (uid 10007): its own SQLite — sessions, every line of every run, approvals, cards
             ├── lane-private (internal) ── lane-private (uid 10005) ── lane-private-egress → api.anthropic.com
             └── lane-code (internal) ───── lane-code (uid 10006) ───── lane-code-egress → GitHub, registries
```

The platform holds no bot token and has no route out. A lane reaches the platform and its own proxy, nothing else.

## Turning it on

```bash
sudo TELEGRAM_OWNER_ID=123456789 PLATFORM=1 bash /opt/retinue/deploy/setup.sh
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build
```

From then on it stays on (as long as `/srv/retinue/platform` exists). `setup.sh` adds the compose profile
`platform`, generates `RETINUE_PLATFORM_TOKEN` (the router to the platform), `RETINUE_LANE_TOKEN_PRIVATE` and
`RETINUE_LANE_TOKEN_CODE` (the platform to each lane) and `RETINUE_HOOK_SECRET`, puts `platform_url` in
`router.yaml`, writes `/srv/retinue/platform/platform.yaml` from `deploy/platform/platform.example.yaml` once, and
copies each lane's list of hosts to `/srv/retinue/egress/lane-<lane>-hosts.txt` once — both are yours to edit
afterwards. With `BACKUP=1`, the platform's volume `platform-data` is backed up; the lanes' volumes are not (the
transcripts are in the platform's database; the work trees are your checkouts). The lanes use the stack's
`CLAUDE_CODE_OAUTH_TOKEN`.

Run the container probe below before the first session.

## Tracks and sessions

A **track** is a name in `platform.yaml`: its lane, its kind, its directory under the lane's `/work` (it must exist
there), its always rules. The example has `notes` (private, on `lane-private`) and `code` (on `lane-code`). To work
on a repository, clone it into the lane's work volume (`docker compose exec lane-code git clone … /work/<name>`),
add a track with `cwd: <name>`, and `docker compose restart platform`.

- `/new <track> [text]` starts a session on that track; the answer names the session. Bare `/new` still starts the
  assistant's conversation over, as before.
- **Reply** to any of a session's messages to write to it. A reply while a card is open makes that card stale («Устарела:
  ты написал новое сообщение»): the call it asked about is not run. Only text goes into a session for now.
- A session's **summary** is the final text of a turn («Итог · <track> · сессия <id>»). A code session closes on it;
  a reply opens it again. A private session goes idle. A session idle for 7 days is closed.
- At most 2 sessions run on a lane and 5 in all; the rest wait their turn.
- If the platform is down, the router says «Платформа недоступна — попробуй позже.» and never hands the message to
  the assistant.

## What a lane sees

- Its own `/work` volume, its CLI's home and transcripts (`/lane/config`), its journal (`/lane/state`). Nothing of
  the router's, the assistant's or the other lane's.
- The CLI pinned in `deploy/lanes/cli.lock` (installed in the image with its npm integrity checked). The lane host
  refuses every run if `claude --version` says anything else.
- Its environment is built from scratch: the subscription token, the proxy, the hook token of the run. Bash runs in
  the CLI's bubblewrap sandbox with the environment scrubbed (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`): a command
  sees neither the token nor the CLI's own process. If the sandbox cannot start (bubblewrap or socat missing,
  bubblewrap blocked), the CLI refuses to start — no session runs, and none runs unsandboxed or unscrubbed.
- The managed settings in the image (`deploy/lanes/managed-settings.json`, the same for both lanes, root's,
  read-only): `WebSearch` and `WebFetch` denied, bypass mode disabled, hooks only to
  `http://platform:9500/hook/*`, Bash sandboxed (with `failIfUnavailable`: never unsandboxed) with `/lane/config`, `/lane/run` and `/etc/claude-code` not writable.
  Settings files a session writes into its directory or home are never read (`--setting-sources ""`).
- The network: `lane-private` reaches `api.anthropic.com` only; `lane-code` also GitHub, PyPI and npm
  (`deploy/egress/lane-code-hosts.txt`), to clone and install. No lane holds a GitHub, registry or mail credential.

## Always rules

Rules in Claude Code's own syntax (`Bash(make test:*)`, `Edit`, `Read(src/**)`), matched by the CLI itself:

- in code: `BASE_ALLOW` (`Read`, `Grep`, `Glob`, `TodoWrite`) and `BASE_DENY` (`WebSearch`, `WebFetch`,
  `CronCreate`, `CronDelete`, `ScheduleWakeup`, `RemoteTrigger`) in `retinue/approvals.py`;
- per lane: `deny` in `platform.yaml`;
- per track: `allow` and `deny` in `platform.yaml`.

There is no button that writes a rule: always rules are edited in `platform.yaml`, never derived from a pressed
card. A call the CLI still asks about despite a rule (a compound command, a path outside the directory) is refused
with a request to repeat it; repeated, it comes to you as a card.

## Approvals

A call no rule decides stops the run and comes as a card: «Разрешение · <track> · сессия <id>», the tool, the
directory, the input verbatim (a Bash command as it is), and what code found in it. Buttons:

- **«Разрешить»** — the session resumes and the call runs exactly once: the grant is bound to a digest of the tool,
  its input, the directory and the file it would change, as the file is now. A file changed since the card, a
  second press, a press after a restart that already used it — nothing runs again.
- **«Отказать»** — the session resumes and the model is told you refused.
- **«Открыть»** — the whole input as a file, with the same buttons. A card longer than one Telegram message offers
  only «Открыть».

A card survives restarts of the platform, the lanes and the router: press it an hour later and the call runs once.
Unpressed for 7 days, it expires. When the model makes several calls at once and the first is deferred, the CLI
keeps only the last one; the others are dropped and the model has to ask again.

## The never list and the courier card

`git push`, `gh pr create`, `gh pr merge`, `gh release create`, `npm publish`, `uv publish`, `twine upload`,
`cargo publish`, `docker push`, `kubectl apply`, `ssh`, `scp`, `rsync`, `sendmail`, `mail` (`NEVER` in
`retinue/approvals.py`). Such a call is refused in the session and comes to you as a courier card: «Курьер · <track> ·
сессия <id>», what the session wants, the directory, the exact command, and «Не выполняю: это делаешь ты сам —
команда выше целиком.» There is no button: run it yourself, where you hold the credential. Each entry is also a
deny rule of every run, so not even a mistaken grant could run it.

## Probes

The CLI behaviour the platform rests on — defer, resume, marker hooks, settings that do not load — is pinned by
probes that run the pinned binary against a local fake Messages API (no credential, no cost):

```bash
bash deploy/lanes/fetch-cli.sh ~/.cache/retinue-cli            # the pinned CLI, integrity checked
RETINUE_LANE_CLI=~/.cache/retinue-cli/claude uv run --locked --with pytest pytest -q -m probe tests
```

Without `RETINUE_LANE_CLI` they are skipped. The login probe also needs `CLAUDE_CODE_OAUTH_TOKEN`. Run them before
bumping the version in `deploy/lanes/cli.lock` (together with `cliengine.CLI_VERSION` and `tests/test_pins.py`).

The **container probe** builds the lane image and runs it under Docker's default seccomp profile, with the real
login: the lane host reports the pinned CLI and `loggedIn`; a Bash run sees no `CLAUDE_CODE_OAUTH_TOKEN` in its
environment or any `/proc/*/environ`; a write into `/lane/config` fails; `WebFetch` is denied. It needs Docker and
`CLAUDE_CODE_OAUTH_TOKEN`, and spends a few model calls:

```bash
CLAUDE_CODE_OAUTH_TOKEN=… uv run --locked --with pytest pytest -q -m probe tests/test_lane_container.py
```

If bubblewrap cannot run under the default profile, sessions do not start. The remedy is yours to choose: a
per-lane seccomp profile that allows creating user namespaces, or the host's
`kernel.apparmor_restrict_unprivileged_userns` setting.

## Operator checks

1. The container probe above, on the server.
2. A code session on a real repository asks for a call no rule decides; `docker compose restart`; an hour later
   press «Разрешить»: the call ran once (the approval's row in the platform's `approvals` table is `executed`; one
   execution in the transcript).
3. A code session asked to push gets a courier card, no approval card, and nothing is pushed.
4. `docker compose stop platform` in the middle of a turn: the session's tool calls fail as blocked; start it again:
   the transcript is complete.
