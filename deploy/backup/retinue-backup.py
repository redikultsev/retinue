#!/usr/bin/env python3
"""Nightly backup of a Retinue host. Runs as root from retinue-backup.service, with the host's python3 (stdlib only).

Two restic passes make one night:

1. `files` — everything, with the containers running: the install folder and the stack's volumes, or, with
   RETINUE_BACKUP_SCOPE=host, the whole of `/` but for a short list of what rebuilds itself. It uploads the bulk.
2. `data` — the containers paused (`docker pause`), the data they write read again whole (`--force`), unpaused. A
   paused container's files lie on disk as if its process had been killed, which every database survives: SQLite,
   Postgres, MySQL alike, without the script knowing which is which. `--force` because restic skips a file whose
   size and times did not change, and a database page rewritten within one tick of the file system's clock looks
   unchanged. The pass reads, but uploads only what changed since the first.

A restore takes the data from the `data` snapshot and everything else from the `files` one of the same night.

Before the passes, into a root-only staging folder that pass 2 takes too: with the host scope, an inventory (versions,
images by digest, volumes, mounts, Swarm secrets' names), and — when the host runs Dokploy — its database (`pg_dump`,
as its own backup does) and the secret its environment variables are encrypted with: without it a new server shows
every app's variables as ciphertext.

<root>/status/backup.json — what the router's morning summary reports. Written on failure as well.
`retinue-backup.py --unpause` — the unit's ExecStopPost: unpauses what this run paused, whatever ended it.

Paths come from the environment (backup.env through systemd), so the same script runs in the tests.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("RETINUE_ROOT", "/srv/retinue"))
VOLUME_ROOT = Path(os.environ.get("RETINUE_VOLUME_ROOT", "/var/lib/docker/volumes"))
VOLUMES = os.environ.get("RETINUE_VOLUMES", "retinue_router-data retinue_assistant-data").split()
SCOPE = os.environ.get("RETINUE_BACKUP_SCOPE", "stack")  # stack | host
HOST_ROOT = Path(os.environ.get("RETINUE_HOST_ROOT", "/"))  # the tests point it at a folder
EXTRA_EXCLUDES = os.environ.get("RETINUE_BACKUP_EXCLUDE", "").split()
STAGING = Path(os.environ.get("RETINUE_BACKUP_STAGING", "/var/backups/retinue/staging"))
PAUSED = STAGING.parent / "paused"  # the ids this run paused, for --unpause
RESTIC = os.environ.get("RESTIC", "/usr/local/bin/restic")
DOCKER = os.environ.get("RETINUE_DOCKER", "docker")
HOST = os.environ.get("RETINUE_BACKUP_HOST", "retinue")  # snapshots are grouped by it; not the machine's name
STATUS = ROOT / "status" / "backup.json"
CACHE = "/var/cache/restic"  # systemd gives the service no HOME, and restic finds no cache without one
# Longest a container may stay paused: under the healthchecks' window (30 s × 3), so none is restarted as unhealthy.
PAUSE_MAX_S = int(os.environ.get("RETINUE_PAUSE_MAX_S", "60"))
# Containers that hold no state and serve others — the proxy in front of every site, a resolver — keep running: by
# name here, or by the label retinue.backup.pause=false on the container.
NO_PAUSE = os.environ.get("RETINUE_BACKUP_NO_PAUSE", "dokploy-traefik").split()
GROWTH_WARN = int(os.environ.get("RETINUE_BACKUP_GROWTH_WARN", str(2 << 30)))  # bytes uploaded in one night
# The stack's scope never takes these off the host: Claude Code's login file; the env files with the subscription
# token (stack.env), the generated secrets and the backup's own keys, the owner's dated copies of them included; the
# Matrix registration; the mail collector's and sender's folders — the owner's Google tokens. setup.sh writes the
# registration anew after a restore; the tokens come from the owner's computer. The pages' Traefik file carries their key, and
# their releases and public files are made again from what is kept. The host's scope takes all of it: a move to a
# new server is not simple without them (docs/backup.md).
SECRETS = [".credentials.json", f"{ROOT}/*.env", f"{ROOT}/*.env.*", f"{ROOT}/**/*.env", f"{ROOT}/**/*.env.*",
           f"{ROOT}/tuwunel/appservices", f"{ROOT}/mail", f"{ROOT}/send", f"{ROOT}/pages/traefik.yml",
           f"{ROOT}/pages/site", f"{ROOT}/pages/public"]
# An upload still being written: renamed or removed while restic reads it; no row names it.
PARTS = ["**/attachments/.*.part"]
# What the host's scope leaves out: kernel and runtime file systems, logs and caches, Docker's images and layers
# (rebuilt; its volumes are not — the negation brings them back, and order matters: restic lets a later pattern win).
HOST_EXCLUDES = ["/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/mnt", "/media", "/swap*", "/lost+found",
                 "/snap", "/var/lib/snapd/snaps", "/var/log", "/var/cache", "/var/lib/apt/lists", "**/.cache",
                 "/var/lib/docker/*", "!/var/lib/docker/volumes", "/var/lib/containerd"]
# Mounts restic must not walk into: network and FUSE file systems hang while their daemon is paused.
FOREIGN_FS = ("fuse", "nfs", "cifs", "smb", "sshfs", "9p", "ceph", "glusterfs")
# Bind-mount sources that are not data.
NOT_DATA = ("/proc", "/sys", "/dev", "/run", "/var/run", "/usr", "/lib", "/bin", "/sbin", "/etc/localtime",
            "/etc/timezone", "/etc/hosts", "/etc/resolv.conf")


class Failure(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code  # restic's exit code when it was restic that failed


def docker(*args: str, check: bool = True) -> str:
    run = subprocess.run([DOCKER, *args], capture_output=True, text=True)
    if check and run.returncode:
        raise Failure(f"docker {args[0]}: {(run.stderr or run.stdout).strip()[:200]}")
    return run.stdout


def labels(container: dict) -> dict:
    return (container.get("Config") or {}).get("Labels") or {}


def running() -> list[dict]:
    """The running containers as `docker inspect` gives them; with the stack's scope, the stack's only."""
    flags = ["ps", "-q"] + (["--filter", "label=com.docker.compose.project=retinue"] if SCOPE == "stack" else [])
    ids = docker(*flags).split()
    return json.loads(docker("inspect", *ids)) if ids else []


def data_paths(containers: list[dict]) -> list[Path]:
    """What pass 2 reads: the stack's volumes, or every volume and every bind-mounted folder of a running container."""
    if SCOPE == "stack":
        folders = [VOLUME_ROOT / name / "_data" for name in VOLUMES]
        if missing := [str(f) for f in folders if not f.is_dir()]:
            raise Failure(f"no volume at {missing[0]}")
        return folders
    paths = {VOLUME_ROOT}
    for container in containers:
        for mount in container.get("Mounts") or []:
            source = mount.get("Source") or ""
            if mount.get("Type") != "bind" or source in ("", "/") or source.startswith(NOT_DATA):
                continue
            path = Path(source)
            if path.exists() and not path.is_socket() and VOLUME_ROOT not in (path, *path.parents):
                paths.add(path)
    # A folder inside another one is read with it.
    return sorted(p for p in paths if not any(q in p.parents for q in paths))


