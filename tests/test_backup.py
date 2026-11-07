"""The host's nightly backup: two restic passes, the second with the containers paused; the secrets left out of the
stack's scope; the host's scope with Dokploy's database and key; a status file for the router. The owner's computer's monthly care.

restic and docker are replaced by fakes that record their arguments: no repository, no network, no root, no daemon.
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
import json, os, sys, time
args = sys.argv[1:]
tag = args[args.index("--tag", args.index("--tag") + 1) + 1] if args.count("--tag") > 1 else ""
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\\n")
open(os.environ["FAKE_LOG"] + ".cache", "w").write(os.environ.get("RESTIC_CACHE_DIR", ""))
if "--exclude-file" in args:
    open(os.environ["FAKE_LOG"] + f".exclude-{{tag}}", "w").write(open(args[args.index("--exclude-file") + 1]).read())
if tag == "data" and os.environ.get("FAKE_HANG"):
    time.sleep(30)
code = int(os.environ.get(f"FAKE_EXIT_{{tag.upper()}}", os.environ.get("FAKE_EXIT", "0")))
if code in (0, 3):
    print(json.dumps({{"message_type": "status", "percent_done": 1}}))
    print(json.dumps({{"message_type": "summary", "snapshot_id": (tag[0] * 8) * 8,
                      "data_added": int(os.environ.get("FAKE_ADDED", "2048")), "total_bytes_processed": 700000}}))
if code:
    print(os.environ.get("FAKE_ERROR", "Fatal: unable to open config file"), file=sys.stderr)
sys.exit(code)
"""
FAKE_DOCKER = """#!{python}
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_DOCKER_LOG"], "a").write(json.dumps(args) + "\\n")
containers = json.loads(os.environ.get("FAKE_CONTAINERS", "[]"))
if args[0] == "ps":
    print("\\n".join(c["Id"] for c in containers))
elif args[0] == "inspect":
    print(json.dumps([c for c in containers if c["Id"] in args[1:]]))
elif args[0] == "exec" and "pg_dump" in args:
    sys.stdout.buffer.write(b"PGDMP\\x01dokploy")
elif args[0] == "exec" and "cat" in args:
    sys.stdout.write(os.environ.get("FAKE_SECRET", "s3cret"))
elif args[0] == "version":
    print("28.5.0")
elif args[:2] in (["volume", "ls"], ["secret", "ls"], ["config", "ls"]):
    print({{"volume": "retinue_router-data", "secret": "dokploy_auth_secret", "config": ""}}[args[0]])
elif args[0] == "pause" and os.environ.get("FAKE_PAUSE_FAILS"):
    sys.exit("Error response from daemon: cannot pause")
"""


def container(cid, name, labels=None, mounts=()):
    return {"Id": cid, "Name": "/" + name, "Image": "sha256:" + cid * 4,
            "Config": {"Image": name + ":1", "Labels": labels or {}, "Env": ["SECRET=never", "PG_MAJOR=16"]},
            "Mounts": list(mounts)}


def host(tmp_path, containers=None):
    """A host in a folder: two volumes with databases, an install folder with secrets, fake restic and docker. By
    default the stack's two containers run."""
    volumes = tmp_path / "volumes"
    router = volumes / "retinue_router-data" / "_data"
    assistant = volumes / "retinue_assistant-data" / "_data"
    (assistant / "claude").mkdir(parents=True)
    router.mkdir(parents=True)
    for path in (router / "router.sqlite", router / "archive.sqlite", assistant / "agent.sqlite"):
        sqlite3.connect(path).execute("CREATE TABLE t (x TEXT)").connection.close()
    (assistant / "claude" / ".credentials.json").write_text("{}")
    srv = tmp_path / "srv"
    srv.mkdir()
    for name in ("stack.env", "secrets.env", "backup.env", "stack.env.bak-20261006"):
        (srv / name).write_text("X=1\n")
    (srv / "router.yaml").write_text("owner: owner\n")
    for name, text in (("restic", FAKE_RESTIC), ("docker", FAKE_DOCKER)):
        fake = tmp_path / name
        fake.write_text(text.format(python=sys.executable))
        fake.chmod(0o755)
    if containers is None:
        containers = [container("aaa1", "retinue-router-1"), container("bbb2", "retinue-assistant-1")]
    env = {"PATH": os.environ["PATH"], "RETINUE_ROOT": str(srv), "RETINUE_VOLUME_ROOT": str(volumes),
           "RETINUE_BACKUP_STAGING": str(tmp_path / "backups" / "staging"), "RESTIC": str(tmp_path / "restic"),
           "RETINUE_DOCKER": str(tmp_path / "docker"), "FAKE_LOG": str(tmp_path / "restic.log"),
           "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"), "FAKE_CONTAINERS": json.dumps(containers)}
    return srv, router, env


