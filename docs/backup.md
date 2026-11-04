# Backup

The archive holds your conversations, so the backup comes with the install. It is
[restic](https://restic.net) to any S3-compatible storage at another provider, run by systemd on the host — not
by a container in the stack: a broken deploy must not stop it, and its keys stay out of Dokploy. Any
S3-compatible storage works.

What goes in, every night at 03:30 in your zone (`OWNER_TZ`, UTC unless you gave one), in two restic passes:

1. `files` — with everything running: `/srv/retinue` (configs, the Base's hub) and the stack's volumes —
   `router-data`, `assistant-data`, and `travel-data`, `platform-data`, `pages-data` with those on. It uploads the
   bulk.
2. `data` — the stack's containers paused (`docker pause`) for the seconds it takes to read their volumes again
   whole (`--force`), then unpaused. A paused container's files lie on disk as if its process had been killed,
   which SQLite survives: every database in the snapshot opens whole, its journal rolled back. `--force` because
   restic skips a file whose size and times did not change, and a page rewritten within one tick of the file
   system's clock looks unchanged. With the platform on, the files you uploaded go in with the same pause: it
   writes a file before its row, so every row in the snapshot names a file that is in it
   ([collections.md](collections.md)).

The pause is at most 60 s (`RETINUE_PAUSE_MAX_S`), under the healthchecks' window: past it the containers are
unpaused first and restic killed, and the night is a failure. Whatever ends a run — a kill, a crash — the unit's
`ExecStopPost` unpauses what it paused, and only that.

What is not backed up with the stack's scope: the lanes' volumes. The room's `/work` holds your repositories' clones
and any session files outside the Base; a commit a session made there that you have not pushed exists only on the
server. The host's scope (below) takes them.

What never leaves the host with the stack's scope: Claude Code's `.credentials.json`, and every `*.env` file under
`/srv/retinue` — `stack.env` holds your subscription token and bot token, `backup.env` the backup's own keys — and
the Matrix appservice registration, which carries generated tokens. After a restore `setup.sh` writes new generated
secrets, and you fill in the rest again.

## The whole server

`RETINUE_BACKUP_SCOPE=host` in `backup.env` backs up the whole server the stack runs on, so that a dead server — or
a move to another provider — is a restore and not a rebuild. Anything you add later is in the next night's snapshot
without a word: a new app, its volume, a folder it bind-mounts, its database of any kind.

- `files` takes `/` but for what rebuilds itself or is not a file: `/proc`, `/sys`, `/dev`, `/run`, `/tmp`, logs,
  caches, snaps, Docker's images and layers (its volumes stay in). Network and FUSE mounts are left out as found.
  Add your own patterns in `RETINUE_BACKUP_EXCLUDE`.
- `data` pauses every running container but those that hold no state and serve others: by name in
  `RETINUE_BACKUP_NO_PAUSE` (default `dokploy-traefik`, the proxy in front of every site) or by the label
  `retinue.backup.pause=false` (a resolver for the VPN, say). It reads again every volume and every folder a running
  container bind-mounts.
- **The secrets go in.** A move is not simple without the host's ssh keys, the apps' variables and the VPN's keys.
  restic encrypts on the server before anything leaves it; whoever has the repository's password and the bucket's
  key has them all. Keep both where the server is not: a password manager and paper.
- Before the passes, into the snapshot: an inventory (`inventory.json`: OS, Docker and restic versions, every
  container's image by digest and its mounts, the Postgres major it runs, the Swarm secrets' names, the users and
  their ids — never a container's environment); and, when the server runs Dokploy, its database (`pg_dump -Fc`, as
  its own backup makes it) and the secret its apps' variables are encrypted with. Without that secret a new server
  shows every app's variables as ciphertext. A step that fails is a warning in the morning, not a lost night.

Every database restores as its volume, paused: Postgres on the same major image (the inventory has it), SQLite and
MySQL alike. Nothing dumps them.

The result of every run, good or bad, goes to `/srv/retinue/status/backup.json`. The router reads it through a
read-only mount, and the health line of the morning brief says `backup — 5 h ago`, or `no backup for 2 d:
<error>` when the last good snapshot is older than 26 hours; a night that uploaded more than 2 GB says so. No summary at all means the server is down. The
containers' health comes the same way, from `status/health.json` ([health.md](health.md)).

## Two keys

The server could be taken over, so its key must not be able to destroy the history. S3 storage without object
lock cannot be made append-only, but the server's key can be denied deletes:

| Key | Where | May |
|---|---|---|
| server | `/srv/retinue/backup.env`, root only | list, read, write; delete only `locks/*` (restic cannot run without removing its lock) |
| your computer | `~/.config/retinue/backup.env` | everything: `forget`, `prune`, `check` |

It can still overwrite what it wrote. That is caught by the monthly `check --read-data`, and survived by the
copy in a local repository on your computer.

A key with no delete at all works too: restic backs up and warns that it could not remove its lock. Each night
then leaves one lock behind; `mac.sh monthly` removes the stale ones before `forget`. To let the server remove
its own locks instead, give its key's user this bucket policy, with your bucket and the user's ARN:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Principal": {"AWS": "<server user ARN>"}, "Action": ["s3:ListBucket"],
     "Resource": ["arn:aws:s3:::<bucket>"]},
    {"Effect": "Allow", "Principal": {"AWS": "<server user ARN>"}, "Action": ["s3:GetObject", "s3:PutObject"],
     "Resource": ["arn:aws:s3:::<bucket>/*"]},
    {"Effect": "Allow", "Principal": {"AWS": "<server user ARN>"}, "Action": ["s3:DeleteObject"],
     "Resource": ["arn:aws:s3:::<bucket>/retinue/locks/*"]}
  ]
}
```

restic passwords are separate from S3 keys: one repository, two restic keys (`restic key add`), one for the
server and one for your computer. If the server is lost, `restic key remove` its key from your computer. Keep
both passwords, your computer's S3 key, the bucket name and the repository URL outside the server — a password
manager that does not run there, and paper. Without them the backup is noise.

## Turn it on

```bash
sudo TELEGRAM_OWNER_ID=123456789 BACKUP=1 bash /opt/retinue/deploy/setup.sh
```

The script creates `/srv/retinue/backup.env` (mode 600) with names to fill in and prints them, never values.
Once `backup.env` exists, every later run keeps the backup on and refreshes the script and the units, with or
without `BACKUP=1`. It installs restic 0.19.1 from the official release, checked against a pinned SHA-256, into
`/usr/local/bin/restic`; the script into `/usr/local/lib/retinue/`; `retinue-backup.service` and
`retinue-backup.timer` into `/etc/systemd/system/`. The timer is enabled when every name in `backup.env` is
filled in:

```
RESTIC_REPOSITORY=s3:https://<endpoint>/<bucket>/retinue
RESTIC_PASSWORD=<the server's restic key password>
AWS_ACCESS_KEY_ID=<the server's S3 key>
AWS_SECRET_ACCESS_KEY=<its secret>
AWS_DEFAULT_REGION=<the endpoint's region>
```

Create the repository from your computer (below), so that its password is never typed on the server, and give
the server its own key; then fill in `backup.env`, run the script again and start the first backup:

```bash
restic init                  # on your computer, with ~/.config/retinue/backup.env loaded
restic key add               # the new password is the server's: it goes into /srv/retinue/backup.env
sudo TELEGRAM_OWNER_ID=123456789 bash /opt/retinue/deploy/setup.sh     # "Backup: on"
sudo systemctl start retinue-backup && sudo cat /srv/retinue/status/backup.json
```

## Your computer: once a month

Install restic (`brew install restic`), then write `~/.config/retinue/backup.env` (mode 600) with your
computer's S3 key, its restic password (or `RESTIC_PASSWORD_COMMAND`, e.g. reading the macOS keychain) and a
folder for the local copy:

```
RESTIC_REPOSITORY=s3:https://<endpoint>/<bucket>/retinue
RESTIC_PASSWORD=<your computer's restic key password>
AWS_ACCESS_KEY_ID=<your computer's S3 key>
AWS_SECRET_ACCESS_KEY=<its secret>
AWS_DEFAULT_REGION=<the endpoint's region>
RETINUE_LOCAL_REPO=/Users/<you>/Backups/retinue-restic
RETINUE_PLATFORM=1          # with the platform on: a snapshot without its databases then fails the restore
RETINUE_SSH_HOST=<your server's ssh host>   # where a run that went through reports itself (status/restore.json)
```

```bash
bash deploy/backup/mac.sh monthly
```

It stops at the first failure, so nothing is deleted after a bad check or restore. First `restic unlock` takes off
the stale locks the server's nightly runs leave (its key may not delete them), which would stop the check. Then it
picks the newest snapshot and refuses to go on if any snapshot is dated more than an hour ahead of the computer's
clock: the server writes the time it likes, and `--keep-within*` counts back from the newest snapshot, so one forged
snapshot dated in the future would put every real one out of range and `forget --prune` would delete them all. Look
at such a snapshot (`restic snapshots`) and remove it by hand. Then:

1. `restic check --read-data` — every byte read back;
2. `restic restore <that snapshot>` into a temporary folder — the databases with their journals, the uploaded files and
   the staging folder, not the server's secrets; every database must pass `integrity_check` and the archive
   must have events from the last two days; with the platform on (`RETINUE_PLATFORM=1`, or `platform.sqlite` is in
   it), `platform.sqlite` and `collections.sqlite` must be there too and every file its rows name must be in the
   platform's volume at its size; the folder is removed;
3. `restic copy` into the local repository (created on first use with the same chunker parameters and your
   password);
4. `restic forget --group-by host,tags --keep-within-daily 7d --keep-within-weekly 1m --keep-within-monthly 6m --prune` — time-based,
   so that many fake snapshots made at once cannot push the real ones out by count — and the same in the local
   repository, monthly ones kept for 24 months;
5. `restic check` of the local repository;
6. the snapshot it proved is kept beside `backup.env` (`verified-snapshot`): a restore onto a new server starts from
   it rather than from `latest`, which a compromised server could have written;
7. the server is told when and which (`/srv/retinue/status/restore.json`, over ssh to `RETINUE_SSH_HOST`). The morning's
   health line says «restore never checked» until the first report, and «restore not checked for N d» once the last
   one is 35 days old; otherwise it says nothing of it. A run that failed reports nothing.

## Restore onto a new server

A night's two snapshots go back together: `files` first, then `data` over it. Take the `data` snapshot your computer
proved last (`verified-snapshot`) and the `files` one of the same night (`restic snapshots`).

```bash
sudo TELEGRAM_OWNER_ID=123456789 BACKUP=1 bash /opt/retinue/deploy/setup.sh   # fill backup.env, stack.env
sudo bash -c 'set -a; . /srv/retinue/backup.env
  restic restore <files snapshot> --target /var/restore && restic restore <data snapshot> --target /var/restore'
