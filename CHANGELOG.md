# What's new

Each release's changes in plain words, for the person who runs Retinue: what they will notice, not how it was built.
After an update the app shows them once, under «What's new» (`retinue/changelog.py`).

A pull request that changes what the owner sees adds its line under «Unreleased». A release renames that heading to
its version and date — `## v0.2.0 — 2026-11-20` — and opens a new, empty «Unreleased» above it (CONTRIBUTING.md,
«Releases»).

## Unreleased

- A server that follows develop sets up each version on its own, `setup.sh` included: nothing is left to run by hand
  after an update, and a version whose set-up fails is not started.
- «Operator» is a place of its own, beside «Today»: the sessions' questions and results as cards, or on the map. The
  sessions on your own computer are there too, with what they asked or brought back; you answer them on the computer.
- Every project's passport has the same short parts — why, scope, files, rules, checks — written for the agent that
  opens the project, and the Base refuses a change that breaks one.
- The left column is the Base's tree: its Spaces on top, every sub-project inside, folded until you open it. No
  state filter and no numbers: what waits is the amber dot; «Archived» is the last row. The Spaces page shows each
  Space by its mark and name.
- «Pages» says why it is empty: the pages service is not set up, has not sent its list, is late, or has no page
  published yet — each with the next step.
- Tasks are grouped by project now, in the same order as the projects on the left, with «No project» last; a
  project's group shows its first five and «Show all» the rest. A task's «⋯» moves it to another project, and «＋» on
  a group adds a task right into it. The other grouping, «Days», is the old «By when»; «By goal» is gone.
- The sessions you run on your own computer show in the list too, under their projects, with whether they are at work
  or waiting for you.
- «Settings» in the app, behind the gear: your Claude subscription, ElevenLabs, the Telegram bots, the repositories'
  tokens, the projects' keys and the sessions' model and effort, each set or replaced there and never shown again,
  with one «Restart» to apply. Run `deploy/setup.sh` once more on the server to install its service.
- Your own computer is no longer called «the Mac»: the setup is written for macOS, Windows and Linux alike, and a
  computer set up before keeps working as it is.
- After an update, the app shows once what changed in it.