def run(env, *args, **extra):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env={**env, **extra})


def calls(tmp_path, name="restic.log"):
    path = tmp_path / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_night_is_two_passes_the_second_with_the_stack_paused_and_read_again_whole(tmp_path):
    srv, router, env = host(tmp_path)
    done = run(env)
    assert done.returncode == 0, done.stderr
    staging = tmp_path / "backups" / "staging"
    assistant = tmp_path / "volumes" / "retinue_assistant-data" / "_data"
    files, data = calls(tmp_path)
    for call, tag in ((files, "files"), (data, "data")):
        assert call[0] == "backup" and "--json" in call and call[call.index("--host") + 1] == "retinue"
        assert call[call.index("--tag") + 1:call.index("--tag") + 4] == ["retinue", "--tag", tag]
        assert call[call.index("--retry-lock") + 1] == "30m"
    assert files[-3:] == [str(srv), str(router), str(assistant)], "the install folder and the stack's volumes"
    assert "--force" not in files
    assert data[-3:] == [str(staging), str(router), str(assistant)], "the data again, and the staging folder"
    assert "--force" in data, "a page rewritten within one clock tick looks unchanged: read it all again"
    assert ["ps", "-q", "--filter", "label=com.docker.compose.project=retinue"] in calls(tmp_path, "docker.log"), \
        "the stack's scope pauses the stack and nothing else on the host"
    docker = calls(tmp_path, "docker.log")
    assert docker.index(["pause", "aaa1", "bbb2"]) < docker.index(["unpause", "aaa1"]), docker
    assert ["unpause", "bbb2"] in docker and not (tmp_path / "backups" / "paused").exists()
    for tag in ("files", "data"):
        excluded = (tmp_path / f"restic.log.exclude-{tag}").read_text().splitlines()
        assert ".credentials.json" in excluded, "the Claude login never leaves the host"
        assert f"{srv}/*.env" in excluded and f"{srv}/*.env.*" in excluded, \
            "stack.env holds the subscription token; backup.env the backup's own keys"
        assert f"{srv}/mail" in excluded and f"{srv}/send" in excluded, "the owner's Google tokens never leave"
        assert f"{srv}/pages/traefik.yml" in excluded and f"{srv}/pages/site" in excluded
        assert "**/attachments/.*.part" in excluded, "an upload still being written stays out"
        assert not any("sqlite" in line for line in excluded), "the databases go in as they lie, paused"
    assert str(staging.parent) in (tmp_path / "restic.log.exclude-files").read_text().splitlines()
    assert (tmp_path / "restic.log.cache").read_text() == "/var/cache/restic", \
        "systemd gives the service no HOME, and restic finds no cache without one"
    assert oct(staging.parent.stat().st_mode & 0o777) == "0o700"

    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["ok"] is True and status["snapshot"] == "d" * 64 and status["files"] == "f" * 64
    assert status["data_added"] == 4096 and status["size"] == 700000 and status["finished"] > 0
    assert status["paused_s"] < 10 and "warning" not in status
    assert oct((srv / "status" / "backup.json").stat().st_mode & 0o777) == "0o644", "the router reads it"


