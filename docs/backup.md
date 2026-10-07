# Backup

The archive holds real conversations from the first day, so the backup comes with the install. It is
[restic](https://restic.net) to any S3-compatible storage at another provider, run by systemd on the host — not
by a container in the stack: a broken deploy must not stop it, and its keys stay out of Dokploy. Written for
MEGA S4.

What goes in, every night at 03:30 Moscow time:

- every `*.sqlite` of the `router-data` and `assistant-data` volumes, copied with SQLite's backup API (a plain
  copy taken during a write is torn) and checked with `quick_check`;
- the rest of both volumes (the assistant's session transcripts) and `/srv/retinue` (configs).

What never leaves the host: the raw database files (their copies are in), Claude Code's `.credentials.json`, and
every `*.env` file under `/srv/retinue` — `stack.env` holds your subscription token and bot token, `backup.env`
the backup's own keys — and the Matrix appservice registration, which carries generated tokens. After a restore
`setup.sh` writes new generated secrets, and you fill in the rest again.

The result of every run, good or bad, goes to `/srv/retinue/status/backup.json`. The router reads it through a
read-only mount, and the health line of the morning summary says `бэкап — 5 ч назад`, or `бэкапа нет 2 сут:
<error>` when the last good snapshot is older than 26 hours. No summary at all means the server is down.

## Two keys

The server could be taken over, so its key must not be able to destroy the history. S3 storage without object
lock cannot be made append-only, but the server's key can be denied deletes:

| Key | Where | May |
|---|---|---|
| server | `/srv/retinue/backup.env`, root only | list, read, write; delete only `locks/*` (restic cannot run without removing its lock) |
| your computer | `~/.config/retinue/backup.env` | everything: `forget`, `prune`, `check` |

It can still overwrite what it wrote. That is caught by the monthly `check --read-data`, and survived by the
copy in a local repository on your computer.

The bucket policy for the server's key, with your bucket and the key's user ARN:

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
```

```bash
bash deploy/backup/mac.sh monthly
```

It stops at the first failure, so nothing is deleted after a bad check or restore:

1. `restic check --read-data` — every byte read back;
2. `restic restore latest` into a temporary folder; every database must pass `integrity_check` and the archive
   must have events from the last two days; the folder is removed;
3. `restic copy` into the local repository (created on first use with the same chunker parameters and your
   password);
4. `restic forget --keep-within-daily 7d --keep-within-weekly 1m --keep-within-monthly 6m --prune` — time-based,
   so that fake snapshots made by a compromised server cannot push the real ones out;
5. `restic check` of the local repository.

## Restore onto a new server

```bash
sudo TELEGRAM_OWNER_ID=123456789 BACKUP=1 bash /opt/retinue/deploy/setup.sh   # fill backup.env, stack.env
sudo bash -c 'set -a; . /srv/retinue/backup.env; restic restore latest --host retinue --tag retinue --target /var/restore'
cd /opt/retinue && sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml create
V=/var/lib/docker/volumes S=/var/restore/var/backups/retinue/staging
for vol in retinue_router-data retinue_assistant-data; do
  sudo cp -a /var/restore$V/$vol/_data/. $V/$vol/_data/     # everything but the databases
  sudo cp -a $S/$vol/. $V/$vol/_data/                       # the consistent copies of the databases
  sudo chown -R 10001:10001 $V/$vol/_data
done
sudo cp /var/restore/srv/retinue/egress/allowed-hosts.txt /srv/retinue/egress/
sudo docker compose -p retinue --env-file /srv/retinue/stack.env -f deploy/compose.yml up -d
```

Then write to the bot and ask about something from before the restore.
