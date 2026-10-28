#!/usr/bin/env bash
# Fetch the lanes' pinned Claude Code CLI: the tarball for this machine's architecture from cli.lock, its npm
# integrity checked before anything is unpacked, `package/claude` installed as <dest>/claude.
# Used by the lane image and by developers running the probes (RETINUE_LANE_CLI=<dest>/claude).
#
#   bash deploy/lanes/fetch-cli.sh /opt/claude
set -euo pipefail

dest=${1:?usage: fetch-cli.sh <dest>}
lock="$(dirname "${BASH_SOURCE[0]}")/cli.lock"
case "$(uname -m)" in
  x86_64 | amd64) arch=linux-x64 ;;
  aarch64 | arm64) arch=linux-arm64 ;;
  *) echo "no pinned CLI for $(uname -m)" >&2; exit 1 ;;
esac
read -r _ version url integrity < <(grep "^$arch " "$lock") || { echo "$arch is not in $lock" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --proto '=https' -o "$tmp/cli.tgz" "$url"
python3 - "$tmp/cli.tgz" "$integrity" <<'EOF'
import base64, hashlib, sys
path, integrity = sys.argv[1], sys.argv[2]
with open(path, "rb") as f:
    actual = "sha512-" + base64.b64encode(hashlib.file_digest(f, "sha512").digest()).decode()
if actual != integrity:
    sys.exit(f"integrity mismatch: {actual} is not {integrity}")
EOF
tar -xzf "$tmp/cli.tgz" -C "$tmp" package/claude
mkdir -p "$dest"                      # not `install -D`: BSD install (macOS) has no -D, and developers run this too
install -m 0755 "$tmp/package/claude" "$dest/claude"
echo "claude $version ($arch) -> $dest/claude"