def test_whatever_ends_the_night_unpauses_what_it_paused_and_nothing_else(tmp_path):
    srv, _, env = host(tmp_path)
    failed = run(env, FAKE_EXIT_DATA="1", FAKE_ERROR="Fatal: unable to save snapshot")
    assert failed.returncode == 1
    docker = calls(tmp_path, "docker.log")
    assert ["unpause", "aaa1"] in docker and ["unpause", "bbb2"] in docker, "a failed pass unpauses too"
    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["ok"] is False and status["error"] == "Fatal: unable to save snapshot"

    (tmp_path / "docker.log").unlink()
    hung = run(env, FAKE_HANG="1", RETINUE_PAUSE_MAX_S="1")
    assert hung.returncode == 1
    docker = calls(tmp_path, "docker.log")
    assert ["unpause", "aaa1"] in docker, "a restic stuck past the limit: the containers first, then restic"
    assert "paused longer than 1 s" in json.loads((srv / "status" / "backup.json").read_text())["error"]

    # The unit's ExecStopPost after a kill: the ids this run wrote, and only those.
    (tmp_path / "docker.log").unlink()
    (tmp_path / "backups" / "paused").write_text("aaa1\n")
    assert run(env, "--unpause").returncode == 0
    assert calls(tmp_path, "docker.log") == [["unpause", "aaa1"]]
    assert not (tmp_path / "backups" / "paused").exists()
    assert run(env, "--unpause").returncode == 0 and calls(tmp_path, "docker.log") == [["unpause", "aaa1"]], \
        "nothing paused: nothing to do"


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
    assert status["last_ok"] == good, "kept across failed nights"

    run(env, FAKE_EXIT_FILES="3", FAKE_ERROR="Warning: at least one source file could not be read")
    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["ok"] is True and "warning" not in status, "files gone while pass 1 read: pass 2 has the data"
    run(env, FAKE_EXIT_DATA="3", FAKE_ERROR="Warning: at least one source file could not be read")
    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["ok"] is True and status["warning"].startswith("Warning: at least one"), "in the data it is news"

    run(env, FAKE_ADDED=str(3 << 30))
    assert "grew by 6.0 GB" in json.loads((srv / "status" / "backup.json").read_text())["warning"]

    env["RETINUE_VOLUMES"] = "retinue_router-data retinue_gone-data"
    run(env)
    status = json.loads((srv / "status" / "backup.json").read_text())
    assert status["ok"] is False and "retinue_gone-data" in status["error"], "a missing volume is a failure, not a skip"


def test_the_host_scope_takes_the_whole_server_and_dokploy_with_its_key(tmp_path):
    """RETINUE_BACKUP_SCOPE=host: `/` but for what rebuilds itself, every container paused but the proxy, every
    volume and bind-mounted folder read again, Dokploy's database and encryption secret, an inventory."""
    app = tmp_path / "apps" / "site"
    app.mkdir(parents=True)
    containers = [
        container("aaa1", "retinue-router-1", {"com.docker.compose.project": "retinue"}),
        container("ccc3", "dokploy.1.x", {"com.docker.swarm.service.name": "dokploy"}),
        container("ddd4", "dokploy-postgres.1.y", {"com.docker.swarm.service.name": "dokploy-postgres"},
                  [{"Type": "volume", "Name": "dokploy-postgres", "Source": "/ignored", "Destination": "/var/lib"}]),
        container("eee5", "dokploy-traefik", mounts=[{"Type": "bind", "Source": "/var/run/docker.sock",
                                                      "Destination": "/var/run/docker.sock"}]),
        container("fff6", "site-app", mounts=[{"Type": "bind", "Source": str(app), "Destination": "/data"},
                                              {"Type": "bind", "Source": "/etc/localtime", "Destination": "/l"}]),
        container("ggg7", "split-dns", {"retinue.backup.pause": "false"}),
    ]
    srv, _, env = host(tmp_path, containers)
    done = run(env, RETINUE_BACKUP_SCOPE="host", RETINUE_HOST_ROOT=str(tmp_path))
    assert done.returncode == 0, done.stderr
    staging = tmp_path / "backups" / "staging"
    files, data = [call for call in calls(tmp_path) if call[0] == "backup"]
    assert files[-1] == str(tmp_path), "the whole server"
    assert data[-3:] == [str(staging), str(app), str(tmp_path / "volumes")], \
        "every volume and the folders bind-mounted for data; not the docker socket, not /etc/localtime"
    excluded = (tmp_path / "restic.log.exclude-files").read_text().splitlines()
    assert excluded.index("/var/lib/docker/*") < excluded.index("!/var/lib/docker/volumes"), \
        "Docker's layers out, its volumes back in: a later pattern wins"
    assert "/proc" in excluded and "**/.cache" in excluded and "/var/log" in excluded
    assert not any(".env" in line or "credentials" in line for line in excluded), \
        "the host's scope takes the secrets: a move without them is not simple"
    docker = calls(tmp_path, "docker.log")
    assert ["ps", "-q"] in docker, "every running container, not the stack's only"
    assert ["pause", "aaa1", "ccc3", "ddd4", "fff6"] in docker, "the proxy and the labelled resolver keep running"
    assert docker.index(["exec", "ddd4", "pg_dump", "-Fc", "-U", "dokploy", "dokploy"]) < \
        docker.index(["pause", "aaa1", "ccc3", "ddd4", "fff6"]), "no exec into a paused container"
    assert (staging / "dokploy" / "dokploy.dump").read_bytes().startswith(b"PGDMP")
    assert (staging / "dokploy" / "auth_secret").read_text() == "s3cret"
    assert oct((staging / "dokploy" / "auth_secret").stat().st_mode & 0o777) == "0o600"
    inventory = json.loads((staging / "inventory.json").read_text())
    assert inventory["docker"] == "28.5.0" and inventory["secrets"] == ["dokploy_auth_secret"]
    assert {c["name"]: c["postgres"] for c in inventory["containers"]}["dokploy-postgres.1.y"] == "16"
    assert "never" not in (staging / "inventory.json").read_text(), "no container's environment in the inventory"
    assert "warning" not in json.loads((srv / "status" / "backup.json").read_text())

    failed = run(env, RETINUE_BACKUP_SCOPE="host", RETINUE_HOST_ROOT=str(tmp_path), FAKE_SECRET="")
    assert failed.returncode == 0, "the files are in the snapshot whatever Dokploy's step did"
    assert "dokploy: no dokploy_auth_secret" in json.loads((srv / "status" / "backup.json").read_text())["warning"]


