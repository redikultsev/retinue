# Retinue face

The owner's web app for the platform: sessions by track, a live transcript, approval and courier cards with the
full input, the changes of a code session with line comments, two sessions side by side. A PWA served as static
files by nginx; `/api/*` goes to the platform's face API ([docs/face.md](../docs/face.md#api)). Russian UI, mobile first, dark and
light by `prefers-color-scheme`.

## Commands

All from the repository root with `pnpm -C face …` (Node 24, pnpm 11):

| command | what |
|---|---|
| `pnpm -C face install` | dependencies, exactly as in `pnpm-lock.yaml` |
| `pnpm -C face dev` | the mock API (:9601) and Vite (:5173, proxying `/api/*` to the mock). Open `http://127.0.0.1:5173/login#RTNE7K2QXP` |
| `RETINUE_API=http://… pnpm -C face dev` | Vite only, proxying `/api/*` to a real face API |
| `pnpm -C face mock` | the mock API alone (`MOCK_PORT`, `MOCK_CODE` to change port and login code) |
| `pnpm -C face build` | typecheck, then the static app in `face/dist/` |
| `pnpm -C face test` | Vitest + Testing Library: the event reducer, the stream (one EventSource, 410, iOS reconnect), the approval card, login, transcript items, the service worker's routing |
| `pnpm -C face typecheck` | `tsc -b` (strict) over the app, the tests, the mock and the scripts |
| `pnpm -C face lint` | Biome |
| `node face/scripts/icons.ts` | redraws the PWA icons in `public/icons/` |

## The mock

`mock/server.ts` (data in `mock/fixtures.ts`) implements the whole contract in memory: login by code (printed at
start) and by Telegram init data (shape only, no HMAC), `meta`, the session list and snapshots, the event stream
with `delta`, input, stop, new sessions (each runs a short scripted turn with streamed text), decisions with the
digest check, and the diff. Fixtures cover every transcript item kind, a long Bash command, an Edit, a TodoWrite
plan, a subagent, a question, a shown card, a card whose input is over Telegram's 4096 limit, and a courier card;
for the compact look and «Сводка», a turn of eight calls in a row (Bash, reads, an MCP tool, WebFetch, an error),
sub-sessions (one queued), a ping with its replies, unpushed commits and a pushed PR, Base files, a pinned row, and
every snapshot's `about` (folder, model, context, MCP servers).
Session `5566aabb` streams forever, for watching two panels at once.

Mock-only helpers: `POST /api/_mock/approval {"session": "0a1b2c3d"}` adds a shown card (an `approval` event);
`POST /api/_mock/prune` prunes the event log and ends the streams, so the app gets 410 and reloads its snapshots.
The mock's cookie is `retinue_mock` without `Secure` (plain http); the real one is `__Host-retinue`.

## How it works

- **One store** (`src/lib/store.ts`): a pure reducer over snapshots and stream events. Every event is an upsert of
  one whole object by key (`session`, `item`, `items_reset`, `approval`, `summary`, `input`); a snapshot carries its cursor,
  and events of a session at or below it are skipped, so a snapshot is never rolled back by an older event.
  `delta` text is kept per session until the final `message`/`result` item or the session leaving `active`.
- **One stream** (`src/lib/live.ts`): one `EventSource` for the whole app (E8: two panels, one connection). An
  HTTP error closes it; its status is then probed: 410 → reload the snapshots and resume from the new cursor,
  401 → login. A different `schema` reloads the page. Back after more than 45 s hidden (iOS freezes background
  PWAs), the stream is reopened from the last event id.
