#!/usr/bin/env python3
"""Nightly backup of a Retinue host. Runs as root from retinue-backup.service, with the host's python3 (stdlib only).

1. Every *.sqlite in the stack's volumes is copied with SQLite's backup API into a staging folder and checked:
   the databases use a rollback journal, and a plain file copy taken during a write is torn.
2. `restic backup` of the copies, the volumes and the install folder. The raw databases (their copies are in
   staging), the Claude login and the secret env files stay out.
3. <root>/status/backup.json — what the router's morning summary reports. Written on failure as well.

Paths come from the environment (backup.env through systemd), so the same script runs in the tests.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("RETINUE_ROOT", "/srv/retinue"))
VOLUME_ROOT = Path(os.environ.get("RETINUE_VOLUME_ROOT", "/var/lib/docker/volumes"))
VOLUMES = os.environ.get("RETINUE_VOLUMES", "retinue_router-data retinue_assistant-data").split()
STAGING = Path(os.environ.get("RETINUE_BACKUP_STAGING", "/var/backups/retinue/staging"))
RESTIC = os.environ.get("RESTIC", "/usr/local/bin/restic")
HOST = os.environ.get("RETINUE_BACKUP_HOST", "retinue")  # snapshots are grouped by it; not the machine's name
STATUS = ROOT / "status" / "backup.json"
CACHE = "/var/cache/restic"  # systemd gives the service no HOME, and restic finds no cache without one
# Never leaves the host: Claude Code's login file; the env files with the subscription token (stack.env), the
# generated secrets and the backup's own keys, the owner's dated copies of them included; the Matrix registration,
# which carries tokens from secrets.env; the mail collector's folder — the owner's Google tokens, and its cursors,
# which a full sync rebuilds. setup.sh writes the registration anew after a restore; the tokens come from the Mac.
SECRETS = [".credentials.json", f"{ROOT}/*.env", f"{ROOT}/*.env.*", f"{ROOT}/**/*.env", f"{ROOT}/**/*.env.*",
           f"{ROOT}/tuwunel/appservices", f"{ROOT}/mail"]


class Failure(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code  # restic's exit code when it was restic that failed


def snapshot() -> tuple[list[Path], list[Path]]:
    """Consistent copies into STAGING/<volume>/...; returns (volume folders, raw database files)."""
    shutil.rmtree(STAGING, ignore_errors=True)
    STAGING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # the copies hold the archive: root only
    STAGING.parent.chmod(0o700)
    STAGING.mkdir(mode=0o700)
    folders, raw = [], []
    for name in VOLUMES:
        folder = VOLUME_ROOT / name / "_data"
        if not folder.is_dir():
            raise Failure(f"no volume {name} at {folder}")
        folders.append(folder)
        for src in sorted(folder.rglob("*.sqlite")):
            dst = STAGING / name / src.relative_to(folder)
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Read-only: root creates no journal files in the volume. A hot journal after a crash makes this fail,
            # which is right: the database is not in a state worth keeping until its owner opens it.
            source = sqlite3.connect(f"{src.as_uri()}?mode=ro", uri=True, timeout=30)
            target = sqlite3.connect(dst)
            try:
                source.backup(target)
                check = target.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                target.close()
                source.close()
            if check != "ok":
                raise Failure(f"{src}: quick_check {check}")
            raw.append(src)
    return folders, raw


def last_line(text: str) -> str:
    """The last line restic wrote to stderr, as a message: JSON errors carry it in error.message."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    try:
        message = json.loads(lines[-1])
    except ValueError:
        return lines[-1][:300]
    if isinstance(message, dict):  # 0.19: {"message_type": "exit_error", "message": …}; older: {"error": {"message": …}}
        text = message.get("message") or (message.get("error") or {}).get("message")
        if text:
            return str(text)[:300]
    return lines[-1][:300]


def backup(folders: list[Path], raw: list[Path]) -> dict:
    excludes = STAGING.parent / "exclude.txt"
    excludes.write_text("\n".join([*(f"{p}{suffix}" for p in raw for suffix in ("", "-journal", "-wal", "-shm")),
                                   *SECRETS]) + "\n")
    run = subprocess.run([RESTIC, "backup", "--json", "--host", HOST, "--tag", "retinue", "--retry-lock", "30m",
                          "-o", "s3.connections=2", "--exclude-file", str(excludes),
                          str(STAGING), *map(str, folders), str(ROOT)], capture_output=True, text=True,
                         env={"RESTIC_CACHE_DIR": CACHE, **os.environ})
    summary = {}
    for line in run.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("message_type") == "summary":
            summary = message
    # 3: the snapshot exists, but some files could not be read. Anything else without a snapshot is a failure.
    if run.returncode not in (0, 3) or not summary.get("snapshot_id"):
        raise Failure(last_line(run.stderr) or f"restic exited {run.returncode} without a snapshot", run.returncode or 1)
    status = {"ok": True, "snapshot": summary["snapshot_id"], "data_added": summary.get("data_added", 0),
              "size": summary.get("total_bytes_processed", 0)}
    if run.returncode == 3:
        status["warning"] = last_line(run.stderr) or "some files could not be read"
    return status


def write(status: dict) -> None:
    STATUS.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False) + "\n")
    tmp.chmod(0o644)  # the router runs as an unprivileged user and reads it through a read-only mount
    os.replace(tmp, STATUS)


def previous_ok() -> float | None:
    try:
        before = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None
    return before.get("finished") if before.get("ok") else before.get("last_ok")


def main() -> int:
    started = time.time()
    try:
        status = backup(*snapshot())
    except Exception as exc:  # whatever broke, the morning summary must hear about it
        code = exc.code if isinstance(exc, Failure) else 1
        write({"ok": False, "finished": time.time(), "error": str(exc)[:300] or type(exc).__name__,
               "exit": code, "last_ok": previous_ok()})
        print(f"backup failed: {exc}", file=sys.stderr)
        return code
    write({**status, "finished": time.time(), "duration_s": round(time.time() - started)})
    print(f"backup done: snapshot {status['snapshot'][:8]}, {status['data_added']} bytes added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
