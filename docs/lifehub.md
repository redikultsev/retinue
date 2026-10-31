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

On a phone the pages are tabs at the bottom — **Now · Trips · Tracker · Library · More** («More» holds the
review, the status, finance and health); from 900 px they are a sidebar. Dark unless your system asks for light;
the glasses button in the header switches to a pure-black theme for additive displays (borders only, type
+20 %), remembered on that device. The header says how fresh the data is and turns yellow past 15 minutes.

What it never does: take HTML from the model (she sends data by a schema, Hugo escapes every value), load anything
from elsewhere (the page policy is `default-src 'self'`, no inline script or style), make a link of an address that
did not come from travel-ops, answer a device that is not on your list (404, as if there were no site).
An address is never page text: a link is a button with its host — to a travel-ops site, a meeting service
(«join · meet.example.com»: its password stays in the button's target) or an address in your own tracker; an address
from a letter is only its host on a chip. An address is kept only if Go and Python read it alike (no `\`, no bad
`%`, no `user@` before the host), and one Hugo cannot parse is skipped, not a failed build. Nobody's e-mail is on
«Now» or the tracker page. Your own HTML pages — courses, notes made in claude.ai — are served under `/pages/`, each a
sandboxed document: see [Pages](#pages).

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

The morning brief's health line says «hub built N min ago» or why it is not up to date.

## How it works

- **Data.** The router writes `now.json` and `status.json` every five minutes and a minute after a model run, and
  `trips/<id>.json` when the assistant publishes a trip; each file is replaced whole. Numbers are counted by code
  from the router's tables; the share of the subscription's windows is the CLI's own report (the account's, Mac
  included); dollars are the CLI's estimate, not a bill.
- **Trips.** The assistant calls `publish_trip` with her words and, for each option, travel-ops' `search_id` and
  `link`. The router looks the option up in travel-ops' stored search (no site is asked) and takes the price, the
  seller, the times, the rating and when the price was seen from there; stay photos come from travel-ops
  (`stay_photos`) and are re-encoded as JPEG by the router's file worker. A page's address is random.
- **«Now».** The next meeting with a countdown; who waits for your answer, by person — three, the rest folded —
  with how long; today, the evening (from 18:00) and tomorrow as a timeline in your local time; deadlines overdue
  first, documents (`until`) apart from facts to re-check (`stale_after`); upcoming trips; a row of chips — broken
  or not, the backup, the limit, the build — that leads to «Status». Your own recurring blocks with nobody else
  and nowhere to be («Block», four hours or longer) are one line «In the background», not meetings.
- **The tracker.** The table of your tracker in the base (docs/proactive.md, «The tracker»; its title is the tab's
  name): a funnel by state, the rows in groups, the tracker's meetings in your calendars two weeks ahead, and the
  letters of your private matter (docs/mail.md) you have not answered. Read only: a row changes in the base.
- **Your time zone.** Every time on every page is local in the zone in force — `owner_tz` at first, then whatever
  you set in words (docs/proactive.md). The hub reads it in one place, `Lifehub.tz`.
- **Charts and dollars.** A chart is geometry the router lays out (`retinue/charts.py`) and the page draws with
  its own classes, so it follows the theme. The CLI's dollar estimate is per session and grows as a session is
  resumed: a run counts what it added to its session (`protocol.tokens`), not the session's running total.
- **Library.** Cards from `pages.json` in the data folder, if there is one: a list (or `{"pages": [...]}`) of
  `{id, title, description, topic, words, reading_min, updated, url}`; `url` is a path on the hub (`/…`).
- **Script.** One file, `static/js/hub.js`: relative times («in 42 min», «updated 3 min ago») and the
  glasses button. Every element already has its text from Hugo, so with the script blocked the page says the same
  in clock time. The gate lets through only `<script src="/js/<name>.js" defer></script>` of a file the image
  ships, and `js/<name>.js` only byte for byte; nothing inline, nothing from elsewhere. Icons: Lucide, inline
  (deploy/lifehub/NOTICE).
- **Build.** `lifehub-build` (no network, the containers' user) builds a new release with Hugo whenever the data
  changed, checks every page and picture (no script but the site's own, no style, no event handler, nothing from
  elsewhere), and only
  then switches `current`. A failed build leaves the last good one live and says why in `build.json`.
- **Serving.** nginx (unprivileged, read-only) serves `current/hub` with the same headers as Traefik and refuses any
  request without the key Traefik adds — a container on Traefik's network that is not Traefik gets 404.

## Pages

Your own HTML pages from the base — a regex course, a cheat sheet, anything a claude.ai artifact made — open on
your devices at `https://hub.in.<your domain>/pages/<id>.html`, with their scripts, highlighting and diagrams, and
the hub lists them («Library», from `pages.json`).

**Publishing.** A page is any `*.html` in a nature folder of the base (`<Space>/knowledge|profile|journal|artifacts/`)
that says so in its `<head>`:

```html
<meta name="lifehub" content="publish">
<meta name="lifehub:id" content="regex-course">      <!-- optional: the address; default — the file's name -->
<meta name="description" content="…">                <!-- optional: the library's line; default — the first <p> -->
```

Push it like any change of the base; a few minutes later it is up. From the phone: send the file to the bot with the
caption `to hub <Space>` (e.g. `to hub travel`; the Russian `в хаб <Space>` works too) — the router's code, not
the model, checks it (HTML, UTF-8, at most
3 MB, a Space with its `AGENTS.md`), adds the `<meta>` if it is missing, commits it to `<Space>/artifacts/<name>.html`
with the trailer `Retinue-Upload: telegram` and answers with the address. A page is 3 MB at most, pictures included;
libraries and fonts are never in git (below).

**The one barrier, and what stands behind it.** A page lives on the hub's own origin, and a script of the hub's
origin could `fetch('/now/')` — your calendar, mail and trips. What stops a page's script is the `sandbox` directive
in the `Content-Security-Policy` header nginx sends with every response under `/pages/` (`sandbox allow-scripts` for
yours, bare `sandbox` for hers; in the header — `sandbox` does not work in `<meta>`). It gives the document an opaque
origin, `null`: to it the hub is another origin, and the hub sends no CORS anywhere but `/pages/_vendor/`, so it can
neither read the hub's pages nor its own neighbours; no cookies, no `localStorage` (a course's progress is not kept
across reloads; the pages catch the error and work). That is a single barrier, not two: if a page were ever served
without its sandbox, it would be a document of the hub's origin. Behind it, the second line: no network at all
(`connect-src 'none'` — a `fetch` fails before it leaves the browser), nothing loaded but `/pages/_vendor/` (every
source names it with the hub's host, not `'self'`: what `'self'` means for an opaque origin is not settled across
browsers), no frames, no forms.

What must never change, for that reason:

- the Traefik router `retinue-lifehub-pages` (`Host(hub) && PathPrefix(/pages/)`, priority 1000) and its
  middlewares — `lifehub-owner` and `lifehub-key`, the key and no CSP. Through `lifehub-headers` the hub's policy
  would come too, and two policies are both enforced: the page's scripts would die (safe, but broken);
- every `add_header` under `location ^~ /pages/` in nginx.conf: each location sets all its headers itself (an
  `add_header` there drops the server's), and each says `always`, so a 404 under `/pages/` is sandboxed too;
- `Access-Control-Allow-Origin` on `/pages/_vendor/` only: on any other path of the hub it would let a sandboxed page
  read it.

**Scripts by author.** The file's whole history decides (every commit that ever touched that exact path, both sides
of a merge, the path read literally — `*` in a name is a character, not a glob):

- no commit of the router's identity without the bot's `Retinue-Upload` trailer — yours from the Mac, a lane's
  session's, the bot's — `sandbox allow-scripts`, inline scripts run;
- any commit of hers (her turn, committed under the router's identity; a revert the router made counts too) —
  `script-src 'none'` and the build strips every script, handler and `javascript:` address: she reads mail and the
  web, and a page is where an injected script would run. An edit of yours on top does not change that: read it and
  publish it under a new name (a new file has no history of hers), and its scripts run.

A page's path is plain — `a-z 0-9 / _ . -`, ending in `.html` — or it is not published. Two files that claim one
`lifehub:id`: yours wins when exactly one is yours; otherwise neither goes up. At most 200 pages.
The bot never replaces a file: a name that is taken gets `-2`, `-3`… and the answer says so.

**What the build does** (`deploy/lifehub/build.py`, no network). The release has two halves, `hub/` (Hugo) and
`pages/`. Each page is rewritten: an exact CDN address in `vendor.json` (highlight.js, mermaid — in attributes and in
scripts' strings alike) becomes `/pages/_vendor/…`; a Google Fonts link becomes `/pages/_vendor/fonts.css` when
every family is vendored (Geologica, JetBrains Mono, Literata, Noto Sans Math), otherwise it goes and the system font
stays;
`preconnect`/`prefetch`/`preload` hints go. Then it is checked, and refused when it has a `<base>`, a
`<meta http-equiv=refresh>`, an `<iframe>`/`<object>`/`<embed>`/`<frame>`, anything loaded from another host (`src`,
`srcset`, `poster`, `<link href>`, `@import`, `url(http…)`, a form's `action`), no `<title>`, no opt-in, a taken
id, more than 3 MB. Links out (`<a href>`) are fine. A script that uses `WebSocket`, `EventSource`,
`RTCPeerConnection`, `sendBeacon`, `window.open`, `location.href =`/`assign`/`replace`, `document.cookie`, `import(`,
a `Worker` or an outside address is a tripwire: the page goes up with its scripts off and the reason in
`pages.json` (`notes`). A refused page is left out of the release and named in `build.json` and the health line
(«pages not published: …») by its id and the check in fixed words — never an address or any other text of the
page: the health line reaches the model. Each entry of `pages.json` carries its file's sha256; a file that does not
match (the router between two writes) waits for the next build. Comment forms a browser and the build's parser read
differently (`<!-->`, `<!--->`, `--!>`, `<![`) are refused; inside `<svg>`/`<math>` a `<script>` or `<style>` is
read as markup, as a browser reads it.

**Vendoring.** `deploy/lifehub/vendor.json` lists every file a page may load: the CDN address, the file under
`/pages/_vendor/`, its sha256. The image fetches them when it is built and refuses one whose sha256 differs, like
Hugo. A new library or version: add it there (with its sha256) and rebuild the image. `/pages/_vendor/` is the only
path of the hub with CORS (`Access-Control-Allow-Origin: *`): a sandboxed page's origin is opaque, so even a font
from the hub is a cross-origin fetch.

**`pages.json`** (the router writes it; the build passes on only the pages that went live, with `scripts` as served
and `notes`) — the hub's library reads `assets/pages.json`, a list, newest first:

| field | |
|---|---|
| `id`, `url` | the address on the hub: `/pages/<id>.html` (the library links it as a local path) |
| `title`, `description` | from `<title>`, `<meta name="description">` or the first paragraph |
| `topic` | the Space, the first folder of the path; `path` — the file in the base |
| `words`, `reading_min` | the visible words; minutes at 180 a minute |
| `updated` | the date of the last commit to the file |
| `author`, `scripts` | `owner` / `assistant`; `on` / `off` |
| `sha256` | of the file's bytes; `path` — the file in the base |

**In the app («Read»).** The router pushes the list to the platform (`Lifehub.library`, `bridge.push_library`):
each page's id, title, full address, Space, day and reading time — no description, no body, no path in the base; a
page the last build refused (build.json) is left out, a page with no `<title>` goes by its id. The face lists them
under «Read» and opens each on the hub, never inside the app ([face.md](face.md#read)). The platform takes a
page's address only as `<hub_origin>/pages/<id>.html`, `hub_origin` from its `platform.yaml`.

**Turn it on.** Pages come with the hub and the base; nothing of their own in DNS or `setup.sh`.

1. The base's `/srv/retinue/memory/policy.json`: add `"*/artifacts/**/*.html"` to `writable` (the router's commits
   are checked by the assistant's rules — the bot's pages go there) and run `setup.sh` again: the hub gets a copy.
2. `sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build` — the
   builder's image with the vendored files, nginx with `/pages/`, the router `retinue-lifehub-pages`.
3. Check, from a device with the VPN on: `curl -sI https://hub.in.example.com/pages/<id>.html` — 200 and a
   `Content-Security-Policy` that starts with `default-src 'none'` and ends in `sandbox allow-scripts` (or `sandbox`
   for hers), and only one CSP header; `curl -sI https://hub.in.example.com/` — the hub's own policy, no
   `Access-Control-Allow-Origin`; VPN off — 404. Open a page on the iPhone: highlighting, diagrams, fonts.

What it does not stop: a page with scripts can still navigate itself elsewhere with its own text in the address, or
use WebRTC — `connect-src` covers neither, and no browser has `navigate-to`. That is why the assistant's pages run no
scripts, and why the tripwires exist (a signal, not a proof: obfuscated code passes them).
