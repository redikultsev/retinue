# Life hub

A small site on your own server for your own devices: **now** (upcoming trips, today's and tomorrow's meetings,
your reminders, mail that waits for you, document deadlines from the knowledge base, the health line), **trips**
(a page per trip the assistant planned: every option, prices with the moment they were seen, ratings, photos) and
**status** (what was processed, what is connected, the share of the subscription's five-hour and weekly windows,
tokens by kind of run, the mail kept and dropped, your mail rules). In code and paths it is `lifehub`: «hub»
already names the knowledge base's git hub.

```
router ── data/*.json, charts/*.svg, photos/*.jpg ──▶ lifehub-build (Hugo, no network) ── releases/<ts>, current
assistant ── publish_trip (bus) ──▶ router ── refine_* / stay_photos ──▶ travel-ops               │
                                                                                                  ▼
your phone / Mac ── WireGuard ── Traefik (your devices only, adds the key) ──▶ lifehub (nginx, read-only)
```

What it never does: take HTML from the model (she sends data by a schema, Hugo escapes every value), load anything
from elsewhere (the page policy is `default-src 'self'`, no inline script or style), make a link of an address that
did not come from travel-ops, answer a device that is not on your list (404, as if there were no site).

## Turn it on

### 1. DNS: a public record for the certificate (once)

Let's Encrypt checks the name over plain HTTP, so the name needs a public A record pointing at the server — like
your Matrix name. At your DNS provider: `hub.in.<your domain>` → the server's public address. Wait until it
resolves from outside:

```bash
dig +short @1.1.1.1 hub.in.example.com          # the server's public address
```

Nothing else is public: Traefik answers anyone who is not one of your devices with 404. The name itself is
public — every certificate goes to the CT logs.

### 2. Your devices reach it through the VPN

Traefik lets through the WireGuard addresses of your devices (wg-easy shows them, e.g. `10.8.0.2` for the Mac,
`10.8.0.3` for the phone). A device must therefore go to the server **through the tunnel**: its DNS must be the
VPN's `10.8.0.1` (split DNS answers `10.8.0.1` for every `*.in` name), otherwise it resolves the public record,
goes over the internet and gets 404.

```bash
dig +short hub.in.example.com                   # on the device, VPN on: 10.8.0.1
```

On the iPhone: WireGuard app → the tunnel → **Edit** → **DNS servers**: `10.8.0.1` → Save. (wg-easy can put it into
new profiles: its client settings, DNS.)

### 3. The server

```bash
cd /opt/retinue && sudo git pull
sudo TELEGRAM_OWNER_ID=<id> LIFEHUB=1 LIFEHUB_HOST=hub.in.example.com LIFEHUB_DEVICES=10.8.0.2/32,10.8.0.3/32 \
  bash deploy/setup.sh
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build
```

`setup.sh` keeps the address and the devices in `/srv/retinue/lifehub/` (later runs need no switch), makes a key
in `secrets.env`, renders `/srv/retinue/lifehub/nginx.conf` and `/etc/dokploy/traefik/dynamic/lifehub.yml`
(both carry the key; root's), and adds `lifehub_url` to `router.yaml`. A new device: edit
`/srv/retinue/lifehub/devices.txt` (one `a.b.c.d/32` per line) and run `setup.sh` again.

### 4. Check

```bash
curl -sI https://hub.in.example.com/ | head -1                         # from a device, VPN on: 200
curl -s -o /dev/null -w '%{http_code}\n' https://hub.in.example.com/   # VPN off, or another device: 404
sudo cat /srv/retinue/lifehub/site/build.json                         # "ok": true, the live release
```

The morning summary's health line says «хаб собран N мин назад» or why it is not up to date.

## How it works

- **Data.** The router writes `now.json` and `status.json` every five minutes and a minute after a model run, and
  `trips/<id>.json` when the assistant publishes a trip; each file is replaced whole. Numbers are counted by code
  from the router's tables; the share of the subscription's windows is the CLI's own report (the account's, Mac
  included); dollars are the CLI's estimate, not a bill.
- **Trips.** The assistant calls `publish_trip` with her words and, for each option, travel-ops' `search_id` and
  `link`. The router looks the option up in travel-ops' stored search (no site is asked) and takes the price, the
  seller, the times, the rating and when the price was seen from there; stay photos come from travel-ops
  (`stay_photos`) and are re-encoded as JPEG by the router's file worker. A page's address is random.
- **Build.** `lifehub-build` (no network, the containers' user) builds a new release with Hugo whenever the data
  changed, checks every page and picture (no script, no style, no event handler, nothing from elsewhere), and only
  then switches `current`. A failed build leaves the last good one live and says why in `build.json`.
- **Serving.** nginx (unprivileged, read-only) serves `current` with the same headers as Traefik and refuses any
  request without the key Traefik adds — a container on Traefik's network that is not Traefik gets 404.
