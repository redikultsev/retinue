# Retinue

**A private retinue of AI agents.** Specialists — study, travel, career, life — live in your own
Matrix server, talk to you from your phone and (soon) to each other, while your personal data stays
on your machine and nothing leaves it without your explicit "yes".

> Status: **pilot**. Owner ↔ agent conversations over Matrix and A2A work. Agent ↔ agent requests,
> sensitivity labels, approvals and schedules are next. Expect breaking changes.

## Why

Personal agents fail in two ways: they leak (one agent with your private data, the web and an
outbox is one prompt injection away from mailing your life to a stranger), or they are so locked
down that you approve every keystroke. Retinue splits the work instead:

- **Trust classes are enforced by the OS, not by prompts.** An agent with your private notes has no
  web; an agent with the web has no private notes. Each agent runs in its own container.
- **One router holds every token.** Agents never see Matrix credentials and never talk to each
  other directly; every exchange goes through the router and lands in an append-only protocol log.
- **Standard protocols.** Agents speak [A2A](https://a2a-protocol.org); the owner uses any Matrix
  client (Element X on iPhone and Mac).
- **Reuse over rewrite.** Claude Agent SDK, a2a-sdk, mautrix, Tuwunel. Retinue is the thin layer
  between them.

## How it works

```
You (Element X) ──► Matrix (Tuwunel, VPN-only) ──► Router (appservice) ──A2A──► Agent hosts
                                                       │                        ├─ Study  (web, no base)
                                                       └─ Protocol log (SQLite) └─ Travel (web, no base)
```

- `retinue/router.py` — Matrix appservice. Each agent is a virtual user with its own room. Only the
  owner's messages are forwarded; agents reply as notices, so bots never answer bots.
- `retinue/agent_host.py` — one agent behind an A2A server. Remembers the conversation per A2A
  context; the session id never comes from a message.
- `retinue/engine.py` — the `Engine` seam. Today: Claude Agent SDK with deny-by-default tool
  permissions. Other engines plug in behind the same interface.
- `agents/<id>/` — an agent is a config (`agent.yaml`: trust class, skills, allowed tools) plus a
  workspace with its instructions (`CLAUDE.md`).

## Model access

The `Engine` uses the Claude Agent SDK. Use an Anthropic API key (`ANTHROPIC_API_KEY`) or, for your
own personal deployment, your own Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN` from
`claude setup-token`). Check Anthropic's current terms for your case.

## Install

See [docs/install.md](docs/install.md). Requirements: a Linux server with Docker, a WireGuard VPN,
a domain, and [Dokploy](https://dokploy.com) (or plain `docker compose`).

## License

[Apache-2.0](LICENSE)
