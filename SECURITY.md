# Security

Retinue holds one person's private data and runs an AI agent with a way out to the web, so a hole in it matters.
Reports are welcome and taken seriously.

## How to report

**Privately, through GitHub:** the repository's **Security** tab → **Report a vulnerability**. Please do not open a
public issue or pull request for a vulnerability, and do not post details anywhere else until it is fixed.

If the button is missing, open an issue titled «Security contact» with no details, and a private channel will be
arranged.

Include what you can: the part (the gate, a guard, an egress list, the face API, the deployment), the version
(a commit hash), the steps or a proof of concept, and what an attacker gets. It is a one-person project: expect an answer, not an SLA.
There is no bug bounty.

## What counts

Anything that lets data leave the server, or lets someone other than the owner act as the owner. For example:

- a way past the gate, the exfil guard or the SNI guard from inside a session;
- a way out of the assistant's container other than the model API;
- a way to reach the face API or the owner's pages host from outside the owner's devices, a page reading another
  page's values or the app's, or the public pages reaching anything but what was shared;
- a token, a key or the owner's data reaching a lane, a log or a message where the docs say it does not.

Known, documented limits are not new findings, though a better fix for one is welcome: see
[Limits and known gaps](README.md#limits-and-known-gaps) and [The room](docs/platform.md#the-room).

## Supported versions

Only `main`. There are no releases.
