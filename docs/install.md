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
(`agents/assistant/`, mounted read-only), the proxy config (`egress/`), and `secrets.env` with generated
tokens. It prints the environment block for the next step. Run it again after every `git pull`.

## 3. Deploy the stack

Dokploy → project → *Compose* from your Git repo, compose path `./deploy/compose.yml`. Paste the printed
environment block and fill in `TELEGRAM_BOT_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`:
your own subscription, personal use). Do not set `ANTHROPIC_API_KEY`: when it is present, Claude Code bills the
key instead of the subscription. Deploy.

Three containers start: `router` (Telegram, the archive, the bus), `assistant` (the model; internal network
only, no volume, config mounted read-only) and `egress` (Squid: the only way out of the internal network).

## 4. Check

Send the bot `/check`: a message written by the system arrives with two buttons; press one — the buttons
disappear and the choice stays. Then ask anything, and later ask what was said before: the assistant searches
the archive. Ask it to remind you of something in five minutes: it names the day and time back, and the reminder
arrives on time, written by the assistant. The morning summary comes every day at 09:00 your time.

The assistant's container has no way out except the model API:

```bash
docker compose exec assistant python -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)"
# OSError: Network is unreachable
docker compose exec egress cat /var/log/squid/access.log | grep -c TCP_DENIED
# refused requests, if any: see "Hosts the proxy lets through"
```

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
split DNS (dnsmasq on `10.8.0.1`). Paste the longer environment block it prints: `COMPOSE_PROFILES=matrix`
starts Tuwunel. Create your account with `docker compose exec router retinue-admin register --username alice`,
then set `MATRIX_ALLOW_REGISTRATION=false` and redeploy. Sign in with Element X.

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
