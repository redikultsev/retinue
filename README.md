# Retinue

**A private assistant that cannot leak.** One assistant talks to you in Telegram and remembers the
conversation in an archive on your own server. It reads your personal data, and therefore has no way out: no
web, no shell, no network except the model API.

> Status: **pilot**. One assistant in one long session, a searchable archive of the conversation, reminders and
> a morning summary in your time zone, closed egress. Mail, a web-search tool behind a schema and approvals for
> messages to other people are next. Expect breaking changes.

## Why

A personal agent with your private data, the web and an outbox is one prompt injection away from mailing
your life to a stranger. Retinue keeps the data and removes the exits, and the removal is done by the system,
not by a prompt:

- **No exits, by construction.** The assistant's container sits on an internal network with no route to the
  internet; its only way out is a proxy that lets through the model API and nothing else. It has no Bash, no
  file tools, no web tools. Its config and instructions are mounted read-only.
- **One conversation, one session.** The assistant talks to you in a single long session that Claude Code
  compacts when it grows; `!compact` squeezes it on request, `!new` starts over. The transcript lives on the
  assistant's own volume. Whatever the session has lost is still in the archive, and the assistant searches it.
- **One router holds every token and writes every word down.** Both sides of every turn go to an append-only
  archive before and after the model is called. The assistant searches the archive through the router; the
  file is never mounted into its container.
- **Time is the router's, not the model's.** Every request carries your local time and weekday. The assistant
  sets reminders with tools (`set_reminder`, `list_reminders`, `cancel_reminder`, `move_reminder`); the router
  checks them — the weekday against the date, the past, a repeat — and wakes the assistant on time: she tells
  you in her own words and adds what she notices in the conversation; if she cannot, the router sends the
  reminder word for word. A morning summary arrives every day at 09:00: written by a separate run outside the
  conversation, or bare if that run fails. A subscription limit is told to you without the model, and the
  refused turn runs again when the window opens.
- **Nothing the assistant writes becomes a link.** Every address in its text reaches you as monospace text,
  with link previews off on every path.
- **Reuse over rewrite.** Claude Agent SDK, a2a-sdk, Squid, SQLite FTS5. Retinue is the thin layer between them.

## How it works

```
You (Telegram) ──► Router ──A2A──► Assistant ──► egress proxy ──► model API, nothing else
                     │   ◄──bus──┘ (search_archive, reminders)
                     ├─ Archive (SQLite, append-only, full-text)
                     ├─ Scheduler (reminders, the morning summary, a retry after the subscription limit)
                     └─ Protocol log and model runs (SQLite)
```

- `retinue/router.py` — entry point: the core plus the channels in the config (Telegram; Matrix is optional).
- `retinue/core.py` — one owner message at a time; archives both sides; tells the assistant what happened in the
  conversation without it; buttons and messages the system sends on its own.
- `retinue/archive.py` — the raw archive: append-only events with a full-text index.
- `retinue/scheduler.py` — reminders and the router's own timed jobs; `retinue/clock.py` — the owner's local time.
- `retinue/bus.py` — what an agent may ask the router for: search the archive, or (when granted) another agent.
- `retinue/agent_host.py` — one agent behind an A2A server; one container, its trust boundary.
- `retinue/engine.py` — the `Engine` seam. Today: Claude Agent SDK, deny-by-default tools, no settings read
  from disk. Other engines plug in behind the same interface.
- `agents/assistant/` — the assistant: `agent.yaml` (tools, limits) and `CLAUDE.md` (instructions).

## Model access

The `Engine` uses the Claude Agent SDK. Use an Anthropic API key (`ANTHROPIC_API_KEY`) or, for your
own personal deployment, your own Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN` from
`claude setup-token`). Check Anthropic's current terms for your case.

## Install

See [docs/install.md](docs/install.md). Requirements: a Linux server with Docker, a Telegram bot, and
[Dokploy](https://dokploy.com) (or plain `docker compose`).

## License

[Apache-2.0](LICENSE)
