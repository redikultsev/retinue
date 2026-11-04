# What an install asks of the owner today, and where each answer lives

Research for issue #13 of the wayfinder map "Retinue set up and grown from the app" (#11). Facts come from the files
of `origin/main` at `5d88e5b`; each one is cited as `file:line`. Nothing here was run on a server. Three claims were
checked by running the real script or command on a laptop and are marked **run**. Examples are invented.

## How to read it

Proposed class, one letter per row:

- **A** - in the app from the first run (the wizard asks it).
- **B** - in the app later (a settings section, changeable any time).
- **C** - stays on the server (the install script or the operator does it; the why is in the row).

Where the app itself cannot open without an answer, the row says **bootstrap**: the first page needs it, so it cannot
be asked by that page. The class there is the one that is possible today.

Columns: item, what it is, read by which container and when, what changing it takes, secret (S) yes/no, class and the
one-line why.

## 0. Four facts that shape every table

1. **`router.yaml` is rewritten whole on every `setup.sh` run** (`deploy/setup.sh:241-312`). A hand edit of it is lost
   on the next run. The only supported hand-written inputs are `owner.yaml`, `checkout.txt`, `sensitive.txt`,
   `policy.json` (all under `memory/`), and they are read by `setup.sh` (`:201-212`, `:291-299`).
2. **`platform.yaml` is written once and then never touched** (`deploy/setup.sh:423-424`). Nothing in it follows a
   later `setup.sh` run.
3. **Switches stay on once their folder or file exists** (`TRAVEL`, `MEMORY`, `MAIL`, `PAGES`, `TGBUSINESS`, `SEND`,
   `CALWRITE`, `PLATFORM`, `FACE`, `BACKUP`: `deploy/setup.sh:47-98`, `:665`). Passing `0` later does nothing; there is
   no off.
4. **The app's login goes only through the Telegram bot.** `/app` in the bot gives a code; the Mini App is checked with
   a key derived from the bot token (`docs/face.md:63-66`, `deploy/setup.sh:604-620`, `retinue/config.py:599`). The
   platform also refuses to start without `CLAUDE_CODE_OAUTH_TOKEN` (`retinue/config.py:590`, a plain `_env`) and the
   router without `TELEGRAM_BOT_TOKEN` (`retinue/config.py:199`). So the bot token, the owner's id, the face
   address, the devices list and the Claude token are **bootstrap** today.

## 1. The `setup.sh` command: switches and values

