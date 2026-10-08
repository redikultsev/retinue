# Knowledge base

The assistant can keep a knowledge base of yours: markdown files in a git repository that you also work in from
your own computer. She edits it with Claude Code's own file tools (`Read`, `Grep`, `Glob`, `Edit`, `Write`; never
`Bash`), and after each of her turns the router — code, not the model — checks the change, commits it and pushes it
to the hub. A change that fails the checks is undone, and both of you are told why.

```
your computer ── git push/pull over the VPN ──┐
                                               ▼
                     /srv/retinue/memory/hub.git   (bare; pre-receive = retinue/kbcheck.py)
                                               ▲
router: /kb-git (repository) + /kb (files) ────┘ push after each turn
assistant: /kb (files only), every file tool call path-checked
```

## Turn it on

```bash
sudo TELEGRAM_OWNER_ID=123456789 MEMORY=1 bash deploy/setup.sh
```

This makes the hub, installs its checks and writes two files that are yours from then on:

- `/srv/retinue/memory/policy.json` — what she may write (`writable`), the showcase files she may change only below
  `marker` (`lines_below`), what is never hers (`never`), what never enters the base whoever pushes (`excluded`,
  e.g. a scratch folder that stays on your computer), the header field that names a record and never changes while
  it lives (`keep`), and your base's own lint (`check`, run in a copy of the new tree). The example assumes Spaces
  with `knowledge/`, `profile/`, `journal/`, `artifacts/` folders; list your own. After editing it, run
  `setup.sh` again: the hub gets a copy.
- `/srv/retinue/memory/checkout.txt` — what of the base her folder holds (git sparse-checkout patterns). Leave
  scripts and hidden folders out: what is not there she cannot read.

Then push your repository into the empty hub from your computer (you are in the hub's group after a new login):

```bash
git remote add hub owner@10.8.0.1:/srv/retinue/memory/hub.git
git push hub main
git branch -u hub/main
```

and restart the stack: the router makes its working copy from the hub at start.

## What the hub checks

Every push, yours and the router's, with the same `kbcheck.py`:

- only `main`, never deleted, never rewritten (`pull` first);
- no symlinks or submodules;
- nothing from `excluded`;
- no two names that are one file on a Mac (`A.md` and `a.md`, `é` composed and decomposed);
- a record keeps its `id` while it lives: deleting it, or making a new one, is fine; renaming it in place is not;
- your base's lint passes on the new tree, run in a fresh private folder with Python told to keep the tree off its
  import path;
- for the router's push (the containers' user, uid 10001) only: the paths of `policy.json` (case and Unicode
  form do not matter, as on a Mac) and no name starting with a dot anywhere.

Who may write what in the hub: `objects/` and `refs/` belong to you and the group of uid 10001 (that is all a
push writes; gc after a push is off). The hub's folder and `config` are yours and not the group's; `hooks/` is
root's. So the router can push, but cannot change what checks a push. It can still write objects and refs
directly, past the hook — the router is trusted code; the model never sees the hub.

## Every day

At 21:00 (`digest_at` in the `memory:` section of `router.yaml`) a message lists her commits of the day: the files,
your own records she changed or deleted first, and whether someone else's text was on her input (a forward, an
attachment, travel results). Each commit has an «Откатить» button for a day; later, `git revert <commit>` on your
computer. What a refused turn wrote is kept in `/srv/retinue/memory/git/refused/`.
