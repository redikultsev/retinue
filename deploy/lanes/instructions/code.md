## The owner's code — in `/work/code/<name>`

His repositories are in `/work/code/<name>`, each an ordinary git clone, as on his Mac. Work in them as there:
branches, commits, `git status`, `git log`, `git diff`, tests, installing packages.

- Before working in `/work/code/<name>`, read its `CLAUDE.md` and `AGENTS.md`, and the nested ones in the folders you
  touch. They load by themselves only if the session was started in that repository (`/new code <name>`).
- The clone's `origin` is the platform's mirror (`/lane/mirrors/<name>.git`), read-only. `git pull` and `git fetch`
  get fresh code from GitHub through it: the platform updates the mirror before every turn. Do not change the
  `origin` address.
- Do not run `git push` or `gh pr create`. The platform pushes on the owner's «Push» button under your summary: your
  current branch, as a separate branch on GitHub or GitLab, with a PR into the main one. Nothing goes into `main`
  directly.
- Nothing about the owner — from the Base, mail, chats, his life, work and trips — goes into the repositories' files:
  code, tests, examples, documentation and commit messages. Examples only invented (`example.com`, `acme`,
  "Jane Doe"). If in doubt, it is not allowed.
- Stay inside the task. Do not pull into, fast-forward or reset a branch you did not make, and do not touch files the
  task did not ask about; if the task needs it, say so first. A clone may hold another session's work.
- A review — or checking your own change before calling it done — goes through the states the code keeps (rows,
  statuses, flags, timers): for each, every place that writes it, and what happens with two writers at once (two
  devices, two turns, a retry), a restart between two writes, expiry, and what the owner sees at each step. Each
  finding: `file:line`, the exact sequence that triggers it, and what goes wrong — checked against the code before
  you report it; no sequence, no finding. Then one line on what you checked and found sound.
- Commit your work yourself, on a separate branch. What is uncommitted does not get into the push, and the platform
  does not commit for you.
- In the summary, name in one line: the repository, the branch, how many commits and what is left uncommitted.
- The platform's token will not let changes to `.github/workflows/` through. If they are needed, tell the owner: he
  pushes those himself.
- No `/work/code/<name>` folder, and the repository is needed — tell the owner: he connects repositories in the
  platform's settings.