CARE = ROOT / "deploy" / "backup" / "care.sh"
FAKE_CARE_RESTIC = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
[[ $1 != version ]] || { echo "${FAKE_VERSION:-restic 0.19.1 compiled with go1.26 on darwin/arm64}"; exit 0; }
[[ $1 != snapshots ]] || { echo "$FAKE_SNAPSHOTS"; exit 0; }
[[ $1 != -r || $3 != init ]] || { mkdir -p "$2" && touch "$2/config"; }
if [[ $1 == restore ]]; then
  args=("$@"); for i in "${!args[@]}"; do [[ ${args[$i]} == --target ]] && target=${args[$((i + 1))]}; done
  volumes="$target/var/lib/docker/volumes"
  for dir in "$FAKE_RESTORE"/*/; do name=$(basename "$dir"); mkdir -p "$volumes/$name/_data"; cp -R "$dir". "$volumes/$name/_data/"; done
  [[ -z ${FAKE_VOLUMES:-} ]] || cp -R "$FAKE_VOLUMES/." "$volumes/"
  [[ -z ${FAKE_STAGING:-} ]] || { mkdir -p "$target/var/backups/retinue" && cp -R "$FAKE_STAGING" "$target/var/backups/retinue/staging"; }
fi"""


FAKE_SSH = """#!/bin/bash
echo "$* <<< $(cat)" >> "$FAKE_SSH_LOG"
"""


def care(tmp_path, archive_bytes=None, platform=None, env="", **fake):
    """The owner's computer: an env file with the deleting key, a fake restic whose `restore` lays out last night's copies.
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
    restic.write_text(FAKE_CARE_RESTIC)
    restic.chmod(0o755)
    ssh = tmp_path / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    run = subprocess.run(["bash", str(CARE), "monthly"], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                              "RETINUE_BACKUP_ENV": str(env_file), "RESTIC": str(restic), "PYTHON": sys.executable,
                              "SSH": str(ssh), "FAKE_SSH_LOG": str(tmp_path / "ssh.log"),
                              "FAKE_LOG": str(tmp_path / "restic.log"), "FAKE_RESTORE": str(snapshot), **volumes,
                              "FAKE_SNAPSHOTS": json.dumps([{"id": "a1b2c3d4e5f6", "short_id": "a1b2c3d4",
                                                             "time": "2026-01-01T03:30:01.123456789+01:00",
                                                             "tags": ["retinue", "data"]}]),
                              **fake})
    calls = (tmp_path / "restic.log").read_text().splitlines() if (tmp_path / "restic.log").exists() else []
    steps = [call.split()[2] if call.startswith("-r ") else call.split()[0] for call in calls]  # -r <local repo> <command>
    return run, steps, calls


def test_monthly_care_on_the_owners_computer_checks_restores_copies_and_only_then_prunes(tmp_path):
    run, steps, calls = care(tmp_path)
    assert run.returncode == 0, run.stderr
    assert steps[0] == "unlock", "the night's stale lock would stop the check: it goes first"
    assert calls[1:3] == ["version", "snapshots --json --host retinue --tag retinue"], "the snapshot to prove is chosen first"
    calls, steps = calls[3:], steps[3:]
    assert steps == ["check", "restore", "init", "copy", "unlock", "forget", "forget", "check"], calls
    assert calls[0] == "check --read-data", "every byte read back: the server's key could overwrite what it wrote"
    assert calls[1].startswith("restore a1b2c3d4e5f6 --target "), "the snapshot chosen, not `latest`"
    assert calls[1].endswith(" --include *.sqlite* --include *.db --include *.db-* --include attachments "
                             "--include staging"), "the databases and what goes with them, not the server's secrets"
    assert calls[2] == f"-r {tmp_path / 'local-repo'} init --from-repo s3:https://s3.example/bucket/retinue " \
                       "--copy-chunker-params", "the copy on the owner's computer is made once, deduplicating like the original"
    assert calls[3] == f"-r {tmp_path / 'local-repo'} copy --from-repo s3:https://s3.example/bucket/retinue " \
                       "--host retinue --tag retinue"
    assert calls[4] == "unlock", "the server's key cannot delete its own locks; forget needs them gone"
    assert calls[5] == ("forget --host retinue --tag retinue --group-by host,tags --keep-within-daily 7d --keep-within-weekly 1m "
                        "--keep-within-monthly 6m --prune")
    assert calls[6] == (f"-r {tmp_path / 'local-repo'} forget --host retinue --tag retinue --group-by host,tags --keep-within-daily 7d "
                        "--keep-within-weekly 1m --keep-within-monthly 24m --prune"), "the copy here is kept too, longer"
    assert calls[7] == f"-r {tmp_path / 'local-repo'} check"
    assert (tmp_path / "verified-snapshot").read_text() == "a1b2c3d4e5f6\n", "a restore elsewhere starts from it"
    assert "integrity ok: 3 databases (agent.sqlite, archive.sqlite, router.sqlite)" in run.stdout
    assert not list(tmp_path.glob("retinue-restore.*")), "the restored copy is removed"
    assert not (tmp_path / "ssh.log").exists() and "set RETINUE_SSH_HOST" in run.stderr, "no host named: said, not failed"


def test_a_monthly_run_that_went_through_tells_the_server_and_a_broken_one_does_not(tmp_path):
    run, _, _ = care(tmp_path, env="RETINUE_SSH_HOST=server.example\n")
    assert run.returncode == 0, run.stderr
    [told] = (tmp_path / "ssh.log").read_text().splitlines()
    assert told.startswith("server.example sudo tee /srv/retinue/status/restore.json >/dev/null <<< {\"checked\": ")
    assert told.endswith(', "snapshot": "a1b2c3d4e5f6"}'), "the server's summary names the proven snapshot"
    broken = tmp_path / "broken"
    broken.mkdir()
    run, _, _ = care(broken, archive_bytes=b"not a database", env="RETINUE_SSH_HOST=server.example\n")
    assert run.returncode != 0 and not (broken / "ssh.log").exists(), "a failed check tells the server nothing"


def test_a_broken_restore_stops_before_anything_is_deleted(tmp_path):
    run, steps, _ = care(tmp_path, archive_bytes=b"not a database")
    assert run.returncode != 0 and steps[3:] == ["check", "restore"], "no copy, no prune after a failed restore"
    assert "archive.sqlite" in run.stderr
    assert not list(tmp_path.glob("retinue-restore.*")), "nor a copy of the archive left in the temporary folder"


def test_with_the_platform_a_snapshot_without_its_collections_or_their_files_fails_the_restore(tmp_path):
    sha = "ab" * 32
    run, steps, _ = care(tmp_path / "a", platform={"collections": False, "rows": [], "blobs": {}})
    assert run.returncode != 0 and steps[3:] == ["check", "restore"] and "collections.sqlite" in run.stderr, \
        "the platform makes the file at its start: a snapshot without it lost it"
    run, steps, _ = care(tmp_path / "b", platform={"collections": True, "rows": [sha], "blobs": {}, "other": [sha]})
    assert run.returncode != 0 and steps[3:] == ["check", "restore"] and "1 uploaded files" in run.stderr, \
        "a row whose file is not there, nor counted from another volume: the uploads were not all taken"
    run, steps, _ = care(tmp_path / "d", platform={"collections": True, "rows": [sha], "blobs": {sha: b"xy"}})
    assert run.returncode != 0 and "1 uploaded files" in run.stderr, "a file cut short is not the file"
    run, steps, _ = care(tmp_path / "e", env="RETINUE_PLATFORM=1\n")
    assert run.returncode != 0 and steps[3:] == ["check", "restore"] and "collections.sqlite, platform.sqlite" in run.stderr, \
        "with the platform on, a snapshot without its whole volume fails too"
    run, steps, _ = care(tmp_path / "c", platform={"collections": True, "rows": [sha], "blobs": {sha: b"x"}})
    assert run.returncode == 0, run.stderr
    assert "(agent.sqlite, archive.sqlite, collections.sqlite, platform.sqlite, router.sqlite)" in run.stdout


def test_a_snapshot_dated_in_the_future_stops_the_month_before_anything_is_deleted(tmp_path):
    """--keep-within* counts back from the newest snapshot: one dated 2099 by a compromised server would put every
    real snapshot out of range, and forget --prune would delete them all."""
    forged = ('[{"id": "a1", "short_id": "a1", "time": "2026-01-01T03:30:01.5+01:00"},'
              ' {"id": "f0", "short_id": "f0f0f0f0", "time": "2099-01-01T00:00:00Z"}]')
    run, steps, _ = care(tmp_path, FAKE_SNAPSHOTS=forged)
    assert run.returncode != 0 and steps == ["unlock", "version", "snapshots"], steps
    assert "snapshot f0f0f0f0 is dated 2099-01-01T00:00:00Z, in the future" in run.stderr
    assert not (tmp_path / "verified-snapshot").exists()


def test_another_restic_series_is_named_but_does_not_stop_the_month(tmp_path):
    run, _, _ = care(tmp_path, FAKE_VERSION="restic 0.20.0 compiled with go1.27 on darwin/arm64")
    assert run.returncode == 0, run.stderr
    assert "warning: restic 0.20.0" in run.stderr


def test_the_restore_check_opens_every_database_by_its_header_and_dokploys_dump_and_key(tmp_path):
    staging = tmp_path / "staging"
    (staging / "dokploy").mkdir(parents=True)
    (staging / "dokploy" / "dokploy.dump").write_bytes(b"PGDMP\x01")
    (staging / "dokploy" / "auth_secret").write_text("s3cret")
    wg = tmp_path / "volumes" / "wg-easy_etc_wireguard" / "_data"
    wg.mkdir(parents=True)
    sqlite3.connect(wg / "wg-easy.db").execute("CREATE TABLE t (x)").connection.close()
    run, _, _ = care(tmp_path / "a", FAKE_STAGING=str(staging), FAKE_VOLUMES=str(tmp_path / "volumes"))
    assert run.returncode == 0, run.stderr
    assert "integrity ok: 4 databases (agent.sqlite, archive.sqlite, router.sqlite), Dokploy" in run.stdout, \
        "wg-easy.db is a database too, found by its header"
    (staging / "dokploy" / "auth_secret").unlink()
    run, steps, _ = care(tmp_path / "b", FAKE_STAGING=str(staging), FAKE_VOLUMES=str(tmp_path / "volumes"))
    assert run.returncode != 0 and "encryption secret" in run.stderr and "forget" not in steps
    (staging / "dokploy" / "auth_secret").write_text("s3cret")
    (wg / "wg-easy.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    run, _, _ = care(tmp_path / "c", FAKE_STAGING=str(staging), FAKE_VOLUMES=str(tmp_path / "volumes"))
    assert run.returncode != 0 and "wg-easy.db" in run.stderr, "a torn database anywhere fails the month"


def test_before_the_first_night_with_two_passes_there_is_nothing_to_prove(tmp_path):
    old = '[{"id": "a1", "short_id": "a1", "time": "2026-01-01T03:30:01Z", "tags": ["retinue"]}]'
    run, steps, _ = care(tmp_path, FAKE_SNAPSHOTS=old)
    assert run.returncode != 0 and steps == ["unlock", "version", "snapshots"]
    assert "no data snapshot yet" in run.stderr
