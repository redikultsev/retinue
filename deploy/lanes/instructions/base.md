## The owner's Base — in `/work/base`

If `/work/base` has a `CLAUDE.md`, it holds a clone of his knowledge base — the same Base he has on his Mac (an empty
folder means the Base is not connected). Work with it by its rules (`/work/base/CLAUDE.md` and what it includes), with
one change: git here is run by the platform, not by you. The Base is written in Russian: keep its Records, headings
and fixed lines in Russian, as its rules say; answer the owner in English.

- Before a turn the platform pulls the fresh Base from the hub into the folder, if nobody on this lane is working right
  now. It does not touch your uncommitted changes.
- Write to the Base as you go, as the owner's agents on the Mac do: a new Record gets its row in its Space's Showcase
  (below the marker) in the same turn, and before your turn ends run the Base's check, `python3 scripts/lint.py` in
  `/work/base` (that is `just check`; there is no `just` here), and fix what it says.
- The commit and the push are the platform's: when your run ends it commits and pushes everything you changed in the
  Base, and the owner reads one line of it under your summary. There is nothing for him to press.
  Do not run `git` in `/work/base` — there is no repository there — do not create one, and do not offer
  "commit and push?" or `git pull`: on the server, rule 6 of the Base's `CLAUDE.md` is carried out by the platform.
- When something kept your files out, the platform tells you in a message of its own ("Message from the platform, not
  from the owner"): the check's words, or a file that now holds both versions between conflict markers because the
  owner changed the same lines from another device meanwhile. Fixing it is your work, not his: merge both sides into
  one text (his side is the newer word), remove every marker, keep the Base's rules, and say in one line what you
  did. Ask the owner only when you truly cannot settle it yourself — his own Record says one thing and yours another,
  and nothing tells which is right.
- If the turn changed files of the Base, name them in the summary in one line:
  `In the Base: travel/journal/…, travel/AGENTS.md`.
- `scripts/`, `justfile`, `.py` and `.sh` files and hidden ones (`.claude/…`, `.gitignore`) do not go from the server
  into the Base. If they need changing, tell the owner: that is done from the Mac.
- Files for the owner still go to `out/` of the working folder: `out/` and `inbox/` do not go into the Base.
