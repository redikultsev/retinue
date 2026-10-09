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

```bash
git clone https://github.com/<you>/retinue /opt/retinue
sudo TELEGRAM_OWNER_ID=123456789 bash /opt/retinue/deploy/setup.sh
```

Your time zone is `Europe/Moscow` unless you say otherwise: add `OWNER_TZ=Europe/Belgrade` (any IANA name) to
the command. Every time the assistant sees, every reminder and the 09:00 morning summary follow it.

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
key instead of the subscription. Deploy.

Three containers start: `router` (Telegram, the archive, the bus), `assistant` (the model; internal network
only, no volume, config mounted read-only) and `egress` (Squid: the only way out of the internal network).

Optional parts, each turned on by `setup.sh` with its own switch and its own guide: `TRAVEL=1` — trip search and
price watches; `MEMORY=1` — the knowledge base she writes ([memory.md](memory.md)); `MAIL=1` — your Gmail and
Google Calendar, read-only ([mail.md](mail.md)); `LIFEHUB=1` — pages for your devices only: now, trips, status
([lifehub.md](lifehub.md)).

## 4. Check

Send the bot `/check`: a message written by the system arrives with two buttons; press one — the buttons
disappear and the choice stays. Then ask anything, and later ask what was said before: the assistant searches
the archive. Ask it to remind you of something in five minutes: it names the day and time back, and the reminder
arrives on time, written by the assistant. The morning summary comes every day at 09:00 your time.

Before sending your own files, run the multimodal probe in the assistant's container: a picture and a PDF it
makes itself go through the assistant's engine, then a question with `resume`, `/compact` and a question after
it. Every step must have `"ok": true`; `broken_photo` must answer without an error.

```bash
docker compose exec -T assistant python - < tests/e2e/multimodal.py
```

Then send the bot a photo, a PDF, a voice note: she answers on substance. Files over 20 MB are refused aloud —
the Bot API serves bots nothing bigger.

The assistant's container has no way out except the model API:

```bash
docker compose exec assistant python -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)"
# OSError: Network is unreachable
docker compose exec egress cat /var/log/squid/access.log | grep -c TCP_DENIED
# refused requests, if any: see "Hosts the proxy lets through"
```

## 5. Backup

Add `BACKUP=1` to the `setup.sh` command and follow [backup.md](backup.md): a nightly restic backup to S3 storage
at another provider, with a server key that cannot delete, a monthly check and test restore from your own
computer, and a line about it in the morning summary.

## 6. Travel (optional)

Trip search and price watches come from [travel-ops](https://github.com/redikultsev/travel-ops), built by
compose from a pinned commit (`deploy/compose.yml`) into an image of about 5 GB: Google Chrome and Camoufox are
inside. Add `TRAVEL=1` to the `setup.sh` command once; from then on it stays on.

```bash
sudo TELEGRAM_OWNER_ID=123456789 TRAVEL=1 bash /opt/retinue/deploy/setup.sh
sudo nano /srv/retinue/travel/profile.yml   # home_airports, travellers, currency: profile.example.yml in travel-ops
docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build
```

Two containers run: `travel-ops` (the MCP server, on the internal network `travel` with the assistant and the
router) and `travel-watch` (the price watches, every 30 minutes). Both reach the travel sites through their own
network; the assistant does not. She calls travel-ops' tools herself and reads its whole answer; the arguments
of each call are checked by the engine before they leave, and every check is a line in the router's protocol.
Links to the sites in `deploy/travel/link-hosts.txt` are clickable; every other address stays monospace. Sites
may limit automated requests: use it for your own trips, as travel-ops' README says.

Both browsers in the travel-ops image are pinned (its `Dockerfile`: `CHROME_VERSION`, `CHROME_SHA256`,
`CAMOUFOX_VERSION`). Once a month: move the pins in travel-ops (its README says where the current values are),
commit and push it, put the new SHA in `deploy/compose.yml` (`tests/test_deploy.py` refuses the old base), and
rebuild without the cache: `docker compose … build --no-cache travel-ops && docker compose … up -d`. Google keeps
only recent Chrome packages, so a build with an old pin fails — that failure is the reminder.

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
docker compose exec -T assistant python - < tests/e2e/coldstart.py
```

Every line must have `"is_error": false`; the first line must show `"api_key_in_env": false`.

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
starts Tuwunel. Create your account with `docker compose exec router retinue-admin register --username alice`,
then set `MATRIX_ALLOW_REGISTRATION=false` in `stack.env` and redeploy. Sign in with Element X.

## Tests

```bash
uv run --frozen --with pytest pytest -q
```

Smoke test with a real homeserver and an echo agent, no model:

```bash
docker build -t retinue:local .
docker compose -f tests/e2e/compose.yml up -d && python3 tests/e2e/smoke.py
docker compose -f tests/e2e/compose.yml down -v
```