| Item | What it is | Read by / when | Changing it takes | S | Class and why |
|---|---|---|---|---|---|
| `TELEGRAM_OWNER_ID` (`setup.sh:27`, `:300-302`, `:589`) | The one Telegram user the bot answers. Required every run (`:30-33`). | Router (`router.yaml: telegram.owner_id`), tg-business and platform (`stack.env`). At start. | Rerun `setup.sh` (it must be passed again every time: `router.yaml` is rebuilt), restart router, platform. | no | **C** bootstrap: the app logs in through this id (`config.py:600`, `:610`). |
| `OWNER_TZ` (`:43-44`, `:244`, `:691`) | The owner's IANA zone; default is the zone already in `router.yaml`, else UTC. | Router (`router.yaml: owner_tz`) at start; the backup timer unit (`:691`). **Not** the platform. | Rerun `setup.sh` + restart router (+ edit `platform.yaml` by hand, see 5.1). | no | **A** (wizard step), then **B**: the owner can already move it by words (`docs/proactive.md:50`), so the app should show the live value. |
| `MATRIX_SERVER_NAME`, `MATRIX_OWNER`, `PRIVATE_SUFFIX` (`:28-29`, `:303-310`, `:514-539`) | The retired Matrix channel; writes the appservice file, a Traefik file and split DNS. | Router, Tuwunel, host Traefik, dnsmasq. | Rerun with the values; no way to remove. | no | **C**: off by default, needs DNS and a VPN (`docs/install.md:215-217`); out of scope for the app. |
| `ROTATE_BUS_SECRET=1` (`:46`, `:220`) | New bus secret and new agent tokens. | Router, assistant. | Rerun + redeploy. | no (acts on S) | **C**: a break-glass action on secrets; keep it on the server. |
| `TRAVEL=1` (`:48-51`, `:260-265`, `:322-329`, `:625-628`) | travel-ops: trip search and price watches. | travel-ops, travel-watch, router (`travel_url`), assistant, platform. | Rerun; fill `TRAVEL_OPS_GIT` and `travel/profile.yml`; build (about 5 GB). | no | **B**: a switch plus two inputs, but the image build is a server job. The app can request it; the server builds it. |
| `MEMORY=1` (`:52-56`, `:331-364`) | The knowledge base she writes, and its hub (a bare git repository with a pre-receive check). | Router, assistant, platform. | Rerun; push the owner's repository into the hub; restart. | no | **A**: the Base is the heart of the product; the wizard should offer it. The hub creation is a server job the app triggers. |
| `MAIL=1` (`:58-62`, `:222`, `:398-404`) | The mail collector: Gmail and Calendar, read only. | collector (+ its proxy), router. | Rerun; run `deploy/mail/login.py` on the owner's computer for each account. | no | **B**: needs a Google Cloud project and a browser consent (`docs/mail.md:23-39`). |
| `PAGES=1`, `PAGES_HOST`, `PAGES_PUBLIC_HOSTS`, `DEVICES` (`:63-66`, `:141-197`, `:374-380`, `:573-586`) | The owner's HTML pages: a VPN-only host, optional public sites, the device list. | pages, pages-build, pages-public, router (`pages_url`), Traefik file. | Rerun; DNS records; `up -d`. `PAGES_PUBLIC_HOSTS=` (empty) removes all public sites (`:172`). | `PAGES_KEY` yes | **B**: a host name and a list; but DNS and the VPN stay with the operator (`docs/pages.md:30-47`). |
| `TGBUSINESS=1` (`:70-74`, `:416-419`, `:590-595`) | The Telegram Business gateway for chosen chats. | tg-business, router. | Rerun; second bot from BotFather; token into `stack.env`; connect it in Telegram settings (`docs/courier.md:52-62`). | token yes | **B**: the steps happen in Telegram; the app can hold the token and the switch. |
| `SEND=1` (needs `MAIL=1`) (`:76-81`, `:111-114`, `:405-409`) | The mail sender (replies by the owner's card). | mail-send, router. | Rerun; `login.py --kind send`. | token yes | **B**: same as mail. |
| `CALWRITE=1` (needs `MAIL=1`) (`:82-87`, `:115-118`, `:410-415`) | The calendar writer. | calwrite, router. | Rerun; `login.py --kind calwrite`. | token yes | **B**: same as mail. |
| `PLATFORM=1` (`:88-92`, `:420-510`) | The platform and its lanes (Claude Code sessions); needs `TELEGRAM_OWNER_ID`. | platform, lane-private, proxies, reach, router. | Rerun; container probe before the first session (`:745-746`). | tokens yes | **A**: the app is the platform's app; without it there is nothing to show. |
| `FACE=1`, `FACE_HOST` (`:95-98`, `:181-184`, `:194-197`, `:383-394`, `:608-623`) | The app itself, over WireGuard only, behind Traefik with a key. | face (nginx), platform, host Traefik. | Rerun; DNS record; `up -d --build`. | `FACE_KEY` yes | **C** bootstrap: this is what serves the app. Its address and VPN cannot be typed into the app they serve. |
| `BACKUP=1` (`:47`, `:638-698`) | Nightly restic backup to S3 (host systemd timer). | Host only. | Rerun; fill `backup.env`; rerun again to enable the timer. | keys yes | **B**: the five values can be typed in the app and handed to the host; installing restic and the timer is a host job. |
| `CALWRITE`, `SEND`, `TG`, `PAGES`, `FACE` folders written "for good" (`:73`, `:79`, `:85`, `:91`, `:98`) | The marker that keeps a switch on. | `setup.sh` only. | Delete the folder or file by hand. | no | **C**: an implementation detail; the app should show state, not the marker. |
| `RETINUE_ROOT`, `RETINUE_HOST_SETUP` (`:40`, `:45`) | Test-only paths. | `setup.sh`. | n/a | no | **C**: tests only. |

## 2. `stack.env` (mode 600, the stack's environment) and `secrets.env`

`stack.env` is read by `docker compose --env-file` and handed to containers as listed in `deploy/compose.yml`.
`secrets.env` is only a store for generated tokens: `setup.sh` sources it (`:238`) and copies values into `stack.env`.
A change to either takes `docker compose up -d` (recreate) for the container that reads it.

| Key | What it is | Read by / when | Changing it takes | S | Class and why |
|---|---|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` (`setup.sh:554`; `compose.yml:92`) | The bot's token from BotFather. Filled by hand. | Router at start (`config.py:199`); the face's login key is derived from it (`setup.sh:611-615`). | Edit, rerun `setup.sh` (to re-derive the key), recreate router and platform. | yes | **C** bootstrap: the only login to the app is this bot. The wizard can ask it only after a first login path exists. |
| `CLAUDE_CODE_OAUTH_TOKEN` (`setup.sh:552`; `compose.yml:130`, `:452`) | The owner's Claude subscription token (`claude setup-token`). | Assistant at start; platform at start, sent with each lane run, never stored. | Edit, recreate assistant and platform. | yes | **C** bootstrap today (the platform will not start without it, `config.py:590`). **A** if the platform may start without it and the app asks it. |
| `ELEVENLABS_API_KEY` (`setup.sh:553`; `compose.yml:94`, `:469`) | Speech to text for voice notes and the app's dictation. Optional. | Router at start; platform (`config.py:762`, popped from its environment). | Edit, recreate router and platform. | yes | **B**: optional, a clean "add a key later" case. |
| `TELEGRAM_BUSINESS_TOKEN` (`setup.sh:592`; `compose.yml:306`) | The second bot's token. | tg-business at start. | Edit, recreate. | yes | **B**: see `TGBUSINESS`. |
| `TRAVEL_OPS_GIT` (`setup.sh:625`; `compose.yml:152`) | The owner's travel-ops git URL at a pinned commit; a build argument. | `docker compose build` only. | Edit, rebuild (about 5 GB). | URL may hold a token | **C**: a build input of a private image; keep on the server. |
| `COMPOSE_PROFILES` (`setup.sh:565-569`) | Which optional services run. | Compose at `up`. | Rerun, `up -d`. | no | **C**: derived from the switches; the app shows services, not this string. |
| `RETINUE_TRAVEL_URL`, `RETINUE_MEMORY` (`setup.sh:570-571`) | Empty or the internal URL/path that turns travel tools and file tools on. | Assistant, platform at start. | Rerun. | no | **C**: derived. |
| `PAGES_HOST`, `PAGES_ORIGIN`, `PAGES_KEY`, `PAGES_PUBLIC_HOSTS`, `PAGES_PUBLIC_RULE`, `PAGES_APP_ORIGIN` (`setup.sh:575-586`) | The pages service's addresses, key and Traefik rule. | pages, pages-public, Traefik labels. | Rerun, `up -d`. | `PAGES_KEY` yes | **C**: derived from the switch; the host names are asked once under `PAGES=1`. |
| `FACE_HOST`, `RETINUE_FACE_ORIGIN`, `RETINUE_WEBAPP_KEY` (`setup.sh:609-615`) | The app's address and the derived Mini App key. | Platform at start; Traefik labels. | Rerun after any bot-token change, recreate platform. | key yes | **C** bootstrap and derived. |
| `TELEGRAM_OWNER_ID` (`setup.sh:589`) | Copy of the command value, written only if the gateway or face is on. | tg-business, platform. | Rerun. | no | **C**: derived. |
| `RETINUE_BUS_SECRET`, `RETINUE_BUS_TOKEN_ASSISTANT`, `RETINUE_COLLECTOR_TOKEN`, `RETINUE_GATEWAY_TOKEN`, `RETINUE_SENDER_TOKEN`, `RETINUE_CALWRITE_TOKEN`, `RETINUE_PLATFORM_TOKEN`, `RETINUE_LANE_TOKEN_PRIVATE`, `RETINUE_LANE_TOKEN_CODE`, `RETINUE_LANE_TOKEN_WEB`, `RETINUE_HOOK_SECRET` (`setup.sh:221-231`, `:550-551`, `:572`, `:590-603`) | Generated service-to-service tokens (`openssl rand -hex 32`). | Router and the peer it signs for. | Rotation: `ROTATE_BUS_SECRET=1` (bus only); the others are made once and kept. | yes | **C**: generated, never typed by the owner; the app must not display them. |
| `MATRIX_SERVER_NAME`, `MATRIX_ALLOW_REGISTRATION`, `MATRIX_REGISTRATION_TOKEN`, `RETINUE_AS_TOKEN`, `RETINUE_HS_TOKEN` (`setup.sh:629-635`) | Matrix variables. `MATRIX_ALLOW_REGISTRATION` starts `true` and the owner turns it off by hand. | Tuwunel, router. | Edit, redeploy. | tokens yes | **C**: Matrix is off by default. |
| `RETINUE_IMAGE`, `RETINUE_VERSION` (`docs/install.md:132-137`; `update.sh:36-41`) | Which release images run. | Compose (image names), router, platform (shown version). | `deploy/update.sh vX.Y.Z` (snapshots, rolls back if unhealthy). | no | **C**: the update is a host action with a rollback; the app may show the version. |

`secrets.env` holds the same generated tokens (`RETINUE_BUS_SECRET`, `RETINUE_COLLECTOR_TOKEN`, `PAGES_KEY`,
`RETINUE_GATEWAY_TOKEN`, `RETINUE_SENDER_TOKEN`, `RETINUE_CALWRITE_TOKEN`, `RETINUE_PLATFORM_TOKEN`,
`RETINUE_LANE_TOKEN_*`, `RETINUE_HOOK_SECRET`, `FACE_KEY`, Matrix tokens: `setup.sh:217-237`). Class **C**: made by
the script, copied to `stack.env`, never typed.

## 3. `router.yaml` (rewritten whole on every run)

Read by: the router container at start (`compose.yml:77`, mounted read-only; `RouterConfig.load`,
`retinue/config.py:168-239`). Changing a written key takes a rerun of `setup.sh` and a router restart. A key that
`setup.sh` does not write cannot be set at all without losing it on the next run.

| Key | Written from | Class and why |
|---|---|---|
| `owner`, `default_agent`, `agents[assistant]` with its rights (`setup.sh:243-259`) | fixed text | **C**: one fixed agent; not an owner choice. |
| `owner_tz` (`:244`) | `OWNER_TZ` | **A**/**B**: see section 1. |
| `travel_url`, `link_hosts` (`:261-264`) | `TRAVEL`, `deploy/travel/link-hosts.txt` | **C**: derived. |
| `collector_url`, `pages_url`, `gateway_url`, `sender_url`, `calwrite_url`, `platform_url`, `face_url` (`:266-290`) | the switches and hosts | **C**: derived. |
| `proactive.week` (`:283-286`) | `memory/owner.yaml: week` | **B**: the owner's own list of "week's numbers" (`docs/memory.md:51-67`); belongs in settings next to the collections it counts. |
| `memory.hub`, `checkout`, `sensitive`, `location` (`:291-299`) | `/srv/retinue/memory/checkout.txt`, `sensitive.txt`, `owner.yaml: location` | **B**: what of the Base she sees and which Spaces are closed are owner decisions; today a file edit plus two restarts. |
| `telegram.owner_id` (`:300-302`) | `TELEGRAM_OWNER_ID` | **C** bootstrap. |
| `matrix.*` (`:303-310`) | Matrix variables | **C**. |
| keys read by the router but never written: `voice` (`config.py:123-129`, `:175-179`; `docs/proactive.md:12-17`), `memory.digest_at`, `memory.author`, `backup_status`, `health_status`, ports | defaults in code | `voice.engine` is **B** (a real owner choice). The rest **C**. See 5.6.9. |

## 4. `platform.yaml` (written once from `deploy/platform/platform.example.yaml`)

Read by: the platform container at start (`compose.yml:428`, read-only; `PlatformConfig.load`,
`retinue/config.py:525-612`). Changing anything takes `docker compose restart platform` (`example:2`).

| Key | What it is | S | Class and why |
|---|---|---|---|
| `state_db` (`example:3`) | Path of the platform's database. | no | **C**: internal. |
| `max_sessions` (`example:4`) | Sessions that may run at once. | no | **B**: a limit the owner may tune; the room has a fixed memory cap (`compose.yml:539-603`). |
| `owner_tz` (`example:5`; `config.py:507`) | The zone the platform speaks to the owner in. Ships as `UTC`. | no | **A**/**B**: the same answer as the router's; today asked twice (see 5.1). |
| `pages_origin` (`example:6-8`, commented; `config.py:513-514`, `:603-605`) | The one origin the app's "Pages" may open. | no | **C** derived: it is `https://` + `PAGES_HOST`, which `setup.sh` already holds. See 5.3. |
| `lanes.private.*` (`example:14-22`) | The room: url, hook url, mount, `max_sessions`, `web: true`, `deny` (the owner's own domains as rules). | no | `deny` is **B** (owner's own domains, also in `open-deny-domains.txt`); the rest **C**: wiring that must match `compose.yml`. |
| `tracks[]` (`example:29-33`) | Named places a session starts: `notes`, `research`, `base`, `code`, with `lane`, `kind`, `cwd`, rules. | no | **C** for the wiring; **B** for the owner's own always-allow, always-deny and always-card rules. |
| `spheres[]` (`example:39-43`) | Work, Travel, Home, Assistant: the app groups sessions by them and each is a Telegram command. | no | **B**: the map's decision 3 (no built-in projects) removes the shipped four; what replaces them is a later ticket. |
| `speech.keyterms`, `speech.proxy` (`example:48-51`) | Words the dictation should expect; the way out to ElevenLabs. | no | **B**: `keyterms` are the owner's words; `proxy` is **C**. |
| `code:` (commented, `example:59-66`) | The owner's repositories in the room: lane, track, proxy, `author`, `repos[]` with `token:` keys. | tokens yes (elsewhere) | **B**: the repository list is the owner's; the tokens go through `set-git-token.sh` (section 6). |
| `base:` (commented, `example:71-75`) | The Base's clone in the room; `author: "Name <email>"` is required (`config.py:536`, `:700`). | no | **A**: the author name is the one fact `setup.sh` cannot know; the wizard asks it. See 5.2. |
| `voice:` (`config.py:640-650`; `docs/proactive.md`) | Platform side of the session-voice. Not in the example. | no | **B**. |
| `face_port`, `listen_port`, `out_store_mb`, `out_day_mb`, `skills_mount`, `owner_memory_mount` (`config.py:497-512`) | Defaults in code; never in the example. | no | **C**: internal. |

## 5. Disagreements: a file written once, or a rule fixed in code, against what the owner gave

Each item says how it was checked. **run** means the real script or command was run; **read** means traced in code.

### 5.1 `OWNER_TZ` reaches `router.yaml` and the backup timer, not `platform.yaml` (read)

`setup.sh` writes `owner_tz: $OWNER_TZ` into `router.yaml` (`:244`) and into the backup unit's `OnCalendar` (`:691`).
`platform.yaml` is installed once with `owner_tz: UTC` (`example:5`) and `[[ -f ... ]] ||` stops any later write
(`:423-424`). The platform validates and uses that value (`config.py:507`, `:583-586`) for "the times the platform says
to you". So a zone given on a later run changes the router and the backup time but not the platform.

There is a third clock: the owner can move the zone by words, and the router keeps that in its own state, which wins
over `router.yaml` (`docs/proactive.md:50-52`). A later `setup.sh` run without `OWNER_TZ` reads the old value from
`router.yaml` (`:43`), not the word-moved one. Three places, three possible values.

### 5.2 `base:` ships commented out, also with `MEMORY=1` and `PLATFORM=1` (read)

The example has `#base:` and its lines commented (`example:67-75`). `setup.sh` knows both switches are on
(`MEMORY_ON`, `PLATFORM_ON`) and creates the Base folders (`:434-440`) but never writes `base:`; its message only says
"Empty until `base:` is in platform.yaml" (`:432-433`). `docs/base.md:360-362` tells the owner to `sudoedit`. Without it
`/work/base` stays an empty folder, while the shipped tracks and spheres all point at `cwd: base`
(`example:30-33`, `:40-43`). The only fact the script cannot know is the author "Name <email>", which the platform
requires (`config.py:536`).

### 5.3 `pages_origin` is a host `setup.sh` already holds, and it is also written by hand (read)

`PAGES_HOST` is stored in `pages/host` (`setup.sh:191`), in `stack.env` as `PAGES_HOST` and `PAGES_ORIGIN`
(`:577-578`) and in `router.yaml` as `pages_url` (`:270`). `platform.yaml` has it commented
(`example:6-8`), and `docs/pages.md:66-67` tells the owner to write it. Without it the router's list of pages is
refused and "Pages" stays empty (`docs/face.md:90-92`).

### 5.4 `policy.example.json` runs `scripts/lint.py`, which a Base may not have (read, partly run)

The shipped `check` is `["python3", "-I", "scripts/lint.py"]` (`policy.example.json:87`). `kbcheck.lint` runs it on
every push when `check` is set (`retinue/kbcheck.py:177-198`). A Base without that script gives a non-zero exit
(**run**: `python -I scripts/lint.py` in a folder without it exits 2 with "can't open file ... No such file or
directory"); the last line becomes the refusal `just check: ...` (`kbcheck.py:196-198`). That refuses the owner's own
push to the hub and the platform's and router's commits alike. The example also assumes a Space layout
(`knowledge/`, `profile/`, `journal/`, `artifacts/`: `:79-81`) and non-English text in `marker` and `owner_mark`
(`:78`, `:86`), none of which a new owner's Base has to follow. An empty `check` is valid (`kbcheck.py:181-182`).

### 5.5 `install-skills.sh` installs an empty set from a folder that has `skills/` inside (run)

The docs say a set with no skill left is refused and the installed set stays (`docs/instructions.md:149`;
script header `install-skills.sh:101-103`). **Run** with a source folder `src/skills/foo/SKILL.md`:

```
skipped skills: no SKILL.md
0 skills:
installed in <dest>/skills            (exit 0, <dest>/skills is empty)
```

Why: the loop takes each direct subfolder of `<source>` (`install-skills.sh:122`); the only one is `skills/`, which has
no `SKILL.md`, so it is skipped by the shell line (`:127`). `prune-skills` then sees an empty folder: it fails only
when a *pruned* skill printed a line starting with `skipped` (`retinue/instructions.py:555-559`), and the shell's own
`skipped` line is not counted. `check-skills` accepts zero skills (`instructions.py:300-316`). The old set is moved
away and the empty one swapped in (`install-skills.sh:138-139`). This is the shape `deploy/skills/sync-skills.sh`
builds (a folder with `skills/` inside), so pointing the installer at the sync's parent folder empties the owner's
skills silently.

### 5.6 Other places where the file and the owner's answer part ways

| # | What | Evidence | Checked |
|---|---|---|---|
| 5.6.1 | **Policy in two copies.** The router reads `policy.json` live (`compose.yml:85`); the hub keeps a copy made only at a `setup.sh` run (`setup.sh:348`). Until the rerun, the router and the hub judge a push by different rules. | `docs/memory.md:29-30` says to rerun. | read |
| 5.6.2 | **`checkout.txt` and `sensitive.txt` reach the router only through `router.yaml`** (`setup.sh:291-297`), so an edit needs a rerun and a router restart. | `docs/memory.md:31-35` | read |
| 5.6.3 | **Switches cannot be turned off.** `TRAVEL=0` after `TRAVEL=1` does nothing: `TRAVEL_ON` stays 1 while `travel/profile.yml` exists, and the same for the others. | `setup.sh:50`, `:56`, `:61`, `:66`, `:73`, `:79`, `:85`, `:91`, `:98`, `:665` | read |
| 5.6.4 | **A later run without the Matrix variables** rewrites `router.yaml` with no `matrix:` section and drops `matrix` from `COMPOSE_PROFILES`, but leaves `MATRIX_SERVER_NAME`, the tokens and `MATRIX_ALLOW_REGISTRATION` in `stack.env` and the appservice file and Traefik/DNS files on disk. | `setup.sh:303`, `:565-569`, `:629-635` (no `drop` branch) | read |
| 5.6.5 | **`TELEGRAM_OWNER_ID` must be passed on every run.** Omitted, the `telegram:` section silently leaves `router.yaml` when a Matrix channel carries the run; with only the other switches the script stops with "no channel" (`:30-33`). `stack.env` keeps the old value. | `setup.sh:300-302`, `:589` | read |
| 5.6.6 | **The Mini App key goes stale on a new bot token.** `RETINUE_WEBAPP_KEY` is derived only at a `setup.sh` run; a changed `TELEGRAM_BOT_TOKEN` leaves the old key until the next run. Without a token at run time the key is dropped and the Mini App login is off (`/app` codes still work). | `setup.sh:608-620`, `:762-763`; `docs/face.md:63-66` | read |
| 5.6.7 | **The open proxy's address list is written once.** `open-deny-ips.txt` is refilled only while it has no entry (`setup.sh:492`), so a changed public address of the server is not picked up. | `setup.sh:492-501` | read |
| 5.6.8 | **The same answer in two files.** Own domains: `egress/open-deny-domains.txt` and `platform.yaml` lane `deny` (`example:20-22`). A project's hosts: `egress/projects/<name>.yaml` and `egress/platform-hosts.txt` (`deploy/egress/projects/travel.example.yaml:2-3`). The commit identity: `code.author` and `base.author` (`example:63`, `:72`). Devices: `devices.txt` and the rendered Traefik files (`setup.sh:375-393`), which change only at a run. | cited | read |
| 5.6.9 | **`router.yaml` keys that cannot be set durably.** `voice:` is documented as an install setting of `router.yaml` (`docs/proactive.md:12-17`), but `setup.sh` rewrites the file and never writes it; `owner.yaml` accepts only `location` and `week` (`setup.sh:204-206`). The same holds for `memory.digest_at` (`config.py:101`, docs `docs/memory.md:96`). | `setup.sh:241-311`; grep for `voice` in `setup.sh` finds only comments | read |
| 5.6.10 | **Dead tokens for retired lanes.** With the platform on, tokens for `code` and `web` are made and written (`setup.sh:93`, `:229`, `:599-603`), though both lanes are retired by the example (`example:10-13`). | cited | read |

## 6. Files edited by hand, and scripts run with `sudo` or on the owner's computer

| Item | What it is | Read by / when | Changing it takes | S | Class and why |
|---|---|---|---|---|---|
| `memory/policy.json` (`setup.sh:201`) | What she may write, what is never hers, the Base's lint. | Router (live mount, `compose.yml:85`), the hub (copy, `setup.sh:348`), platform (hub's copy). | Edit, rerun `setup.sh`. | no | **B**: owner rules; but see 5.4 and 5.6.1. The shipped default should be neutral. |
| `memory/checkout.txt` (`:202`, `:293-294`) | Sparse-checkout patterns: what of the Base the assistant's folder holds. | Router at start. | Edit, rerun, restart. | no | **B**. |
| `memory/sensitive.txt` (`:203`, `:296-297`) | Closed Spaces for the draft cards. Default `*` closes all. | Router at start. | Edit, rerun, restart. | no | **B**. |
| `memory/owner.yaml` (`:204-213`) | `location`, `week`. Created by the owner; refused for any other key. | `setup.sh` into `router.yaml`. | Edit, rerun, restart. | no | **B**. |
| `devices.txt` (`:159-188`) | The owner's WireGuard addresses (`a.b.c.d/32`), one list for the pages and the app. | `setup.sh` into Traefik files. | Edit, rerun. | no | **C** bootstrap: it decides who can reach the app. |
| `pages/host`, `pages/public_hosts.txt`, `face/host` (`:168-197`) | Kept hostnames. | `setup.sh`. | Rerun with the variable. | no | **C**. |
| `egress/allowed-hosts.txt` (`:512`; `docs/install.md:154-159`) | Hosts the assistant's proxy lets through; starts with `api.anthropic.com`. | `egress` (Squid) at start. | Edit, `restart egress`. | no | **C**: a security allowlist; keep it on the server. |
| `egress/mail-hosts.txt` (`:403`) | The collector's proxy list (Google hosts). | mail proxy. | Edit, restart. | no | **C**. |
| `egress/platform-hosts.txt` (`:452-468`) | The platform's way out: GitHub, GitLab, ElevenLabs. | platform-egress. | Edit, `restart platform-egress`. | no | **C**. |
| `egress/lane-code-hosts.txt` (`:441-443`) | The retired lane-code's list. | legacy profile only. | n/a | no | **C**: retired. |
| `egress/open-*.txt`, `open-squid.conf` (`:469-503`) | The room's open proxy: our rules (replaced each run) and the owner's domains and server addresses (kept). | lane-private-egress. | Edit, `restart lane-private-egress`. | no | **C**: a security control; `open-deny-domains.txt` is the owner's domains, see 5.6.8. |
| `egress/projects/<name>.yaml`, `egress/project-keys.env` (`:456-458`) | A project's own hosts and API key (`RETINUE_PROJECT_KEY_<NAME>`). | platform, read-only. | Create, add the hosts to `platform-hosts.txt` too, restart platform and egress. | key yes | **B** once projects are pluggable: a project brings its hosts. The key is the owner's secret. |
| `code/tokens.env`, `deploy/code/set-git-token.sh` (`:450-451`; script `:1-249`) | One git token per repository owner; written with echo off, mode 600. | platform (`compose.yml:474-478`). | `sudo bash set-git-token.sh <key>`, restart platform. | yes | **B**: a token typed in the app, written by the host. The script is the "how a secret reaches the container" model. |
| `travel/profile.yml` (`:322-329`) | Home airports, travellers, currency. | travel-ops. | Edit, restart. | no | **B**. |
| `backup.env` (`:639-674`) | `RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`. | Host backup service. | Edit, rerun to enable the timer. | all yes | **B**; the password's off-server copy stays with the owner (`docs/backup.md:95-98`). |
| `deploy/skills/install-skills.sh` (`:105-140`) | Copies the owner's skills into `/srv/retinue/skills/skills` after a check; swapped in whole. | lane-private and platform, read-only, each run (no restart). | `sudo bash install-skills.sh <folder>`; `--empty` clears. | no | **B**: the owner's skills belong to a project; today a command on the server. See 5.5. |
| `deploy/memory/install-owner-memory.sh` (`:1-204`) | Copies the owner's Claude Code memory notes (`*.md`) as a read-only snapshot. | lane-private, each run. | `sudo bash ... <folder>`; `--empty`. | maybe | **B**. |
| `deploy/mail/login.py` (`:1-40`) | Browser consent per Google account and kind; puts tokens on the server over ssh. | collector, sender, calendar writer. | Run on the owner's computer with `RETINUE_SSH_HOST` and `~/.config/retinue/google-client.json`. | yes | **B**: the consent needs a browser on the owner's side; the app can start it. |
| `deploy/backup/mac.sh`, restic key steps (`docs/backup.md`) | The owner's computer: init the repository, add the server's key, monthly restore check. | Owner's computer. | by hand | yes | **C**: the point is that it happens off the server. |
| `deploy/update.sh` (`:1-40`) | Update to a release tag with snapshot and rollback. | Host. | `sudo bash update.sh vX.Y.Z` | no | **C**. |
| `deploy/evals/run.py` (`docs/platform.md:1396-1397`) | Behaviour checks, run with `sudo`. | Host. | by hand | no | **C**: operator tool. |
| Container probes (`docs/install.md:72-90`; `docs/platform.md:Turning it on`) | One-time checks after the first deploy. | Host. | by hand | no | **C**. |
| `ROOT` ownership steps (`setup.sh:356-363`, `:401`, `:408`, `:414`, `:418`) | `chown` to fixed uids, groups `retinue-memory` (10001) and `retinue-base` (10020), `usermod` of the sudo user. | Host. | `setup.sh` | no | **C**. |

## 7. What no file holds: prerequisites outside the stack

| Item | What it asks | Where it is documented | Class and why |
|---|---|---|---|
| The server, Docker, packages | Debian 13 or similar, git, curl, python3, openssl, iproute2, apparmor, Docker with compose (`docs/install.md:16-27`) | install.md | **C**: before any app exists. |
| Bot from BotFather; user id lookup; cloud password | `/newbot`, threaded mode off, two-step verification (`docs/install.md:7-14`) | install.md | **C** bootstrap: outside the stack and before the app. |
| WireGuard, split DNS, Traefik/Dokploy, certificates | VPN addresses for devices, DNS `10.8.0.1`, public `A` records for each public host (`docs/pages.md:30-47`, `docs/face.md:40-56`) | pages.md, face.md | **C**: the HTTPS name and VPN are what the map already leaves to the install script. |
| Google Cloud project and Desktop client JSON | Gmail and Calendar APIs, consent screen, published app (`docs/mail.md:23-39`) | mail.md | **B**: steps in Google's console that no app can do; the app can show the checklist. |
| Second Telegram bot and Business settings | Secretary Mode, Chat Automation (`docs/courier.md:52-62`) | courier.md | **B**: steps in Telegram; the app can hold the token. |
| S3 bucket, provider, restic keys | An account at another provider; a key without delete (`docs/backup.md`) | backup.md | **C**: outside the stack; the values are **B**. |
| ElevenLabs account | A key with `speech_to_text` only and a credit limit (`docs/install.md:43-47`) | install.md | **B**: the key is typed in the app. |
| Git hosting tokens | Fine-grained GitHub token or GitLab project token (`docs/code.md:76-77`) | code.md | **B**. |

## 8. Counts and what stands out

Counts are of rows in the tables above (a row with two letters is counted once for each).

81 rows carry a class (sections 1 to 4, 6 and 7), counted by the first letter given:

- **A**, in the app from the first run: 6 (the zone, the Base and its author, the platform, the Claude token if the platform may start without it, and the owner's two-letter A/B rows).
- **B**, in the app later: 33 (optional services and their tokens, voice key, backup values, the owner's rules files, repositories and project hosts, skills, memory snapshot, Google and Business consents).
- **C**, stays on the server: 42 (the server, VPN, HTTPS names, devices, every generated token, egress allowlists, update, probes, ownership steps).

Nine rows give two letters (for example "A for the first run, B afterwards"); each is counted once, for its first letter.
Several C rows are marked bootstrap (the bot token and owner id, the face address, the devices list, the Claude token
today): the app cannot ask what it needs in order to open.

What stands out:

1. **A very small first-run set** is enough to make the app useful: zone, the Base's author, a Claude token, the bot
   token and owner id (bootstrap), the pages host, and the switches `MEMORY`, `PLATFORM`.
2. **Bootstrap is the hard edge.** The app opens only after the bot token, the owner's id, the face address, the
   devices and (today) the Claude token exist. What the install script must keep is the server, the VPN, the HTTPS
   name and those few values. The Claude token is the one that could move into the app if the platform may start
   without it.
3. **Six disagreements are one pattern.** A value is asked once, written to one file, and the other files that need
   it are written once and never again (5.1, 5.2, 5.3, 5.6.1, 5.6.2, 5.6.8). Each is a derived value that `setup.sh`
   already holds, or an owner answer stored in two places.
4. **Three are plain bugs** whatever the app does: the shipped `scripts/lint.py` check (5.4), the empty skills set
   (5.5) and the tokens/state left after a Matrix run is dropped (5.6.4).
