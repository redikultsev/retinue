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
- Commit your work yourself, on a separate branch. What is uncommitted does not get into the push, and the platform
  does not commit for you.
- In the summary, name in one line: the repository, the branch, how many commits and what is left uncommitted.
- The platform's token will not let changes to `.github/workflows/` through. If they are needed, tell the owner: he
  pushes those himself.
- No `/work/code/<name>` folder, and the repository is needed — tell the owner: he connects repositories in the
  platform's settings.
