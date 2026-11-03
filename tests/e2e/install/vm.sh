#!/usr/bin/env bash
# Boot a fresh Debian 13 (trixie) cloud VM on this machine with KVM — Debian's own generic cloud image, its own kernel,
# systemd and AppArmor, as a new server has them — copy the checkout in and run host.sh there as root.
# For .github/workflows/install.yml; on a CI runner, not on a laptop (it needs /dev/kvm and installs QEMU).
#
#   bash tests/e2e/install/vm.sh <work dir>
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../../.." && pwd)
WORK=$(mkdir -p "${1:?usage: vm.sh <work dir>}" && cd "$1" && pwd)
case $(uname -m) in
  x86_64) ARCH=amd64 QEMU=(qemu-system-x86_64 -machine q35,accel=kvm) PACKAGES=(qemu-system-x86) ;;
  aarch64) ARCH=arm64 QEMU=(qemu-system-aarch64 -machine virt,accel=kvm -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd)
           PACKAGES=(qemu-system-arm qemu-efi-aarch64) ;;
  *) echo "vm.sh: no Debian cloud image for $(uname -m)" >&2; exit 1 ;;
esac
IMAGE=debian-13-genericcloud-$ARCH.qcow2
IMAGES=https://cloud.debian.org/images/cloud/trixie/latest
SSH=(ssh -i "$WORK/key" -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
     -o ServerAliveInterval=30 -o ConnectTimeout=5 debian@127.0.0.1)
step() { printf '\n== %s\n' "$*"; }

[[ -e /dev/kvm ]] || { echo "vm.sh: no /dev/kvm on this runner" >&2; exit 1; }
step "QEMU and the Debian 13 image ($IMAGE, today's, checked against Debian's SHA512SUMS)"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${PACKAGES[@]}" qemu-utils cloud-image-utils
sudo chmod 666 /dev/kvm
curl -fsSL -o "$WORK/SHA512SUMS" "$IMAGES/SHA512SUMS"
curl -fsSL -o "$WORK/$IMAGE" "$IMAGES/$IMAGE"
(cd "$WORK" && grep " $IMAGE\$" SHA512SUMS | sha512sum -c -)

step "a fresh VM: $(nproc) CPUs, the disk grown to 60 GB by cloud-init, a key for ssh"
qemu-img create -q -f qcow2 -b "$WORK/$IMAGE" -F qcow2 "$WORK/disk.qcow2" 60G
ssh-keygen -q -t ed25519 -N '' -f "$WORK/key"
cat > "$WORK/user-data" <<EOF
#cloud-config
users:
  - name: debian
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys: ["$(cat "$WORK/key.pub")"]
EOF
printf 'instance-id: retinue-rehearsal\nlocal-hostname: retinue\n' > "$WORK/meta-data"
cloud-localds "$WORK/seed.img" "$WORK/user-data" "$WORK/meta-data"
memory=$(( $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo) - 1536 ))
"${QEMU[@]}" -cpu host -smp "$(nproc)" -m "$memory" -display none -daemonize -pidfile "$WORK/qemu.pid" \
  -serial "file:$WORK/serial.log" \
  -drive "if=virtio,file=$WORK/disk.qcow2" -drive "if=virtio,format=raw,file=$WORK/seed.img" \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=net0
for _ in $(seq 60); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done
"${SSH[@]}" 'cloud-init status --wait >/dev/null; . /etc/os-release; echo "$PRETTY_NAME, $(uname -r), $(free -m | awk "/Mem/ {print \$2}") MiB, $(df -h / | awk "NR==2 {print \$2}")"'

step "the checkout into /opt/retinue, then host.sh as root"
git -C "$REPO" archive --format=tar HEAD | "${SSH[@]}" 'sudo install -d /opt/retinue && sudo tar -x -C /opt/retinue'
"${SSH[@]}" 'sudo bash /opt/retinue/tests/e2e/install/host.sh /opt/retinue'
