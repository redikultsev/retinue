"""The host's nightly backup: consistent SQLite copies, restic with the secrets left out, a status file for the router.

restic is replaced by a fake that records its arguments: no repository, no network, no root.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "backup" / "retinue-backup.py"
FAKE_RESTIC = """#!{python}
import json, os, sys
open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
open(os.environ["FAKE_LOG"] + ".cache", "w").write(os.environ.get("RESTIC_CACHE_DIR", ""))
args = sys.argv[1:]
if "--exclude-file" in args:
    open(os.environ["FAKE_LOG"] + ".exclude", "w").write(open(args[args.index("--exclude-file") + 1]).read())
code = int(os.environ.get("FAKE_EXIT", "0"))
if code in (0, 3):
    print(json.dumps({{"message_type": "status", "percent_done": 1}}))
    print(json.dumps({{"message_type": "summary", "snapshot_id": "4f9c2a1b" * 8, "data_added": 2048,
                      "total_bytes_processed": 700000}}))
if code:
    print(os.environ.get("FAKE_ERROR", "Fatal: unable to open config file"), file=sys.stderr)
sys.exit(code)
"""


def host(tmp_path):
    """A host in a folder: two volumes with live databases, an install folder with secrets, a fake restic."""
    volumes = tmp_path / "volumes"
    router = volumes / "retinue_router-data" / "_data"
    assistant = volumes / "retinue_assistant-data" / "_data"
    (assistant / "claude").mkdir(parents=True)
    router.mkdir(parents=True)
    for path in (router / "router.sqlite", router / "archive.sqlite", assistant / "agent.sqlite"):
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE t (x TEXT)")
        db.execute("INSERT INTO t VALUES ('committed')")
        db.commit()
        db.close()
    (assistant / "claude" / ".credentials.json").write_text("{}")
    srv = tmp_path / "srv"
    srv.mkdir()
    for name in ("stack.env", "secrets.env", "backup.env", "stack.env.bak-20261006"):
        (srv / name).write_text("X=1\n")
    (srv / "router.yaml").write_text("owner: owner\n")
    restic = tmp_path / "restic"
    restic.write_text(FAKE_RESTIC.format(python=sys.executable))
    restic.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "RETINUE_ROOT": str(srv), "RETINUE_VOLUME_ROOT": str(volumes),
           "RETINUE_BACKUP_STAGING": str(tmp_path / "backups" / "staging"), "RESTIC": str(restic),
           "FAKE_LOG": str(tmp_path / "restic.log")}
    return srv, router, env


def run(env, **extra):
    return subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env={**env, **extra})


def test_a_night_backup_copies_databases_consistently_and_leaves_secrets_out(tmp_path):
    srv, router, env = host(tmp_path)
    writer = sqlite3.connect(router / "router.sqlite")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES ('not yet')")  # the router is in the middle of a write
    done = run(env)
    writer.rollback()
    assert done.returncode == 0, done.stderr
    staging = tmp_path / "backups" / "staging"
    copies = sorted(str(p.relative_to(staging)) for p in staging.rglob("*.sqlite"))
    assert copies == ["retinue_assistant-data/agent.sqlite", "retinue_router-data/archive.sqlite",
                      "retinue_router-data/router.sqlite"]
    copy = sqlite3.connect(staging / "retinue_router-data" / "router.sqlite")
    assert copy.execute("SELECT x FROM t").fetchall() == [("committed",)], "the committed state, not a torn file"
    assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert oct(staging.parent.stat().st_mode & 0o777) == "0o700", "the copies hold the archive: root only"

    (call,) = [json.loads(line) for line in (tmp_path / "restic.log").read_text().splitlines()]
    assert call[0] == "backup" and "--json" in call and call[call.index("--tag") + 1] == "retinue"
    assert call[call.index("--host") + 1] == "retinue" and call[call.index("--retry-lock") + 1] == "30m"
    assert call[-4:] == [str(staging), str(router), str(tmp_path / "volumes" / "retinue_assistant-data" / "_data"),
                         str(srv)], "the copies, both volumes, the install folder"
    excluded = (tmp_path / "restic.log.exclude").read_text().splitlines()
    assert f"{router}/router.sqlite" in excluded and f"{router}/router.sqlite-journal" in excluded, \
        "raw databases stay out: their consistent copies are in staging"
    assert not any(line.startswith(str(staging)) for line in excluded)
    assert ".credentials.json" in excluded, "the Claude login never leaves the host"
    assert f"{srv}/*.env" in excluded and f"{srv}/*.env.*" in excluded, \
        "stack.env holds the subscription token; backup.env the backup's own keys"
    assert f"{srv}/mail" in excluded, "the owner's Google tokens never leave the host"
    assert f"{srv}/send" in excluded, "nor his gmail.send tokens"
    assert f"{srv}/tuwunel/appservices" in excluded, "the Matrix registration carries tokens too"
    assert f"{srv}/pages/traefik.yml" in excluded, "the pages' key, rendered from secrets.env; setup.sh renders it again"
    assert f"{srv}/pages/site" in excluded and f"{srv}/pages/public" in excluded, \
        "the built pages and the shared copies: made again from the data and the store, which are kept"
    assert (tmp_path / "restic.log.cache").read_text() == "/var/cache/restic", \
        "systemd gives the service no HOME, and restic finds no cache without one"

    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["ok"] is True and status["snapshot"] == "4f9c2a1b" * 8
    assert status["data_added"] == 2048 and status["size"] == 700000 and status["finished"] > 0
    assert oct((srv / "status" / "backup.json").stat().st_mode & 0o777) == "0o644", "the router reads it"


def test_a_failed_night_is_on_record_with_the_error_and_the_last_good_one(tmp_path):
    srv, _, env = host(tmp_path)
    assert run(env).returncode == 0
    good = json.loads((srv / "status" / "backup.json").read_text())["finished"]
    failed = run(env, FAKE_EXIT="1", FAKE_ERROR="Fatal: unable to open config file: Stat: Access Denied.")
    assert failed.returncode == 1
    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["ok"] is False and status["exit"] == 1 and status["last_ok"] == good
    assert status["error"] == "Fatal: unable to open config file: Stat: Access Denied."
    run(env, FAKE_EXIT="1", FAKE_ERROR=json.dumps({"message_type": "exit_error", "code": 1,
                                                   "message": "unable to locate cache directory"}))
    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["error"] == "unable to locate cache directory", "restic 0.19 writes its fatal error as JSON"
    assert json.loads((srv / "status" / "backup.json").read_text())["last_ok"] == good, "kept across failed nights"

    partial = run(env, FAKE_EXIT="3", FAKE_ERROR="Warning: at least one source file could not be read")
    status = json.loads((srv / "status" / "backup.json").read_text())
    assert partial.returncode == 0 and status["ok"] is True and status["warning"].startswith("Warning: at least one")

    env["RETINUE_VOLUMES"] = "retinue_router-data retinue_gone-data"
    run(env)
    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["ok"] is False and "retinue_gone-data" in status["error"], "a missing volume is a failure, not a skip"


def test_the_platforms_collections_and_uploaded_files_go_in_with_its_volume(tmp_path):
    """collections.sqlite is copied whole while the platform writes (the copy has what was committed), and the
    uploaded files beside it are in the volume restic reads after — never excluded."""
    srv, _, env = host(tmp_path)
    platform = tmp_path / "volumes" / "retinue_platform-data" / "_data"
    (platform / "attachments").mkdir(parents=True)
    sha = "ab" * 32
    (platform / "attachments" / sha).write_bytes(b"%PDF-1.7 statement")
    db = sqlite3.connect(platform / "collections.sqlite")
    db.execute("CREATE TABLE files (sha256 TEXT PRIMARY KEY, name TEXT, size INTEGER, mime TEXT, created REAL)")
    db.execute("INSERT INTO files VALUES (?, 'statement.pdf', 18, 'application/pdf', 1)", (sha,))
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO files VALUES (?, 'later.pdf', 1, 'application/pdf', 2)", ("cd" * 32,))  # not committed
    done = run(env, RETINUE_VOLUMES="retinue_router-data retinue_assistant-data retinue_platform-data")
    db.rollback()
    assert done.returncode == 0, done.stderr
    staged = tmp_path / "backups" / "staging" / "retinue_platform-data" / "collections.sqlite"
    assert sqlite3.connect(staged).execute("SELECT sha256 FROM files").fetchall() == [(sha,)]
    (call,) = [json.loads(line) for line in (tmp_path / "restic.log").read_text().splitlines()]
    assert str(platform) in call, "the volume, the uploaded files with it"
    excluded = (tmp_path / "restic.log.exclude").read_text().splitlines()
    assert f"{platform}/collections.sqlite" in excluded, "the raw file stays out: its copy is in staging"
    assert [line for line in excluded if "attachments" in line] == ["**/attachments/.*.part"], \
        "only an upload still being written stays out"


MAC = ROOT / "deploy" / "backup" / "mac.sh"
FAKE_MAC_RESTIC = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
[[ $1 != version ]] || { echo "${FAKE_VERSION:-restic 0.19.1 compiled with go1.26 on darwin/arm64}"; exit 0; }
[[ $1 != snapshots ]] || { echo "$FAKE_SNAPSHOTS"; exit 0; }
[[ $1 != -r || $3 != init ]] || { mkdir -p "$2" && touch "$2/config"; }
if [[ $1 == restore ]]; then
  target=${@: -1}
  mkdir -p "$target/var/backups/retinue" && cp -R "$FAKE_RESTORE" "$target/var/backups/retinue/staging"
  [[ -z ${FAKE_VOLUMES:-} ]] || { mkdir -p "$target/var/lib/docker" && cp -R "$FAKE_VOLUMES" "$target/var/lib/docker/volumes"; }
fi
"""


