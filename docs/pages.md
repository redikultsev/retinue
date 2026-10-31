# Pages

Your own HTML pages — a course, a cheat sheet, a checklist, anything a claude.ai artifact made, a small site — open
on your devices at `https://pages.in.<your domain>/<id>`, with their scripts, highlighting and diagrams, and **keep
what you do in them**: a tick, a quiz's answers, a course's progress, a form's records are saved on the server, so a
reload or another device has them. A page you **share** goes on a public site of yours — `https://pages.<your
domain>/<id>`, or the home page of a site of its own like `https://notes.<your domain>/` — where anyone sees it with
what it keeps, kept up to date within seconds of every change. Nothing is public until you share it. The app lists
them under «Pages» ([face.md](face.md#pages)).

```
base (git) ── router: pages.json, pages/*.html; trip pages ──▶ pages-build (no network): checked release, current
                                                                          │
your phone / Mac ── WireGuard ── Traefik (your devices only, adds the key) ──▶ pages (retinue-pages): shell, page,
                                                                          │     its store (SQLite), «Share»
                                                                          ▼ writes the shared pages' files
anyone ── Traefik (the public sites, at a pace) ──▶ pages-public (nginx, static, read-only)
```

Three containers, each with one job. **pages-build** checks every page with no network and switches its live
release. **pages** serves that release to your devices only and keeps what each page saves. **pages-public** is a
static nginx with no store and no API: the internet reaches nothing but the files the service wrote for the pages you
shared. None of them shares an origin, a cookie, an API or a network with the app.

## Turn it on

### 1. DNS (once)

Let's Encrypt checks a name over plain HTTP, so every name needs a public A record pointing at the server — like
your Matrix name. At your DNS provider:

- `pages.in.<your domain>` → the server's public address. Your devices reach it through the VPN only (below);
  Traefik answers anyone else with 404. The name itself is public — every certificate goes to the CT logs.
- each public site, e.g. `pages.<your domain>` (and later `notes.<your domain>`…) → the same address. These answer
  anyone.

```bash
dig +short @1.1.1.1 pages.in.example.com        # the server's public address
```

### 2. Your devices reach the owner's host through the VPN

Traefik lets through the WireGuard addresses of your devices (wg-easy shows them, e.g. `10.8.0.2` for the Mac,
`10.8.0.3` for the phone) — one list for the pages and the app, `/srv/retinue/devices.txt`. A device must therefore
go to the server **through the tunnel**: its DNS must be the VPN's `10.8.0.1` (split DNS answers `10.8.0.1` for
every `*.in` name), otherwise it resolves the public record, goes over the internet and gets 404.

```bash
dig +short pages.in.example.com                 # on the device, VPN on: 10.8.0.1
```

On the iPhone: WireGuard app → the tunnel → **Edit** → **DNS servers**: `10.8.0.1` → Save.

### 3. The server

```bash
cd /opt/retinue && sudo git pull
sudo TELEGRAM_OWNER_ID=<id> PAGES=1 PAGES_HOST=pages.in.example.com DEVICES=10.8.0.2/32,10.8.0.3/32 \
  PAGES_PUBLIC_HOSTS=pages.example.com bash deploy/setup.sh
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d --build
```

`setup.sh` keeps the address in `/srv/retinue/pages/host`, the public sites in `pages/public_hosts.txt` and the
devices in `/srv/retinue/devices.txt` (later runs need no switch), makes a key in `secrets.env`, renders
`/etc/dokploy/traefik/dynamic/pages.yml` (it carries the key; root's), adds `pages_url` to `router.yaml` and the
public sites' rule to `stack.env`. In `platform.yaml`, write `pages_origin: https://pages.in.example.com`: the app
opens pages there and nowhere else. A new device: edit `devices.txt` and run `setup.sh` again. A new public site:
its DNS record, then `PAGES_PUBLIC_HOSTS=pages.example.com,notes.example.com` once more, then `up -d`.

### 4. Check

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://pages.in.example.com/       # VPN on: 200; VPN off: 404
sudo cat /srv/retinue/pages/site/build.json                               # "ok": true, the live release
curl -sI https://pages.example.com/x | head -1                            # 404 until something is shared
```

The morning brief's health line says «pages built N min ago» or why they are not up to date.

## Publishing a page

A page is any `*.html` in a nature folder of the base (`<Space>/knowledge|profile|journal|artifacts/`) that says so
in its `<head>`:

```html
<meta name="lifehub" content="publish">
<meta name="lifehub:id" content="regex-course">      <!-- optional: the address; default — the file's name -->
<meta name="description" content="…">                <!-- optional: the list's line; default — the first <p> -->
```

(The word is the retired life hub's; the pages already in the base say it, so it stays.) Push it like any change of
the base; a minute later it is up at `https://pages.in.<your domain>/regex-course`. From the phone: send the file to
the bot with the caption `to pages <Space>` (e.g. `to pages travel`; `to hub <Space>` and the Russian `в хаб <Space>`
work too) — the router's code, not the model, checks it (HTML, UTF-8, at most 3 MB, a Space with its `AGENTS.md`),
adds the `<meta>` if it is missing, commits it to `<Space>/artifacts/<name>.html` with the trailer
`Retinue-Upload: telegram` and answers with the address. A page is 3 MB at most, pictures included; libraries and
fonts are never in git (below).

**Trip pages.** When the assistant publishes a trip (`publish_trip`: her words and, for each option, travel-ops'
`search_id` and `link`), the router looks every option up in travel-ops' stored search (no site is asked), takes the
price, the seller, the times, the rating and when the price was seen from there, and writes the page itself — static
HTML, every value escaped, no script, stay photos re-encoded as JPEG inside it — as `trip-<16 hex>`, section «trips».
It goes through the same build as any page.

**Scripts by author.** The file's whole history decides (every commit that ever touched that exact path, both sides
of a merge, the path read literally — `*` in a name is a character, not a glob):

- no commit of the router's identity without the bot's `Retinue-Upload` trailer — yours from the Mac, a lane's
  session's, the bot's — the page's scripts run;
- any commit of hers (her turn, committed under the router's identity; a revert the router made counts too) —
  `script-src 'none'` and the build strips every script, handler and `javascript:` address: she reads mail and the
  web, and a page is where an injected script would run. An edit of yours on top does not change that: read it and
  publish it under a new name (a new file has no history of hers), and its scripts run.

A page's path is plain — `a-z 0-9 / _ . -`, ending in `.html` — or it is not published. Two files that claim one
`lifehub:id`: yours wins when exactly one is yours; otherwise neither goes up. An id starting with `trip-` is the
router's. At most 200 pages. The bot never replaces a file: a name that is taken gets `-2`, `-3`… and the answer
says so.

## What a page keeps

A page runs in a frame on an origin of its own that the browser makes up for it — the `sandbox` — so it has no
`localStorage`, no cookies and no network of its own. The service gives it back storage, through the shell around it:

- **`localStorage`** and **`sessionStorage`**, as a browser's (`getItem`, `setItem`, `removeItem`, `clear`, `key`,
  `length`, keys as properties), synchronous: the page is served with its saved values in it. A page that already
  keeps its progress in `localStorage` keeps it now with no change. `sessionStorage` lives for the visit.
- **`window.storage`**, the shape of Claude artifacts' persistent storage — `await window.storage.get(key)`,
  `set(key, value)`, `delete(key)`, `list(prefix)`, each with an optional `shared` flag; values are strings (store
  JSON as text). A claude.ai page that uses it works as it is. `shared` and personal are two names for this page's
  own values: there is one owner.

Every change goes to the shell, which saves it a moment later (`PUT /api/store/<id>`); the bar says «Saving…»,
«Saved», or «Not saved: …» with why, and tries again. Two devices: each change is saved key by key, the last write of
a key wins. A page whose scripts do not run keeps nothing.

**Limits** (a save over one is refused whole, the page's values as they were): a key is at most 512 characters, a
value 1 MB, a page 2000 keys and 5 MB, all pages 500 MB; text only. The values stay when a page leaves the base and
come back with it under the same id. They are in the nightly backup (the volume `pages-data`).

## Sharing

The bar's **Share** → a public site (one of `PAGES_PUBLIC_HOSTS`) and whether the page is that site's **home page** →
the link (`https://pages.example.com/regex-course`, or `https://notes.example.com/` as a home page), with **Copy**
and **Stop sharing**. One page can be on several sites; a site's home page shared again by another page is replaced.

- **What a visitor sees**: the page with its scripts, its highlighting and fonts, and **what it keeps** — your ticks,
  your records — read-only. His own clicks work in his tab and are not kept (a reload starts from yours). Nothing of
  the owner's host, the store or any other page is reachable from there.
- **Up to date**: every change of the page in the base, and every value it saves, writes its public copy again within
  seconds. A page that leaves the base leaves its sites; it comes back to them if it comes back.
- **Stop sharing** deletes its files: the link is 404 at once.
- **Links**: a link between two pages you shared on one site opens there; a link out opens in a new tab.
- Indexing is not blocked on a public site: it is yours, under your name. A page the assistant wrote runs no script
  there either.

## How it is kept apart

**The page is never a document of the service's origin.** `/<id>` is the **shell** — the service's own page and
script (`retinue/pages_static/`): the title, your bar, the frame. The page itself is `/raw/<id>` in that frame,
sandboxed twice with the same permissions — the frame's `sandbox` attribute and the response's CSP `sandbox`
(`allow-scripts allow-forms allow-modals` when its scripts run; `allow-popups allow-popups-to-escape-sandbox` when they
do not, so a link opens a window of its own) — because a browser grants only what both grant. So the page has an
opaque origin: to it the shell, the store and every other page are another origin, and none sends it CORS but
`/pages/_vendor/`. Behind that, no network at all (`connect-src 'none'`), nothing loaded but `/pages/_vendor/` (named
with the host, never `'self'`), no frames, no forms sent (`form-action 'none'`), no workers, no popups or navigation
of its own when its scripts run: a link goes to the shell, which opens it with no referrer.

**A page names no page.** Its values come from the shell over a MessageChannel the shell hands its frame at each
load; the shell saves them for the id in its own address. `/raw/<id>` answers only a frame of the same origin loading
it (`Sec-Fetch-Dest: iframe`, `Sec-Fetch-Site: same-origin`): a page that points its frame at another page's raw
address gets 403, not that page's values, and so does a raw address opened in a tab. The shell opens a window only
from your click, with `noopener` and `noreferrer`, and is `Cross-Origin-Opener-Policy: same-origin`.

**The owner's host answers only Traefik.** Traefik lets through your devices and adds `X-Pages-Key`; any other
request — a container on Traefik's network that is not Traefik — gets 404. The API takes a call only from the shell:
`X-Pages: 1`, an `Origin` of its own, `Sec-Fetch-Site: same-origin`; no cookie exists to steal. Every answer carries
its headers, an error's too: a response under `/raw/` without its sandbox would be a document of this origin.

**The public sites are static.** `pages-public` serves `/public/<host>/` read-only: the shell, the page with its
values written into it, the shell's script and the vendored files, under the same sandbox and the same `Sec-Fetch`
rule. Traefik routes only the configured sites there, at 20 requests a second (60 in a burst) and 30 at once, and
overwrites `X-Pages-Key` with nothing.

What it does not stop: a page whose scripts run reads what it keeps itself, and can put it into a link you click — the
shell opens what you click. That is why the assistant's pages run no script, and why the build's tripwires exist (a
signal, not a proof: obfuscated code passes them).

## What the build does

`deploy/pages/build.py`, no network. Each page is rewritten: an exact CDN address in `vendor.json` (highlight.js,
mermaid — in attributes and in scripts' strings alike) becomes `/pages/_vendor/…`; a Google Fonts link becomes
`/pages/_vendor/fonts.css` when every family is vendored (Geologica, JetBrains Mono, Literata, Noto Sans Math),
otherwise it goes and the system font stays; `preconnect`/`prefetch`/`preload` hints go. Then it is checked, and
refused when it has a `<base>`, a `<meta http-equiv=refresh>`, an `<iframe>`/`<object>`/`<embed>`/`<frame>`, anything
loaded from another host (`src`, `srcset`, `poster`, `<link href>`, `@import`, `url(http…)`, a form's `action`), no
`<title>`, no opt-in, a taken id, more than 3 MB. Links out (`<a href>`) are fine. A script that uses `WebSocket`,
`EventSource`, `RTCPeerConnection`, `sendBeacon`, `window.open`, `location.href =`/`assign`/`replace`,
`document.cookie`, `import(`, a `Worker` or an outside address is a tripwire: the page goes up with its scripts off and
the reason in its `notes`. A refused page is left out of the release and named in `build.json` and the health line
(«pages not published: …») by its id and the check in fixed words — never an address or any other text of the page:
the health line reaches the model. Each entry of `pages.json` carries its file's sha256; a file that does not match
(the router between two writes) waits for the next build. Comment forms a browser and the build's parser read
differently (`<!-->`, `<!--->`, `--!>`, `<![`) are refused; inside `<svg>`/`<math>` a `<script>` or `<style>` is
read as markup, as a browser reads it. A release is `<id>.html` (scripts off), `scripts/<id>.html` (on), its own
`pages.json` of the pages that went live, and `_vendor/`; `current` moves to it in one rename, and the last three stay.

**Vendoring.** `deploy/pages/vendor.json` lists every file a page may load: the CDN address, the file under
`/pages/_vendor/`, its sha256. The builder's image fetches them when it is built and refuses one whose sha256 differs.
A new library or version: add it there (with its sha256) and rebuild the image.

**`pages.json`** (the router writes it; the build passes on only the pages that went live, with `scripts` as served
and `notes`):

| field | |
|---|---|
| `id`, `url` | the page's address on the owner's host: `/<id>` |
| `title`, `description` | from `<title>`, `<meta name="description">` or the first paragraph |
| `topic` | the Space, the first folder of the path (`trips` for a trip page); `path` — the file in the base |
| `words`, `reading_min` | the visible words; minutes at 180 a minute |
| `updated` | the date of the last commit to the file |
| `author`, `scripts` | `owner` / `assistant`; `on` / `off` |
| `sha256` | of the file's bytes |

**In the app («Pages»).** The router pushes the list to the platform (`Pages.library`, `bridge.push_library`): each
page's id, title, full address, Space, day and reading time — no description, no body, no path in the base; a page
the last build refused (build.json) is left out. The app lists them and opens each on the owner's host, never inside
the app ([face.md](face.md#pages)). The platform takes a page's address only as `<pages_origin>/<id>`, `pages_origin`
from its `platform.yaml`.

## The life hub, retired

Pages were once one part of the **life hub** (`hub.in.<your domain>`), a Hugo site that also had «Now» (the day's
meetings), «Trips», «Tracker», «Status» and «Review» pages. The app took their place — «Today» has the chips, what
waits for you and the deadlines; «Week» how the work went — and the hub's own pages are gone with it; its pages and
trips are the pages service's. `setup.sh` no longer starts it and says how to remove what is left, which is yours to
do: `docker rm -f retinue-lifehub-1 retinue-lifehub-build-1`, `rm /etc/dokploy/traefik/dynamic/lifehub.yml`,
`rm -r /srv/retinue/lifehub`, and the DNS record `hub.in.<your domain>`.