cd /opt/retinue && sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml create
V=/var/lib/docker/volumes
# With travel, the platform or pages on (TRAVEL=1, PLATFORM=1, PAGES=1 on the setup.sh line too), add their volumes.
for vol in retinue_router-data retinue_assistant-data; do
  sudo rm -rf $V/$vol/_data && sudo cp -a /var/restore$V/$vol/_data $V/$vol/   # owners and modes as they were
done
sudo cp /var/restore/srv/retinue/egress/allowed-hosts.txt /srv/retinue/egress/
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d
```

With the host's scope, the whole server comes back the same way, in this order:

1. Firewall at the new provider (ssh, 80, 443, the VPN's UDP port); the old server's backup timer off, or two hosts
   write under one name.
2. Docker of the inventory's version; with dockerd stopped, `/var/lib/docker/volumes` from the snapshots, whole and
   into an empty folder (a volume an app already initialised breaks a restored Postgres) — all but Dokploy's
   database volume.
3. `/etc/dokploy`, then Dokploy's installer at the inventory's version (`DOKPLOY_VERSION=…`); stop the `dokploy`
   service; bring back the old encryption secret (`docker secret create` from `staging/dokploy/auth_secret`,
   `docker service update --secret-rm/--secret-add` under the same target name); `pg_restore --clean --if-exists
   -d dokploy` the dump inside `dokploy-postgres`; start it, and check one app's variables read as text.
4. `/srv`, `/opt`, `/root`, `/home`; from `/etc` only what is yours — the ssh host keys, your units, `sudoers.d`,
   `docker/daemon.json`, `sysctl.d`, `cron.d`, the firewall's rules. Never all of it: it carries the old provider's
   network, disks and machine id. Users with the inventory's ids.
5. `docker compose up -d` for the stacks under `/opt` and `/srv`; DNS to the new address (lower its TTL a day
   ahead); certificates renew once it points there.

Rehearse it once on a throwaway server before you need it, and again after a major update of Docker or Dokploy.

Then write to the bot and ask about something from before the restore.

With the platform on, a restore brings back the devices as the snapshot had them: a Mac revoked after it has a
working token again. Run `retinue-platform collections device list` in the platform's container and revoke every
device the owner had taken back since ([collections.md](collections.md#setting-up-a-mac)).