FAKE_SSH = """#!/bin/bash
echo "$* <<< $(cat)" >> "$FAKE_SSH_LOG"
"""


def mac(tmp_path, archive_bytes=None, platform=None, env="", **fake):
    """The owner's Mac: an env file with the deleting key, a fake restic whose `restore` lays out last night's copies.
    `platform`: the platform's volume too — `{"collections": bool, "rows": [sha…], "blobs": {sha: bytes}}`, and
    `"other"`: blobs in another volume's `attachments/`. `env`: more lines for backup.env."""
    snapshot = tmp_path / "snapshot"
    (snapshot / "retinue_router-data").mkdir(parents=True)
    (snapshot / "retinue_assistant-data").mkdir()
    for name in ("retinue_router-data/router.sqlite", "retinue_assistant-data/agent.sqlite"):
        sqlite3.connect(snapshot / name).execute("CREATE TABLE t (x)").connection.close()
    volumes = {}
    if platform is not None:
        (snapshot / "retinue_platform-data").mkdir()
        sqlite3.connect(snapshot / "retinue_platform-data" / "platform.sqlite").execute("CREATE TABLE t (x)").connection.close()
        if platform["collections"]:
            db = sqlite3.connect(snapshot / "retinue_platform-data" / "collections.sqlite")
            db.execute("CREATE TABLE files (sha256 TEXT PRIMARY KEY, size INTEGER)")
            db.executemany("INSERT INTO files VALUES (?, 1)", [(sha,) for sha in platform["rows"]])
            db.commit()
            db.close()
        blobs = tmp_path / "volumes" / "retinue_platform-data" / "_data" / "attachments"
        blobs.mkdir(parents=True)
        for sha, data in platform["blobs"].items():
            (blobs / sha).write_bytes(data)
        other = tmp_path / "volumes" / "retinue_travel-data" / "_data" / "attachments"
        other.mkdir(parents=True)
        for sha in platform.get("other", []):
            (other / sha).write_bytes(b"x")
        volumes = {"FAKE_VOLUMES": str(tmp_path / "volumes")}
    archive = snapshot / "retinue_router-data" / "archive.sqlite"
    if archive_bytes is None:
        db = sqlite3.connect(archive)
        db.execute("CREATE TABLE events (ts REAL)")
        db.execute("INSERT INTO events VALUES (strftime('%s','now') - 3600)")
        db.commit()
        db.close()
    else:
        archive.write_bytes(archive_bytes)
    env_file = tmp_path / "backup.env"
    env_file.write_text("RESTIC_REPOSITORY=s3:https://s3.example/bucket/retinue\nRESTIC_PASSWORD=p\n"
                        "AWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\nAWS_DEFAULT_REGION=r\n"
                        f"RETINUE_LOCAL_REPO={tmp_path / 'local-repo'}\n{env}")
    restic = tmp_path / "restic"
    restic.write_text(FAKE_MAC_RESTIC)
    restic.chmod(0o755)
    ssh = tmp_path / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    run = subprocess.run(["bash", str(MAC), "monthly"], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                              "RETINUE_BACKUP_ENV": str(env_file), "RESTIC": str(restic), "PYTHON": sys.executable,
                              "SSH": str(ssh), "FAKE_SSH_LOG": str(tmp_path / "ssh.log"),
                              "FAKE_LOG": str(tmp_path / "restic.log"), "FAKE_RESTORE": str(snapshot), **volumes,
                              "FAKE_SNAPSHOTS": json.dumps([{"id": "a1b2c3d4e5f6", "short_id": "a1b2c3d4",
                                                             "time": "2026-01-01T03:30:01.123456789+01:00"}]),
                              **fake})
    calls = (tmp_path / "restic.log").read_text().splitlines() if (tmp_path / "restic.log").exists() else []
    steps = [call.split()[2] if call.startswith("-r ") else call.split()[0] for call in calls]  # -r <local repo> <command>
    return run, steps, calls


