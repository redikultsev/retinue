# Rules for agents working in this repository

This repository is public. It is one person's platform, and it must say nothing about that person.

- **No personal data, anywhere in the repository.** Not in code, tests, fixtures, mocks, examples, docs, comments,
  commit messages or pull requests. That covers the owner's employers and clients, colleagues, the people in his
  mail and chats, names, where he lives and travels (cities, dates, flights, stays), documents, visas, banks and
  money, health, study, a job search or a move, his other projects and repositories, his hosts, devices and paths,
  and anything taken from his knowledge base, mail or chats.
- **Examples are invented.** `example.com`, `example.org`, `acme`, «Anna Petrova», `<owner-id>`, `123456789`,
  documentation IP ranges (`203.0.113.0/24`), AMS→LIS. A real case that motivates a test is rewritten with made-up
  details before it goes in, including details that would let someone recognise the original (a landmark, a brand,
  a date that matches a real event).
- **When in doubt, it is personal.** Replace it with something invented, or ask.
- Code, comments, docs, commit messages and the strings the owner reads in the product are in English (see the
  README). Behaviour changes come with their doc and tests in the same change ([CONTRIBUTING.md](CONTRIBUTING.md)).
- **Use the skills a task has.** Before any task, look for skills that fit it — the ones installed, the plugin
  catalogue, and the web (Anthropic's `claude-plugins-official` and `knowledge-work-plugins`, `anthropics/skills`,
  skill directories) — read them and work by them; say which you used. For anything the owner sees in the app, that
  is every design skill and guideline you can find (`frontend-design`, design critique, accessibility, UX copy,
  interaction design, the Web Interface Guidelines), applied to every screen the change touches, not to one.
- **The look has its rules.** Before changing a screen, read [docs/design.md](docs/design.md) — what each colour
  means, the type scale, a queue's row, how a change is checked — and write there what your change decides. Its hard
  rules (no emoji, no tracked capitals, no `transition-all`, no text under 11 px) are a test that fails the build.
- **See it before you call it done.** A change to the app is looked at in a browser at the sizes it is used at — a
  laptop window, a large screen, a phone — with short and long content, and on the deployed app after deploying.

## Agent skills

### Issue tracker

GitHub Issues on `retinue-oss/retinue`, which are public, so the rules above apply to every issue. See
`docs/agents/issue-tracker.md`. Two people develop it at once: work starts from an issue, claimed by assigning
yourself before the first line of code; one assigned to someone else is theirs (its «Two of us at once»).

### Triage labels

The five default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See
`docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the root, alongside the per-part guides in `docs/`. See
`docs/agents/domain.md`.