def foreign_mounts() -> list[str]:
    try:
        lines = Path("/proc/self/mounts").read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) > 2 and parts[2].startswith(FOREIGN_FS) and parts[2] != "fusectl":
            found.append(parts[1].replace("\\040", " "))
    return found


def stage(containers: list[dict], warnings: list[str]) -> None:
    """The staging folder: the inventory and Dokploy's database and key. A step that fails is a warning, not a lost
    night: the files are in the snapshot whatever happens here."""
    shutil.rmtree(STAGING, ignore_errors=True)
    STAGING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # it holds secrets: root only
    STAGING.parent.chmod(0o700)
    STAGING.mkdir(mode=0o700)
    if SCOPE != "host":
        return
    for step in (inventory, dokploy):
        try:
            step(containers)
        except Exception as exc:
            warnings.append(f"{step.__name__}: {exc}")


def inventory(containers: list[dict]) -> None:
    """What a restore on a new host needs to know: versions, each container's image by digest, its volumes and bind
    mounts, the Postgres major it runs, the Swarm secrets to make again, the users and their ids. Never env or args."""
    def words(*args: str) -> list[str]:
        return docker(*args, check=False).split()

    os_release = Path("/etc/os-release")
    pretty = [line.split("=", 1)[1].strip('"') for line in (os_release.read_text().splitlines()
                                                           if os_release.exists() else [])
              if line.startswith("PRETTY_NAME=")]
    info = {
        "os": pretty[0] if pretty else "",
        "docker": docker("version", "--format", "{{.Server.Version}}", check=False).strip(),
        "restic": subprocess.run([RESTIC, "version"], capture_output=True, text=True).stdout.strip(),
        "containers": [{"name": c.get("Name", "").lstrip("/"), "image": (c.get("Config") or {}).get("Image"),
                        "digest": c.get("Image"),
                        "postgres": next((e.split("=", 1)[1] for e in (c.get("Config") or {}).get("Env") or []
                                          if e.startswith("PG_MAJOR=")), None),
                        "compose": labels(c).get("com.docker.compose.project"),
                        "service": labels(c).get("com.docker.swarm.service.name"),
                        "mounts": [{"type": m.get("Type"), "name": m.get("Name"), "source": m.get("Source"),
                                    "target": m.get("Destination")} for m in c.get("Mounts") or []]}
                       for c in containers],
        "volumes": words("volume", "ls", "-q"),
        "secrets": words("secret", "ls", "--format", "{{.Name}}"),
        "configs": words("config", "ls", "--format", "{{.Name}}"),
        "users": [{"name": f[0], "uid": int(f[2]), "gid": int(f[3])}
                  for f in (line.split(":") for line in Path("/etc/passwd").read_text().splitlines())
                  if len(f) > 3 and f[2].isdigit() and (int(f[2]) >= 1000 or f[2] == "0")],
    }
    (STAGING / "inventory.json").write_text(json.dumps(info, indent=1) + "\n")