def test_monthly_care_on_the_mac_checks_restores_copies_and_only_then_prunes(tmp_path):
    run, steps, calls = mac(tmp_path)
    assert run.returncode == 0, run.stderr
    assert steps[0] == "unlock", "the night's stale lock would stop the check: it goes first"
    assert calls[1:3] == ["version", "snapshots --json --host retinue --tag retinue"], "the snapshot to prove is chosen first"
    calls, steps = calls[3:], steps[3:]
    assert steps == ["check", "restore", "init", "copy", "unlock", "forget", "forget", "check"], calls
    assert calls[0] == "check --read-data", "every byte read back: the server's key could overwrite what it wrote"
    assert calls[1].startswith("restore a1b2c3d4e5f6 --target "), "the snapshot chosen, not `latest`"
    assert calls[2] == f"-r {tmp_path / 'local-repo'} init --from-repo s3:https://s3.example/bucket/retinue " \
                       "--copy-chunker-params", "the copy on the Mac is made once, deduplicating like the original"
    assert calls[3] == f"-r {tmp_path / 'local-repo'} copy --from-repo s3:https://s3.example/bucket/retinue " \
                       "--host retinue --tag retinue"
    assert calls[4] == "unlock", "the server's key cannot delete its own locks; forget needs them gone"
    assert calls[5] == ("forget --host retinue --tag retinue --keep-within-daily 7d --keep-within-weekly 1m "
                        "--keep-within-monthly 6m --prune")
    assert calls[6] == (f"-r {tmp_path / 'local-repo'} forget --host retinue --tag retinue --keep-within-daily 7d "
                        "--keep-within-weekly 1m --keep-within-monthly 24m --prune"), "the copy here is kept too, longer"
    assert calls[7] == f"-r {tmp_path / 'local-repo'} check"
    assert (tmp_path / "verified-snapshot").read_text() == "a1b2c3d4e5f6\n", "a restore elsewhere starts from it"
    assert "integrity ok: agent.sqlite, archive.sqlite, router.sqlite" in run.stdout
    assert not list(tmp_path.glob("retinue-restore.*")), "the restored copy is removed"
    assert not (tmp_path / "ssh.log").exists() and "set RETINUE_SSH_HOST" in run.stderr, "no host named: said, not failed"