- **Routes** (the whole table is in docs/face.md, «The shell»): `/login` (code, `#code` from the bot's link, or the
  Mini App's `tgWebAppData`), `/` (goes to `/today`, «Сегодня»), `/sessions`, `/spaces…`, `/s/<id>` (`?tab=changes`, `?with=<id>` for a
  second panel on screens ≥ 1280 px), `/a/<approval id>` (one card from `GET /api/approvals/{id}` — the Mini App deep link;
  `startapp=a_<id>` works too), `/read` («Читать»: the hub's pages, each opened on the hub; docs/face.md).
- **Cards** show the header lines the platform built, the full input in a scrollable monospace block (invisible
  characters written as `⟦U+…⟧`, as the platform does), and «Разрешить» / «Отказать», which post the action
  with the card's `digest`.
- **PWA**: `public/manifest.webmanifest`, icons, `apple-touch-icon` and iOS meta tags; `public/sw.js` caches the
  app shell only and never intercepts `/api/*`.
- **Untrusted text**: model output is rendered by Streamdown with raw HTML off (no `rehype-raw`, `skipHtml`),
  media and embedding elements removed, and images replaced by their alt and URL as plain text, so nothing in a
  transcript loads from the network by itself; links keep Streamdown's confirmation before leaving.
- **Cards** wrap the input (nothing hides past the right edge), show its line and character count, and mark long
  runs of spaces (≥ 8), many blank lines (≥ 3) and invisible or bidi characters as visible ⟦…⟧ labels; a Bash
  card lists every field of the call, not only `command`.

## Content-Security-Policy (REQUIRED)

nginx (part D) must send exactly this header with the app:

```
Content-Security-Policy: default-src 'self'; img-src 'self' data:; connect-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'
```

Why each relaxation is needed, and nothing more:

- `style-src 'unsafe-inline'`: the diff view (Shiki inside `@pierre/diffs`) writes token colours as inline `style`
  attributes. Scripts stay `script-src 'self'`: the build has no inline script and no `eval` (Shiki runs its
  JavaScript regex engine, not WebAssembly, so no `'wasm-unsafe-eval'` either).
- `img-src data:`: kept from the agreed policy; the current build does not need it, and a `data:` image cannot
  reach the network. No remote image is ever rendered.
- `frame-ancestors 'none'` blocks the app in an iframe. The Telegram Mini App on phones opens it in a web view, not an
  iframe, so it works; Telegram Web and Desktop, which frame Mini Apps, would need
  `frame-ancestors https://web.telegram.org` — not needed for the owner's phone, left out on purpose.

## Dependencies

Exact versions, each released at least two weeks before it was added; `pnpm-workspace.yaml` sets
`minimumReleaseAge` so transitive ones follow the same rule.

Runtime (in the bundle):

| package | version | why |
|---|---|---|
| react, react-dom | 19.3.0 | the UI |
| @tanstack/react-router | 1.170.42 | typed routes and search params (`/s/<id>?with=…`); code-based, no generator plugin |
| streamdown | 2.7.0 | markdown that renders half-written text safely while it streams (`delta`), sanitised (rehype-sanitize/harden) |
| @pierre/diffs | 1.5.2 | the «Изменения» tab: parses `git diff`, highlights, and has line click + line annotations, which the line comments need. Loaded lazily (its own chunk) |
| react-resizable-panels | 4.14.4 | two sessions side by side, resizable (E8) |
| class-variance-authority, clsx, tailwind-merge | 0.7.1, 2.1.1, 3.7.0 | what the copied shadcn/ui components are written with |

Development only:

| package | version | why |
|---|---|---|
| vite, @vitejs/plugin-react | 8.3.4, 6.1.2 | dev server with the `/api` proxy, the build |
| tailwindcss, @tailwindcss/vite | 4.3.3 | styles (shadcn/ui is written in Tailwind) |
| typescript | 7.0.2 | strict typecheck |
| vitest, jsdom | 5.0.3, 30.1.2 | tests in a DOM |
| @testing-library/react, @testing-library/dom | 16.3.3, 10.4.2 | component tests |
| @biomejs/biome | 2.5.15 | lint (one binary instead of ESLint and its plugins) |
| @types/react, @types/react-dom, @types/node | 19.3.0, 19.3.0, 26.6.5 | types |

Deliberately not used: Radix (the native `<dialog>` and plain buttons with ARIA roles are enough here), an icon
package (a few inline SVGs), a PWA plugin (the service worker is 80 lines), Telegram's `telegram-web-app.js`, remote
fonts, analytics.

## Serving

The nginx image, its CSP header and the Mini App button in the bot are in `deploy/face/` and docs/face.md.
Third-party notices: [NOTICE](NOTICE).
