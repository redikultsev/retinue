# Contributing

Retinue is one person's platform, run on one server. Contributions are welcome when they keep it that way: small,
readable changes that make a part safer, clearer or easier to run for yourself. For anything larger than a fix,
open an issue first and say what you want to change and why, so neither of us spends a weekend on a pull request
that will not land.

## Set up

- Python 3.12 and [uv](https://docs.astral.sh/uv/) for the package in `retinue/`;
- Node 24 and pnpm 11 for the app in `face/` ([face/README.md](face/README.md));
- Docker only for the deployment and the e2e smoke test ([docs/install.md](docs/install.md#tests)).

## Before a pull request

There is no CI: run the checks yourself and say in the pull request that they pass.

```bash
uv run --frozen --with pytest pytest -q
uv run --with ruff ruff check .
pnpm -C face typecheck && pnpm -C face lint && pnpm -C face test
```

## How the code is written

- **English for everything: code, comments, docs, commit messages and every string the owner reads** in the bot, the
  app and the agents' instructions. A new button gets an English label, its doc quotes it in «guillemets». A parser
  of what the owner types keeps accepting the Russian forms it accepted before.
- **A change to behaviour changes its doc in the same pull request.** Each part has one guide in `docs/`; the README
  links them.
- **Security code comes with a test that shows the refusal.** The gate (`retinue/gate.py`, `retinue/approvals.py`),
  the guards (`retinue/exfil.py`, `retinue/sniguard.py`), the egress lists (`deploy/egress/`) and the lane settings
  (`deploy/lanes/`) decide what leaves the server. A pull request that widens any of them says so in its title.
- **No real personal data, anywhere.** Fixtures and examples use `example.com`, `example.org`, `acme`, `<owner-id>`,
  `123456789`, documentation IP ranges (`203.0.113.0/24`) and made-up names. No real mail, chats, notes, hostnames,
  tokens or paths from your own machine.
- **Match the surrounding code**: its naming, its comment density, its idiom. Ruff is a check, not a formatter; there
  is no formatter.
- Commit subjects name the part first: `Platform: …`, `Face: …`, `Docs: …`.

## Reporting a vulnerability

Not in an issue: see [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), as the rest
of the repository is.