def test_a_monthly_run_that_went_through_tells_the_server_and_a_broken_one_does_not(tmp_path):
    run, _, _ = mac(tmp_path, env="RETINUE_SSH_HOST=server.example\n")
    assert run.returncode == 0, run.stderr
    [told] = (tmp_path / "ssh.log").read_text().splitlines()
    assert told.startswith("server.example sudo tee /srv/retinue/status/restore.json >/dev/null <<< {\"checked\": ")
    assert told.endswith(', "snapshot": "a1b2c3d4e5f6"}'), "the server's summary names the proven snapshot"
    broken = tmp_path / "broken"
    broken.mkdir()
    run, _, _ = mac(broken, archive_bytes=b"not a database", env="RETINUE_SSH_HOST=server.example\n")
    assert run.returncode != 0 and not (broken / "ssh.log").exists(), "a failed check tells the server nothing"


def test_a_broken_restore_stops_before_anything_is_deleted(tmp_path):
    run, steps, _ = mac(tmp_path, archive_bytes=b"not a database")
    assert run.returncode != 0 and steps[3:] == ["check", "restore"], "no copy, no prune after a failed restore"
    assert "archive.sqlite" in run.stderr
    assert not list(tmp_path.glob("retinue-restore.*")), "nor a copy of the archive left in the temporary folder"


