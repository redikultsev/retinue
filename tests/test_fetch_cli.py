"""deploy/lanes/fetch-cli.sh installs the lane CLI only after the tarball matches its npm integrity in cli.lock.
The download is a fake `curl` that copies a local tarball, so no network is needed."""

import base64
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://registry.example/claude.tgz"


def _tarball() -> bytes:
    data = b"#!/bin/sh\necho fake claude\n"
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo("package/claude")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def fetch(tmp_path, matching: bool) -> tuple[subprocess.CompletedProcess, Path]:
    tarball = _tarball()
    (tmp_path / "cli.tgz").write_bytes(tarball)
    lanes = tmp_path / "lanes"
    lanes.mkdir()
    shutil.copy(ROOT / "deploy" / "lanes" / "fetch-cli.sh", lanes)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball if matching else b"another tarball").digest()).decode()
    (lanes / "cli.lock").write_text("".join(f"{arch} 9.9.9 {URL} {integrity}\n"
                                            for arch in ("linux-x64", "linux-arm64")))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    curl = bin_ / "curl"
    curl.write_text('#!/usr/bin/env bash\n'
                    'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
                    f'cp "{tmp_path / "cli.tgz"}" "$out"\n')
    curl.chmod(0o755)
    dest = tmp_path / "dest"
    run = subprocess.run(["bash", str(lanes / "fetch-cli.sh"), str(dest)], capture_output=True, text=True,
                         env={"PATH": f"{bin_}:{os.environ['PATH']}"})
    return run, dest


def test_a_tarball_matching_the_lock_is_installed(tmp_path):
    run, dest = fetch(tmp_path, matching=True)
    assert run.returncode == 0, run.stderr
    assert os.access(dest / "claude", os.X_OK)
    assert "9.9.9" in run.stdout


def test_a_tarball_not_matching_the_lock_is_refused_before_unpacking(tmp_path):
    run, dest = fetch(tmp_path, matching=False)
    assert run.returncode != 0
    assert "integrity mismatch" in run.stderr
    assert not (dest / "claude").exists()