def dokploy(containers: list[dict]) -> None:
    """Dokploy's database and the secret its apps' variables are encrypted with, when the host runs it."""
    def task(service: str) -> str | None:
        return next((c["Id"] for c in containers if labels(c).get("com.docker.swarm.service.name") == service), None)

    database, app = task("dokploy-postgres"), task("dokploy")
    if not database:
        return
    folder = STAGING / "dokploy"
    folder.mkdir(mode=0o700)
    with open(folder / "dokploy.dump", "wb") as out:
        run = subprocess.run([DOCKER, "exec", database, "pg_dump", "-Fc", "-U", "dokploy", "dokploy"], stdout=out,
                             stderr=subprocess.PIPE)
    if run.returncode:
        raise Failure(f"pg_dump: {run.stderr.decode(errors='replace').strip()[:200]}")
    if not app:
        raise Failure("its database is running but Dokploy is not: no encryption secret taken")
    secret = subprocess.run([DOCKER, "exec", app, "cat", "/run/secrets/dokploy_auth_secret"], capture_output=True)
    if secret.returncode or not secret.stdout.strip():
        raise Failure("no dokploy_auth_secret in its container: the apps' variables could not be decrypted")
    key = folder / "auth_secret"
    key.touch(mode=0o600)
    key.write_bytes(secret.stdout)


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