def test_with_the_platform_a_snapshot_without_its_collections_or_their_files_fails_the_restore(tmp_path):
    sha = "ab" * 32
    run, steps, _ = mac(tmp_path / "a", platform={"collections": False, "rows": [], "blobs": {}})
    assert run.returncode != 0 and steps[3:] == ["check", "restore"] and "collections.sqlite" in run.stderr, \
        "the platform makes the file at its start: a snapshot without it lost it"
    run, steps, _ = mac(tmp_path / "b", platform={"collections": True, "rows": [sha], "blobs": {}, "other": [sha]})
    assert run.returncode != 0 and steps[3:] == ["check", "restore"] and "1 uploaded files" in run.stderr, \
        "a row whose file is not there, nor counted from another volume: the uploads were not all taken"
    run, steps, _ = mac(tmp_path / "d", platform={"collections": True, "rows": [sha], "blobs": {sha: b"xy"}})
    assert run.returncode != 0 and "1 uploaded files" in run.stderr, "a file cut short is not the file"
    run, steps, _ = mac(tmp_path / "e", env="RETINUE_PLATFORM=1\n")
    assert run.returncode != 0 and steps[3:] == ["check", "restore"] and "collections.sqlite, platform.sqlite" in run.stderr, \
        "with the platform on, a snapshot without its whole volume fails too"
    run, steps, _ = mac(tmp_path / "c", platform={"collections": True, "rows": [sha], "blobs": {sha: b"x"}})
    assert run.returncode == 0, run.stderr
    assert "integrity ok: agent.sqlite, archive.sqlite, collections.sqlite, platform.sqlite, router.sqlite" in run.stdout


def test_a_snapshot_dated_in_the_future_stops_the_month_before_anything_is_deleted(tmp_path):
    """--keep-within* counts back from the newest snapshot: one dated 2099 by a compromised server would put every
    real snapshot out of range, and forget --prune would delete them all."""
    forged = ('[{"id": "a1", "short_id": "a1", "time": "2026-01-01T03:30:01.5+01:00"},'
              ' {"id": "f0", "short_id": "f0f0f0f0", "time": "2099-01-01T00:00:00Z"}]')
    run, steps, _ = mac(tmp_path, FAKE_SNAPSHOTS=forged)
    assert run.returncode != 0 and steps == ["unlock", "version", "snapshots"], steps
    assert "snapshot f0f0f0f0 is dated 2099-01-01T00:00:00Z, in the future" in run.stderr
    assert not (tmp_path / "verified-snapshot").exists()


def test_another_restic_series_is_named_but_does_not_stop_the_month(tmp_path):
    run, _, _ = mac(tmp_path, FAKE_VERSION="restic 0.20.0 compiled with go1.27 on darwin/arm64")
    assert run.returncode == 0, run.stderr
    assert "warning: restic 0.20.0" in run.stderr
