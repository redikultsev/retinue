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
    assert f"{srv}/lifehub/nginx.conf" in excluded and f"{srv}/lifehub/traefik.yml" in excluded, \
        "the life hub's key, rendered from secrets.env; setup.sh renders it again"
    assert f"{srv}/lifehub/site" in excluded, "the built pages: the builder makes them again from the data"
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


MAC = ROOT / "deploy" / "backup" / "mac.sh"
FAKE_MAC_RESTIC = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
if [[ $1 == restore ]]; then
  target=${@: -1}
  mkdir -p "$target/var/backups/retinue" && cp -R "$FAKE_RESTORE" "$target/var/backups/retinue/staging"
fi
"""


def mac(tmp_path, archive_bytes=None):
    """The owner's Mac: an env file with the deleting key, a fake restic whose `restore` lays out last night's copies."""
    snapshot = tmp_path / "snapshot"
    (snapshot / "retinue_router-data").mkdir(parents=True)
    (snapshot / "retinue_assistant-data").mkdir()
    for name in ("retinue_router-data/router.sqlite", "retinue_assistant-data/agent.sqlite"):
        sqlite3.connect(snapshot / name).execute("CREATE TABLE t (x)").connection.close()
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
                        f"RETINUE_LOCAL_REPO={tmp_path / 'local-repo'}\n")
    restic = tmp_path / "restic"
    restic.write_text(FAKE_MAC_RESTIC)
    restic.chmod(0o755)
    run = subprocess.run(["bash", str(MAC), "monthly"], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                              "RETINUE_BACKUP_ENV": str(env_file), "RESTIC": str(restic), "PYTHON": sys.executable,
                              "FAKE_LOG": str(tmp_path / "restic.log"), "FAKE_RESTORE": str(snapshot)})
    calls = (tmp_path / "restic.log").read_text().splitlines() if (tmp_path / "restic.log").exists() else []
    steps = [call.split()[2] if call.startswith("-r ") else call.split()[0] for call in calls]  # -r <local repo> <command>
    return run, steps, calls


def test_monthly_care_on_the_mac_checks_restores_copies_and_only_then_prunes(tmp_path):
    run, steps, calls = mac(tmp_path)
    assert run.returncode == 0, run.stderr
    assert steps == ["check", "restore", "init", "copy", "unlock", "forget", "check"], calls
    assert calls[0] == "check --read-data", "every byte read back: the server's key could overwrite what it wrote"
    assert calls[1].startswith("restore latest --host retinue --tag retinue --target ")
    assert calls[2] == f"-r {tmp_path / 'local-repo'} init --from-repo s3:https://s3.example/bucket/retinue " \
                       "--copy-chunker-params", "the copy on the Mac is made once, deduplicating like the original"
    assert calls[3] == f"-r {tmp_path / 'local-repo'} copy --from-repo s3:https://s3.example/bucket/retinue " \
                       "--host retinue --tag retinue"
    assert calls[4] == "unlock", "the server's key cannot delete its own locks; forget needs them gone"
    assert calls[5] == ("forget --host retinue --tag retinue --keep-within-daily 7d --keep-within-weekly 1m "
                        "--keep-within-monthly 6m --prune")
    assert calls[6] == f"-r {tmp_path / 'local-repo'} check"
    assert "integrity ok: agent.sqlite, archive.sqlite, router.sqlite" in run.stdout
    assert not list(tmp_path.glob("retinue-restore.*")), "the restored copy is removed"


def test_a_broken_restore_stops_before_anything_is_deleted(tmp_path):
    run, steps, _ = mac(tmp_path, archive_bytes=b"not a database")
    assert run.returncode != 0 and steps == ["check", "restore"], "no copy, no prune after a failed restore"
    assert "archive.sqlite" in run.stderr
    assert not list(tmp_path.glob("retinue-restore.*")), "nor a copy of the archive left in the temporary folder"