def restic(tag: str, paths: list[Path], excludes: list[str], *flags: str,
           timeout: float | None = None) -> tuple[dict, int, str]:
    """One `restic backup`: its summary, exit code and last error line. 3: the snapshot exists, but some files could
    not be read. Anything else without a snapshot is a failure. Past `timeout` the paused containers are unpaused
    first — a restic stuck in the kernel may not die at once — then restic is killed."""
    exclude_file = STAGING.parent / f"exclude-{tag}.txt"
    exclude_file.write_text("\n".join(excludes) + "\n")
    command = [RESTIC, "backup", "--json", "--host", HOST, "--tag", "retinue", "--tag", tag, "--retry-lock", "30m",
               "-o", "s3.connections=2", "--exclude-file", str(exclude_file), *flags, *map(str, paths)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={"RESTIC_CACHE_DIR": CACHE, **os.environ})
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        unpause()
        process.kill()
        process.communicate()
        raise Failure(f"containers paused longer than {PAUSE_MAX_S} s: unpaused, tonight's data snapshot is lost")
    summary = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("message_type") == "summary":
            summary = message
    if process.returncode not in (0, 3) or not summary.get("snapshot_id"):
        raise Failure(last_line(stderr) or f"restic exited {process.returncode} without a snapshot",
                      process.returncode or 1)
    return summary, process.returncode, last_line(stderr)


def pause(containers: list[dict]) -> None:
    ids = [c["Id"] for c in containers
           if labels(c).get("retinue.backup.pause") != "false" and c.get("Name", "").lstrip("/") not in NO_PAUSE]
    PAUSED.write_text("\n".join(ids) + "\n")  # first: --unpause must know them even if `docker pause` dies midway
    if ids:
        docker("pause", *ids)


def unpause() -> None:
    """Unpauses what this run paused and nothing else: a container the owner paused himself stays as it is."""
    try:
        ids = PAUSED.read_text().split()
    except OSError:
        return
    for one in ids:  # one by one: a container removed meanwhile must not keep the others frozen
        docker("unpause", one, check=False)
    PAUSED.unlink(missing_ok=True)


def backup() -> dict:
    warnings: list[str] = []
    containers = running()
    stage(containers, warnings)
    data = data_paths(containers)
    parts = [*PARTS, *EXTRA_EXCLUDES]
    if SCOPE == "host":
        foreign = foreign_mounts()
        files_paths, files_excludes = [HOST_ROOT], [*HOST_EXCLUDES, str(STAGING.parent), *foreign, *parts]
        data_excludes = [*foreign, *parts]
    else:
        files_paths, files_excludes = [ROOT, *data], [str(STAGING.parent), *SECRETS, *parts]
        data_excludes = [*SECRETS, *parts]
    # Pass 1. Files that vanished while it read (a work tree, a temporary file) are no news: pass 2 reads the data.
    files, _, _ = restic("files", files_paths, files_excludes)
    # Pass 2.
    started = time.time()
    pause(containers)
    try:
        snapshot, code, error = restic("data", [STAGING, *data], data_excludes, "--force", timeout=PAUSE_MAX_S)
    finally:
        unpause()
    if code == 3:
        warnings.append(error or "some files could not be read")
    added = files.get("data_added", 0) + snapshot.get("data_added", 0)
    if added > GROWTH_WARN:
        warnings.append(f"the backup grew by {added / (1 << 30):.1f} GB tonight")
    status = {"ok": True, "snapshot": snapshot["snapshot_id"], "files": files["snapshot_id"], "data_added": added,
              "size": files.get("total_bytes_processed", 0), "paused_s": round(time.time() - started, 1)}
    if warnings:
        status["warning"] = "; ".join(warnings)
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
    if sys.argv[1:] == ["--unpause"]:
        unpause()
        return 0
    started = time.time()
    try:
        status = backup()
    except Exception as exc:  # whatever broke, the morning summary must hear about it
        code = exc.code if isinstance(exc, Failure) else 1
        write({"ok": False, "finished": time.time(), "error": str(exc)[:300] or type(exc).__name__,
               "exit": code, "last_ok": previous_ok()})
        print(f"backup failed: {exc}", file=sys.stderr)
        return code
    write({**status, "finished": time.time(), "duration_s": round(time.time() - started)})
    print(f"backup done: snapshot {status['snapshot'][:8]}, {status['data_added']} bytes added, "
          f"paused {status['paused_s']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
