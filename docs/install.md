# Install (pilot)

Target: one Linux server with Docker and [Dokploy](https://dokploy.com) (or plain `docker compose`). The owner
talks to one assistant in Telegram. Nothing listens on a public port: the bot polls Telegram, the assistant
reaches only the model API, and only through a proxy.

## 1. Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and keep the token. Leave **Threaded Mode**
   off: there is one chat and one stream.
2. Find your Telegram user id (for example, send any message to [@userinfobot](https://t.me/userinfobot)).
   The bot answers this id and nobody else.
3. In your own Telegram account turn on the cloud password (*Settings → Privacy and Security → Two-Step
   Verification*) and look through *Devices*. Whoever takes over the account talks to the assistant as you.

## 2. Prepare the host

A fresh Debian 13 server (x86_64 or arm64, [below](#debian-13-and-arm64)) needs a few packages and Docker Engine with
the compose plugin: Dokploy's installer sets Docker up, or take it from Docker's repository for Debian
([docs.docker.com](https://docs.docker.com/engine/install/debian/): `docker-ce`, `docker-ce-cli`, `containerd.io`,
`docker-buildx-plugin`, `docker-compose-plugin`).

```bash
sudo apt-get install -y git curl ca-certificates python3 openssl iproute2 apparmor
sudo git clone https://github.com/<you>/retinue /opt/retinue
sudo TELEGRAM_OWNER_ID=123456789 bash /opt/retinue/deploy/setup.sh
```

Your time zone is UTC unless you say otherwise: add `OWNER_TZ=Europe/Berlin` (any IANA name) to the command; a
later run without it keeps the zone you gave. Every time the assistant sees, every reminder, the 09:00 morning
summary and the nightly backup follow it. Later you change
it in words («I'm in Springfield»): the router keeps your word over this setting ([proactive.md](proactive.md)).

The script creates `/srv/retinue`: `router.yaml`, the assistant's config and instructions
(`agents/assistant/`, mounted read-only), the proxy config (`egress/`), `secrets.env` with generated tokens, and
`stack.env` (mode 600) — the stack's environment. It prints names, never values: which it wrote and which you fill
in. Run it again after every `git pull`; it keeps what you filled in. If a secret ever leaks, run it with
`ROTATE_BUS_SECRET=1` and redeploy: a new bus secret and new agent tokens.

## 3. Deploy the stack

Fill in `TELEGRAM_BOT_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`: your own subscription,
personal use) in `/srv/retinue/stack.env`. `ELEVENLABS_API_KEY` is optional: voice notes, audio and the sound of
videos are transcribed by [ElevenLabs](https://elevenlabs.io) Scribe; without the key they are refused aloud. Use
a paid plan or pay-as-you-go (the free tier blocks datacenter addresses), turn off the use of your data for
training in the account settings, and create a key with the `speech_to_text` permission only and a credit
limit. Audio goes to ElevenLabs in the US; the router deletes each transcript there right after reading it. Then either `sudo docker compose -p retinue --env-file
/srv/retinue/stack.env -f deploy/compose.yml up -d --build` from `/opt/retinue`, or Dokploy → project →
*Compose* from your Git repo, compose path `./deploy/compose.yml`, with the file's lines in *Environment*. Do not set `ANTHROPIC_API_KEY`: when it is present, Claude Code bills the
key instead of the subscription. Deploy. Once the app is on, each of these keys can also be set or replaced
from its «Settings», which writes the same lines ([face.md](face.md#settings)).

Three containers start: `router` (Telegram, the archive, the bus), `assistant` (the model; internal network
only, no volume, config mounted read-only) and `egress` (Squid: the only way out of the internal network).

Optional parts, each turned on by `setup.sh` with its own switch and its own guide: `TRAVEL=1` — trip search and
price watches; `MEMORY=1` — the knowledge base she writes ([memory.md](memory.md)); `MAIL=1` — your Gmail and
Google Calendar, read-only ([mail.md](mail.md)); `PAGES=1` — your HTML pages, keeping what you do in them, and the ones you
share ([pages.md](pages.md)); `TGBUSINESS=1` and `SEND=1` — your chosen Telegram chats and replies to people, each
sent only by your «Send» under the exact text ([courier.md](courier.md)); `PLATFORM=1` — Claude Code sessions
in lane containers, driven from the same chat, in auto mode: what its classifier blocks approved by you
([platform.md](platform.md)); `FACE=1 FACE_HOST=app.in.<your domain>` — the platform's app on your phone, over WireGuard only ([face.md](face.md)), and at
`/mcp/mac` on the same host your collections for Claude Code on your Mac, by a device's token — the Mac a WireGuard peer
in `DEVICES` ([collections.md](collections.md#from-the-mac)).

## 4. Check

Send the bot `/check`: a message written by the system arrives with two buttons; press one — the buttons
disappear and the choice stays. Then ask anything, and later ask what was said before: the assistant searches
the archive. Ask it to remind you of something in five minutes: it names the day and time back, and the reminder
arrives on time, written by the assistant. The morning summary comes every day at 09:00 your time.

Before sending your own files, run the multimodal probe in the assistant's container: a picture and a PDF it
makes itself go through the assistant's engine, then a question with `resume`, `/compact` and a question after
it. Every step must have `"ok": true`; `broken_photo` must answer without an error.

```bash
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml exec -T assistant python - < tests/e2e/multimodal.py
```

Then send the bot a photo, a PDF, a voice note: she answers on substance. Files over 20 MB are refused aloud —
the Bot API serves bots nothing bigger.

The assistant's container has no way out except the model API:

```bash
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml exec assistant python -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)"
# OSError: Network is unreachable
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml exec egress cat /var/log/squid/access.log | grep -c TCP_DENIED
# refused requests, if any: see "Hosts the proxy lets through"
```

## 5. Backup

Add `BACKUP=1` to the `setup.sh` command and follow [backup.md](backup.md): a nightly restic backup to S3 storage
at another provider, with a server key that cannot delete, a monthly check and test restore from your own
computer, and a line about it in the morning summary.

The containers' health needs nothing: `setup.sh` installs a host timer that looks at them every two minutes, and
the router tells you when one goes bad and when it is well again ([health.md](health.md)).

## 6. Travel (optional)

Trip search and price watches come from travel-ops, a separate MCP server, built by compose from your
travel-ops checkout at a pinned commit into an image of about 5 GB: Google Chrome and Camoufox are inside. Add
`TRAVEL=1` to the `setup.sh` command once; from then on it stays on. The source is required with travel on and is
yours, not this repository's: `TRAVEL_OPS_GIT` in `stack.env`, a git URL with the commit after `#` (the script asks
for it until it is filled).

```bash
sudo TELEGRAM_OWNER_ID=123456789 TRAVEL=1 bash /opt/retinue/deploy/setup.sh
echo 'TRAVEL_OPS_GIT=https://github.com/<you>/travel-ops.git#<40-hex commit>' | sudo tee -a /srv/retinue/stack.env
sudo nano /srv/retinue/travel/profile.yml   # home_airports, travellers, currency: profile.example.yml in travel-ops
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build
```

Two containers run: `travel-ops` (the MCP server, on internal networks of two members each: with the assistant,
with the router, with the platform) and `travel-watch` (the price watches, every 30 minutes). Both reach the travel
sites through their own network; the assistant and the platform do not. With the platform on, its sessions have
travel-ops' tools too ([platform](platform.md#travel-prices-in-a-session)). She calls travel-ops' tools herself and reads its whole answer; the arguments
of each call are checked by the engine before they leave, and every check is a line in the router's protocol.
Links to the sites in `deploy/travel/link-hosts.txt` are clickable; every other address stays monospace. Sites
may limit automated requests: use it for your own trips, as travel-ops' README says.

Both browsers in the travel-ops image are pinned (its `Dockerfile`: `CHROME_VERSION`, `CHROME_SHA256`,
`CAMOUFOX_VERSION`). Once a month: move the pins in travel-ops (its README says where the current values are),
commit and push it, put the new SHA in `deploy/compose.yml` (`tests/test_deploy.py` refuses the old base), and
rebuild without the cache: `docker compose … build --no-cache travel-ops && docker compose … up -d`. Google keeps
only recent Chrome packages, so a build with an old pin fails — that failure is the reminder.

## Updating

A tag `vX.Y.Z` pushed to the repository publishes Retinue's own images to the owner's GitHub registry as
`ghcr.io/<owner>/retinue*:vX.Y.Z` (`.github/workflows/release.yml`), each for linux/amd64 and linux/arm64 under the
one tag: the server pulls its own. A server on releases has
`RETINUE_IMAGE=ghcr.io/<owner>/retinue` and `RETINUE_VERSION=vX.Y.Z` in `stack.env`; compose runs those images and
gives the version to the router and the platform. Without the two lines compose builds from the checkout as
`retinue*:local`, as before; on a server on releases, leave `--build` out.

For a private repository, log the server in once with a token that has `read:packages` only
(`sudo docker login ghcr.io`). Then, from the checkout:

```bash
sudo RETINUE_IMAGE=ghcr.io/<owner>/retinue bash deploy/update.sh v1.2.0   # RETINUE_IMAGE the first time only
```

It checks the stack is healthy, pulls the tag's images (a tag not published stops here, nothing changed), builds the
lane's image from the checkout — it carries Claude Code, whose licence lets no one but Anthropic hand it out, so no
registry has it; a failed build stops here too — stops the stack, snapshots its volumes (but the lanes' working trees), the code services' state folders and `stack.env` into
`/var/backups/retinue/updates/`, writes the version to `stack.env`, starts the stack — each service migrates its
database when it opens it — and waits up to five minutes for every service to be healthy (`deploy/health`). When one
is not, it puts the snapshot back and starts the version before. The compose file is the checkout's: when a release
changes `deploy/compose.yml` or `setup.sh`, run `setup.sh` first; check out the tag before every update, since the
lane's image is built from the checkout. Under Dokploy, set the two
lines in *Environment* and redeploy: no snapshot, no way back.

## Following develop

A server can follow `develop` instead of releases: each push there that CI passes is on it minutes later, with no one
on the server. CI builds the commit's images once, as a release's (`release.yml`, both architectures), and pushes
them as `ghcr.io/<owner>/retinue*:sha-<12 hex>`; its last job, `deploy` in `.github/workflows/ci.yml`, logs in over
ssh with a key that can do one thing, run `deploy/deploy.sh` with the commit's sha. Only the lane's image is built on
the server, as in «Updating»: it checks the commit out (its compose file names its images) and switches to them with `update.sh` — the steps of
«Updating», snapshot and way back included. Not healthy: the data, `stack.env` and the checkout go back to the commit
before. The last ten snapshots are kept. The registry's packages are public, as the repository is; for a private one,
log the server in as in «Updating».

To turn it on, once:

```bash
ssh-keygen -t ed25519 -N '' -C retinue-deploy -f deploy-key          # on your computer
# on the server, a line of /root/.ssh/authorized_keys:
command="/usr/bin/bash /opt/retinue/deploy/deploy.sh",restrict ssh-ed25519 AAAA… retinue-deploy
# in the repository: Settings → Environments → `server` (deployment branches: develop), its secrets
#   DEPLOY_KEY          the private key, deploy-key
#   DEPLOY_HOST         the server's address
#   DEPLOY_KNOWN_HOSTS  ssh-keyscan -t ed25519 <address>
```

Without the environment's secrets the job says so and stops; a fork never has them. A deploy by hand is the same
command: `sudo bash deploy/deploy.sh <sha>`.

## Hosts the proxy lets through

`/srv/retinue/egress/allowed-hosts.txt` starts with one line, `api.anthropic.com`. If a run fails and the proxy
log shows `TCP_DENIED` for another host, decide whether that host is needed (the list of hosts Claude Code uses
is in its [network configuration docs](https://code.claude.com/docs/en/network-config)), add it to the file and
restart `egress`. `setup.sh` never overwrites this file.

## Versions

`pyproject.toml` pins `claude-agent-sdk` to an exact version; the SDK wheel carries its own `claude` CLI, and
the image installs everything from `uv.lock`. To update: change the pin, run `uv lock`, update `CLI_VERSION` in
`tests/test_pins.py`, rebuild, and repeat the trial run below before trusting the new image.

```bash
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml exec -T assistant python - < tests/e2e/coldstart.py
```

Every line must have `"is_error": false`; the first line must show `"api_key_in_env": false`.

Every image `deploy/compose.yml` pulls from a registry is pinned by digest — Squid once, as `x-squid-image` in `deploy/compose.yml`, Tuwunel,
the public pages' nginx, and dnsmasq in `deploy/split-dns/compose.yml` — and `tests/test_deploy.py` refuses a bare tag.
Retinue's own images are the exception: a release's are named by its tag, set by `deploy/update.sh` («Updating»
above), and without one they are built from the checkout.
To move a pin, pull the tag on the server, take its digest and put it in:

```bash
sudo docker pull ubuntu/squid:<tag> && sudo docker image inspect --format '{{index .RepoDigests 0}}' ubuntu/squid:<tag>
```

Every service has a `mem_limit`, sized from `docker stats --no-stream` and the cgroups' `memory.peak` on the server
with room on top. What runs all the time is capped at under 8 GiB in total; the room and travel-ops, which work when
you use them, have their own ceilings. A container that reaches its ceiling is killed by the kernel and restarted by Docker, and the owner hears of it (`docs/health.md`).

## Debian 13 and arm64

The install works on Debian 13 (trixie) on x86_64 and on arm64. What was checked:

- **Rehearsed in CI** (`.github/workflows/install.yml`, `tests/e2e/install/host.sh`): on a fresh host the packages
  above and Docker from Docker's repository, `setup.sh` with every switch but travel (Matrix, memory, mail, send,
  Telegram Business, pages, platform, face, backup) on test values, every image built, the stack started, and every
  service healthy by `deploy/health`'s own unit; then the assistant has no route out, bubblewrap runs in the lane
  under its AppArmor profile, and the lane's pinned CLI starts. On x86_64 the host is Debian 13's own cloud image in
  a VM (its kernel, systemd and AppArmor); on arm64 it is GitHub's arm64 runner, Ubuntu 24.04, with no VM.
- **Not rehearsed**: anything that needs a real secret — Telegram (a made-up bot token is refused at start, so the
  router runs on its Matrix channel there), a model call, Google's mail, ElevenLabs, the backup's S3 — and Dokploy
  itself, WireGuard and Traefik's certificates. The container probe ([platform.md](platform.md)) is yours to run on
  the server.
- **arm64 pieces**: every image pinned by digest (Squid, Tuwunel, nginx-unprivileged, dnsmasq, the face's Node) is a
  multi-platform index with linux/arm64; the Dockerfiles' bases (`python:3.12-slim`, `python:3.12-slim-trixie`,
  `node:24.22.1-trixie-slim`, `ghcr.io/astral-sh/uv:0.6.12`) too. `uv.lock` has a manylinux aarch64 or pure wheel for
  every package Linux installs, `claude-agent-sdk` and its bundled CLI included; `face/pnpm-lock.yaml` carries the
  linux arm64 (glibc and musl) builds of rolldown, lightningcss, Tailwind's oxide, Biome and TypeScript's native
  compiler; `deploy/lanes/cli.lock` has the CLI's linux-arm64 tarball; `setup.sh` pins restic's linux_arm64 file
  by its own sha256. A release publishes each of Retinue's images for both architectures under one tag.
- **Travel does not run on arm64**: travel-ops' Dockerfile installs Google Chrome's amd64 package, and Google ships
  no Chrome for Linux on arm64. Take an x86 server if you want travel.

When you move a digest pin, keep the multi-platform index's digest (`RepoDigests` gives it), not one platform's.

## Matrix (optional)

The Matrix adapter is still in the code and is off by default. To run it you also need a WireGuard VPN on
`10.8.0.0/24` with the server at `10.8.0.1`, a public `A` record for the homeserver name (for Let's Encrypt)
and TCP 80/443 open.

```bash
sudo TELEGRAM_OWNER_ID=123456789 MATRIX_SERVER_NAME=matrix.example.com MATRIX_OWNER=alice \
  bash /opt/retinue/deploy/setup.sh
```

The script then also writes the appservice registration, installs the Traefik middleware `vpn-only` and starts
split DNS (dnsmasq on `10.8.0.1`), and adds the Matrix variables to `stack.env`: `COMPOSE_PROFILES=matrix`
starts Tuwunel. Create your account with `sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml exec router retinue-admin register --username alice`,
then set `MATRIX_ALLOW_REGISTRATION=false` in `stack.env` and redeploy. Sign in with Element X.

## Tests

```bash
uv run --frozen --with pytest pytest -q
uv run --with ruff ruff check .        # the rules are in pyproject.toml: pyflakes and pycodestyle's errors
```

Smoke test with a real homeserver and an echo agent, no model:

```bash
docker build -t retinue:local .
python3 tests/e2e/smoke.py      # brings the stack up itself: the homeserver, the owner, then the router
docker compose -f tests/e2e/compose.yml down -v
```
