# Contributing

Retinue is one person's platform, run on one server. Contributions are welcome when they keep it that way: small,
readable changes that make a part safer, clearer or easier to run for yourself. For anything larger than a fix,
open an issue first and say what you want to change and why, so neither of us spends a weekend on a pull request
that will not land.

## Set up

- Python 3.12 and [uv](https://docs.astral.sh/uv/) for the package in `retinue/`;
- Node 24 and pnpm 11 for the app in `face/` ([face/README.md](face/README.md));
- Docker only for the deployment and the e2e smoke test ([docs/install.md](docs/install.md#tests)).

## Branches

Work goes to `develop`: push to it, or open your pull request against it. `main` takes pull requests from
`develop` only, merged as a merge commit; a direct push to `main`, or a pull request into it from any other branch,
is refused ([.github/workflows/main-from-develop.yml](.github/workflows/main-from-develop.yml) and the repository's
ruleset on `main`).

CodeQL scans the Python, the app's TypeScript and the workflows on every push and pull request
([.github/workflows/codeql.yml](.github/workflows/codeql.yml)); its findings show on the pull request's diff and in
the Security tab, and one rated high or critical that a pull request adds keeps it out of `main`. A finding that is
not real is dismissed there with the reason, never silenced in the code.

## Releases

A pull request that changes what the owner sees adds one line in plain words under «Unreleased» in
[CHANGELOG.md](CHANGELOG.md) — the template's «For the owner». A release, from `main`:

1. Through `develop`, rename «Unreleased» to the version and date (`## v0.2.0 — 2026-11-20`) and open a new, empty
   «Unreleased» above it.
2. Once that is in `main`, tag its commit `v0.2.0` and push the tag: the release workflow builds the images and
   publishes them ([.github/workflows/release.yml](.github/workflows/release.yml)).
3. A server takes it with `deploy/update.sh v0.2.0` (docs/install.md, «Updating»): it checks, backs up, switches,
   and goes back by itself when the new version does not come up. The app then shows the release's lines once, under
   «What's new».

## Before a pull request

GitHub runs these checks on every pull request and every push to main or develop
([.github/workflows/ci.yml](.github/workflows/ci.yml)): the same commands, on Linux, where the tests that need Docker
run too, plus the probes on the pinned CLI, where a skip is a failure ([docs/platform.md](docs/platform.md#probes)). Run
them yourself first.

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
- **A rule of the contract with the CLI moves with its code and its test, in one commit.** A numbered rule in
  [docs/platform.md](docs/platform.md#probes), the code, and a test that names the number and was seen failing when
  written. A refusal is checked by a call, not by a saved setting.
- **A test asserts behaviour and data, never wording.** Return codes, refusal flags, parsed config, ids, paths,
  numbers and order are checked by code; what a message, doc or prompt says is read by a person or judged by a model.
  No regex against prose, and no test that scans the repository's texts for a word ([AGENTS.md](AGENTS.md) has the
  full rule).
- **Tests run in parallel.** CI runs pytest in one process per core (`pytest -n auto`, pytest-xdist), and in no fixed
  order, so a test shares nothing with another: it writes only under its own `tmp_path`, never into the checkout or a
  fixed path; a server it starts binds port 0, or a socket it already holds, never a port picked and bound later; a
  container, network or image it makes has a random suffix, or a name that holds its content's hash; the environment
  changes through `monkeypatch` or a subprocess's own `env`. Locally, `-n auto` works the same way. The probes
  (`-m probe`) run in one process: one of them takes a server down and brings it back on the same port.
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
