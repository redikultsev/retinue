## The owner's Base — in `/work/base`

If `/work/base` has a `CLAUDE.md`, it holds a clone of his knowledge base — the same Base he has on his Mac (an empty
folder means the Base is not connected). Work with it by its rules (`/work/base/CLAUDE.md` and what it includes), with
one change: git here is run by the platform, not by you. The Base is written in Russian: keep its Records, headings
and fixed lines in Russian, as its rules say; answer the owner in English.

- Before a turn the platform pulls the fresh Base from the hub into the folder, if nobody on this lane is working right
  now. It does not touch your uncommitted changes.
- The commit and the push are done by the platform, on the owner's «To the Base» button after the turn.
  Do not run `git` in `/work/base` — there is no repository there — do not create one, and do not offer
  "commit and push?" or `git pull`: on the server, rule 6 of the Base's `CLAUDE.md` is carried out by the platform.
- If the turn changed files of the Base, name them in the summary in one line:
  `In the Base: travel/journal/…, travel/AGENTS.md`.
- The Base's check is `python3 scripts/lint.py` in `/work/base` (that is `just check`; there is no `just` here).
- `scripts/`, `justfile`, `.py` and `.sh` files and hidden ones (`.claude/…`, `.gitignore`) do not go from the server
  into the Base. If they need changing, tell the owner: that is done from the Mac.
- Files for the owner still go to `out/` of the working folder: `out/` and `inbox/` do not go into the Base.
