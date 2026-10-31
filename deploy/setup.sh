#!/usr/bin/env bash
# Prepare the host for the Retinue stack: /srv/retinue with the router config, agent files and secrets.
# Safe to run again after `git pull`.
#
#   sudo TELEGRAM_OWNER_ID=123456789 bash deploy/setup.sh
#   sudo TELEGRAM_OWNER_ID=123456789 OWNER_TZ=Europe/Berlin bash deploy/setup.sh   # default: the zone kept, else UTC
#   sudo TELEGRAM_OWNER_ID=123456789 MATRIX_SERVER_NAME=matrix.example.com MATRIX_OWNER=alice bash deploy/setup.sh
#   sudo TELEGRAM_OWNER_ID=123456789 ROTATE_BUS_SECRET=1 bash deploy/setup.sh   # a new bus secret and agent tokens
#   sudo TELEGRAM_OWNER_ID=123456789 TRAVEL=1 bash deploy/setup.sh   # travel-ops: trip search and price watches
#   sudo TELEGRAM_OWNER_ID=123456789 MEMORY=1 bash deploy/setup.sh   # the knowledge base she writes, and its hub
#   sudo TELEGRAM_OWNER_ID=123456789 MAIL=1 bash deploy/setup.sh     # the mail collector: Gmail and Google Calendar
#   sudo TELEGRAM_OWNER_ID=123456789 PAGES=1 PAGES_HOST=pages.in.example.com DEVICES=10.8.0.2/32,10.8.0.3/32 \
#     PAGES_PUBLIC_HOSTS=pages.example.com bash deploy/setup.sh     # your HTML pages, and the ones you share
#   sudo TELEGRAM_OWNER_ID=123456789 TGBUSINESS=1 bash deploy/setup.sh   # your chosen Telegram chats, replies by card
#   sudo TELEGRAM_OWNER_ID=123456789 SEND=1 bash deploy/setup.sh     # replies by mail (needs MAIL=1), by card
#   sudo TELEGRAM_OWNER_ID=123456789 PLATFORM=1 bash deploy/setup.sh # Claude Code sessions in the room, from Telegram
#   sudo TELEGRAM_OWNER_ID=123456789 PLATFORM=1 FACE=1 FACE_HOST=app.in.example.com bash deploy/setup.sh
#                                                                     # the platform's app, over WireGuard only
#   sudo TELEGRAM_OWNER_ID=123456789 BACKUP=1 bash deploy/setup.sh   # the nightly restic backup (docs/backup.md)
#
# The stack's environment goes to /srv/retinue/stack.env (mode 600). The script prints names, never values:
# a terminal ends up in logs and transcripts.
# At least one channel is required. Everything Matrix needs (appservice registration, Traefik middleware,
# split DNS) is made only when MATRIX_SERVER_NAME is set.
set -euo pipefail

TELEGRAM_OWNER_ID=${TELEGRAM_OWNER_ID:-}
MATRIX_SERVER_NAME=${MATRIX_SERVER_NAME:-}
MATRIX_OWNER=${MATRIX_OWNER:-}
if [[ -z $TELEGRAM_OWNER_ID && -z $MATRIX_SERVER_NAME ]]; then
  echo "no channel: set TELEGRAM_OWNER_ID (your Telegram user id) or MATRIX_SERVER_NAME and MATRIX_OWNER" >&2
  exit 1
fi
if [[ -n $MATRIX_SERVER_NAME && -z $MATRIX_OWNER ]]; then
  echo "MATRIX_OWNER is required with MATRIX_SERVER_NAME: the owner's localpart, e.g. alice" >&2
  exit 1
fi

REPO=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${RETINUE_ROOT:-/srv/retinue}      # tests point this at a temporary folder
# The owner's time zone: reminders, the morning summary, every time shown, the nightly backup. A run without OWNER_TZ
# keeps the zone router.yaml already has; the first one without it takes UTC.
OWNER_TZ=${OWNER_TZ:-$(sed -n 's/^owner_tz: *//p' "$ROOT/router.yaml" 2>/dev/null | head -n 1 || true)}
OWNER_TZ=${OWNER_TZ:-UTC}
HOST_SETUP=${RETINUE_HOST_SETUP:-1}     # 0: write files under ROOT only; no Traefik or split DNS (tests)
ROTATE_BUS_SECRET=${ROTATE_BUS_SECRET:-0}
BACKUP=${BACKUP:-0}                     # 1: nightly restic backup to S3; on for good once backup.env exists
TRAVEL=${TRAVEL:-0}                     # 1: travel-ops; on for good once travel/profile.yml exists
TRAVEL_ON=0
[[ $TRAVEL == 1 || -f $ROOT/travel/profile.yml ]] && TRAVEL_ON=1
TRAVEL_URL=http://travel-ops:8765/mcp
MEMORY=${MEMORY:-0}                     # 1: the knowledge base and its hub; on for good once the hub exists
MEMORY_ON=0
MEMORY_DIR=$ROOT/memory
HUB=$MEMORY_DIR/hub.git
[[ $MEMORY == 1 || -d $HUB ]] && MEMORY_ON=1
MEMORY_UID=10001                        # the containers' user (Dockerfile): the router writes the hub as it
MAIL=${MAIL:-0}                         # 1: the mail collector; on for good once its folder exists
MAIL_DIR=$ROOT/mail
MAIL_ON=0
[[ $MAIL == 1 || -d $MAIL_DIR ]] && MAIL_ON=1
MAIL_UID=10002                          # the collector's own user: the owner's tokens are its alone
PAGES=${PAGES:-0}                       # 1: the pages service; on for good once its address is written
PAGES_DIR=$ROOT/pages
PAGES_ON=0
[[ $PAGES == 1 || -f $PAGES_DIR/host ]] && PAGES_ON=1
PAGES_UID=10021                         # the pages service's own user: its store and the public sites' files
LIFEHUB_DIR=$ROOT/lifehub               # the retired life hub: its list of devices is read once, its files are the owner's
NGINX_UID=101                           # nginx-unprivileged's user: it reads the rendered nginx.conf
TGBUSINESS=${TGBUSINESS:-0}             # 1: the Telegram Business gateway; on for good once its folder exists
TG_DIR=$ROOT/telegram
TG_ON=0
[[ $TGBUSINESS == 1 || -d $TG_DIR ]] && TG_ON=1
TG_UID=10003                            # the gateway's own user: the Business bot's state is its alone
GATEWAY_URL=http://tg-business:9300
SEND=${SEND:-0}                         # 1: the mail sender; on for good once its folder exists
SEND_DIR=$ROOT/send
SEND_ON=0
[[ $SEND == 1 || -d $SEND_DIR ]] && SEND_ON=1
SEND_UID=10004                          # the sender's own user: the gmail.send tokens are its alone
SENDER_URL=http://mail-send:9400
PLATFORM=${PLATFORM:-0}                 # 1: the platform and its lanes; on for good once its folder exists
PLATFORM_DIR=$ROOT/platform
PLATFORM_ON=0
[[ $PLATFORM == 1 || -d $PLATFORM_DIR ]] && PLATFORM_ON=1
PLATFORM_URL=http://platform:9500
LANES=(private code web)
BASE_GID=10020                          # retinue-base: lane-private (10005) and the platform (10007) share the clone
FACE=${FACE:-0}                         # 1: the face, the platform's app; on for good once its address is written
FACE_DIR=$ROOT/face
FACE_ON=0
[[ $FACE == 1 || -f $FACE_DIR/host ]] && FACE_ON=1
if [[ $PLATFORM_ON == 1 && -z $TELEGRAM_OWNER_ID ]]; then
  echo "PLATFORM=1 needs TELEGRAM_OWNER_ID: the sessions' cards and your replies go through the Telegram bot" >&2
  exit 1
fi
if [[ $FACE_ON == 1 && $PLATFORM_ON == 0 ]]; then
  echo "FACE=1 needs PLATFORM=1: the face is the platform's app, its API is the platform's" >&2
  exit 1
fi
if [[ $TG_ON == 1 && -z $TELEGRAM_OWNER_ID ]]; then
  echo "TGBUSINESS=1 needs TELEGRAM_OWNER_ID: only your own account's connection counts" >&2
  exit 1
fi
if [[ $SEND_ON == 1 && $MAIL_ON == 0 ]]; then
  echo "SEND=1 needs MAIL=1: a reply's address and thread come from the letters the collector kept" >&2
  exit 1
fi
COLLECTOR_URL=http://collector:9200
AGENTS=(assistant)
token() { openssl rand -hex 32; }
upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }
# A list with an entry in it: a line that is neither blank nor a comment.
has_entry() { [[ -f $1 ]] && grep -qvE '^[[:space:]]*(#|$)' "$1"; }
# The open proxy's list of this server's addresses: the example's comments, then (on the host) its public addresses.
fill_open_ips() {
  cat "$REPO/deploy/egress/open-deny-ips.txt"
  [[ $HOST_SETUP == 1 ]] || return 0
  ip -o addr show scope global | awk '{print $4}' | python3 -c '
import ipaddress, sys
for line in sys.stdin:
    iface = ipaddress.ip_interface(line.strip())
    if iface.ip.is_global:
        print(iface.network if iface.version == 6 else f"{iface.ip}/32")'
}
OPEN_CHANGED=0

install -d -m 755 "$ROOT"
# The pages' and the face's addresses and the owner's devices: given once, checked — all of them before anything is
# kept — then kept here (the owner's to edit). Both are behind Traefik for these devices only.
host_name() {   # $1: the switch, $2: the variable's name, $3: its value, $4: an example
  [[ -n $3 ]] || { echo "$1=1 needs $2: the name, e.g. $4" >&2; return 1; }
  [[ $3 =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || {
    echo "$2: a host name, e.g. $4" >&2; return 1; }
}
devices() {     # $1: the switch, $2: the variable's name, $3: its value; prints one a.b.c.d/32 per line
  local list device
  list=$(tr ', ' '\n\n' <<< "$3" | sed '/^$/d')
  [[ -n $list ]] || {
    echo "$1=1 needs $2: your devices' WireGuard addresses, e.g. 10.8.0.2/32,10.8.0.3/32" >&2; return 1; }
  while read -r device; do
    [[ $device =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]] || {
      echo "$2: one address per device, a.b.c.d/32 — not $device" >&2; return 1; }
  done <<< "$list"
  printf '%s\n' "$list"
}
# The owner's devices: one list for every host behind the VPN (the face, the pages) in $ROOT/devices.txt — given as
# DEVICES (FACE_DEVICES and LIFEHUB_DEVICES: earlier names), else the list kept, else an earlier version's.
DEVICES_FILE=$ROOT/devices.txt
if [[ $PAGES_ON == 1 || $FACE_ON == 1 ]]; then
  DEVICES=${DEVICES:-${FACE_DEVICES:-${LIFEHUB_DEVICES:-}}}
  for kept in "$DEVICES_FILE" "$LIFEHUB_DIR/devices.txt" "$FACE_DIR/devices.txt"; do
    [[ -n $DEVICES ]] && break
    DEVICES=$(paste -sd, "$kept" 2>/dev/null || true)
  done
  DEVICE_LIST=$(devices "PAGES or FACE" DEVICES "$DEVICES") || exit 1
fi
if [[ $PAGES_ON == 1 ]]; then
  PAGES_HOST=${PAGES_HOST:-$(cat "$PAGES_DIR/host" 2>/dev/null || true)}
  host_name PAGES PAGES_HOST "$PAGES_HOST" pages.in.example.com || exit 1
  # The public sites a page may be shared on: none until named; an empty PAGES_PUBLIC_HOSTS= takes them all away.
  PAGES_PUBLIC_HOSTS=${PAGES_PUBLIC_HOSTS-$(paste -sd, "$PAGES_DIR/public_hosts.txt" 2>/dev/null || true)}
  PUBLIC_LIST=$(tr ', ' '\n\n' <<< "$PAGES_PUBLIC_HOSTS" | sed '/^$/d')
  while read -r public; do
    [[ -n $public ]] || continue
    host_name PAGES PAGES_PUBLIC_HOSTS "$public" pages.example.com || exit 1
    [[ $public != "$PAGES_HOST" && $public != *.in.* ]] || {
      echo "PAGES_PUBLIC_HOSTS: $public is a name behind the VPN — a public site needs a name of its own" >&2; exit 1; }
  done <<< "$PUBLIC_LIST"
fi
if [[ $FACE_ON == 1 ]]; then
  FACE_HOST=${FACE_HOST:-$(cat "$FACE_DIR/host" 2>/dev/null || true)}
  host_name FACE FACE_HOST "$FACE_HOST" app.in.example.com || exit 1
fi
if [[ $PAGES_ON == 1 || $FACE_ON == 1 ]]; then
  printf '%s\n' "$DEVICE_LIST" > "$DEVICES_FILE"
  rm -f "$FACE_DIR/devices.txt"         # one list: no second one to forget
fi
if [[ $PAGES_ON == 1 ]]; then
  install -d -m 755 "$PAGES_DIR"
  printf '%s\n' "$PAGES_HOST" > "$PAGES_DIR/host"
  printf '%s\n' "$PUBLIC_LIST" | sed '/^$/d' > "$PAGES_DIR/public_hosts.txt"
fi
if [[ $FACE_ON == 1 ]]; then
  install -d -m 755 "$FACE_DIR"
  printf '%s\n' "$FACE_HOST" > "$FACE_DIR/host"
fi
# The knowledge base. The mounts exist whether it is on or not, so the folders and the policy always do; what she
# may write and see is the owner's (written once from the examples, then edited by hand).
install -d -m 755 "$MEMORY_DIR" "$MEMORY_DIR/tree" "$MEMORY_DIR/git"
[[ -f $MEMORY_DIR/policy.json ]] || install -m 644 "$REPO/deploy/memory/policy.example.json" "$MEMORY_DIR/policy.json"
[[ -f $MEMORY_DIR/checkout.txt ]] || install -m 644 "$REPO/deploy/memory/checkout.example.txt" "$MEMORY_DIR/checkout.txt"
[[ -f $MEMORY_DIR/sensitive.txt ]] || install -m 644 "$REPO/deploy/memory/sensitive.example.txt" "$MEMORY_DIR/sensitive.txt"
if [[ -f $MEMORY_DIR/owner.yaml ]] && grep -vE '^([[:space:]]|#|$|(location|private|tracker):)' "$MEMORY_DIR/owner.yaml" >/dev/null; then
  echo "$MEMORY_DIR/owner.yaml: only the keys location, private and tracker go there (docs/memory.md)" >&2; exit 1
fi
SECRETS="$ROOT/secrets.env"
[[ -f $SECRETS ]] || (umask 077; : > "$SECRETS")
secret() { grep -q "^$1=" "$SECRETS" || echo "$1=$(token)" >> "$SECRETS"; }   # made once, then kept
drop() { local rest; rest=$(grep -v "^$1=" "$2" || true); printf '%s\n' "$rest" | sed '/^$/d' > "$2"; }
[[ $ROTATE_BUS_SECRET == 1 ]] && drop RETINUE_BUS_SECRET "$SECRETS"   # the agents' tokens follow from it
secret RETINUE_BUS_SECRET
[[ $MAIL_ON == 1 ]] && secret RETINUE_COLLECTOR_TOKEN   # the router signs its calls to the collector with it
[[ $PAGES_ON == 1 ]] && secret PAGES_KEY   # Traefik adds it to the pages' requests; the service refuses any without it
[[ $TG_ON == 1 ]] && secret RETINUE_GATEWAY_TOKEN   # the router signs its calls to the Telegram Business gateway
[[ $SEND_ON == 1 ]] && secret RETINUE_SENDER_TOKEN  # the router signs its calls to the mail sender
if [[ $PLATFORM_ON == 1 ]]; then
  secret RETINUE_PLATFORM_TOKEN         # the router signs its calls to the platform
  for lane in "${LANES[@]}"; do secret "RETINUE_LANE_TOKEN_$(upper "$lane")"; done   # the platform's, to each lane
  secret RETINUE_HOOK_SECRET
fi
[[ $FACE_ON == 1 ]] && secret FACE_KEY   # Traefik adds it to the face's requests; nginx refuses any without it
if [[ -n $MATRIX_SERVER_NAME ]]; then
  secret RETINUE_AS_TOKEN
  secret RETINUE_HS_TOKEN
  secret MATRIX_REGISTRATION_TOKEN
fi
source "$SECRETS"
bus_token() { printf 'retinue-bus:%s' "$1" | openssl dgst -sha256 -hmac "$RETINUE_BUS_SECRET" -r | cut -d' ' -f1; }

{
  cat <<YAML
owner: owner
owner_tz: $OWNER_TZ
default_agent: assistant
agents:
  - id: assistant
    name: Assistant
    url: http://assistant:9000
    description: The system's one face
    trust_class: private
    archive: true     # may search the raw archive through the bus
    reminders: true   # may set, list, move and cancel the owner's reminders through the bus
    attachments: true # may fetch what the owner sent (#N) again through the bus
    trips: true       # may publish a trip page in the life hub (refused while there is no hub)
    drafts: true      # may propose a reply to a person; it leaves only by your «Send» (refused with no sender)
    can_call: []      # no other agents at this stage
YAML
  if [[ $TRAVEL_ON == 1 ]]; then
    printf 'travel_url: %s\n' "$TRAVEL_URL"      # the router collects price alerts there
    printf 'link_hosts:\n'                       # travel-ops' sites: a link there is clickable in Telegram
    sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e "s/^[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/  - '\1'/" \
      "$REPO/deploy/travel/link-hosts.txt"
  fi
  if [[ $MAIL_ON == 1 ]]; then
    printf 'collector_url: %s\n' "$COLLECTOR_URL"   # what the collector found, the router takes there
  fi
  if [[ $PAGES_ON == 1 ]]; then
    printf 'pages_url: https://%s\n' "$PAGES_HOST"   # the pages' address; the data goes to /pages
  fi
  if [[ $TG_ON == 1 ]]; then
    printf 'gateway_url: %s\n' "$GATEWAY_URL"      # your chosen Telegram chats; replies sent by your card
  fi
  if [[ $SEND_ON == 1 ]]; then
    printf 'sender_url: %s\n' "$SENDER_URL"        # letters sent by your card
  fi
  if [[ $PLATFORM_ON == 1 ]]; then
    printf 'platform_url: %s\n' "$PLATFORM_URL"    # the lanes' sessions: their cards, your replies and presses
  fi
  if [[ $FACE_ON == 1 ]]; then
    printf 'face_url: https://%s\n' "$FACE_HOST"   # /app logs you in there; a card opens there as a Mini App
  fi
  if [[ $MEMORY_ON == 1 ]]; then
    printf 'memory:\n  hub: /hub\n  checkout:\n'   # the rest: defaults in config.py (MemoryConfig)
    sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e "s/^[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/    - '\1'/" \
      "$MEMORY_DIR/checkout.txt"
    printf '  sensitive:\n'                       # closed Spaces: a draft's card is louder when she read them
    sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e "s/^[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/    - '\1'/" \
      "$MEMORY_DIR/sensitive.txt"
    if [[ -f $MEMORY_DIR/owner.yaml ]]; then        # your own words for what the code keeps generic (docs/memory.md)
      sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' -e 's/^/  /' "$MEMORY_DIR/owner.yaml"
    fi
  fi
  if [[ -n $TELEGRAM_OWNER_ID ]]; then
    printf 'telegram:\n  owner_id: %s\n' "$TELEGRAM_OWNER_ID"
  fi
  if [[ -n $MATRIX_SERVER_NAME ]]; then
    cat <<YAML
matrix:
  homeserver: http://tuwunel:6167
  server_name: $MATRIX_SERVER_NAME
  owner: "@$MATRIX_OWNER:$MATRIX_SERVER_NAME"
YAML
  fi
} > "$ROOT/router.yaml"
chmod 644 "$ROOT/router.yaml"

for agent in "${AGENTS[@]}"; do
  # Config and instructions, always as they are in the repo: the container mounts this folder read-only.
  install -d -m 755 "$ROOT/agents/$agent"
  install -m 644 "$REPO/agents/$agent/agent.yaml" "$REPO/agents/$agent/CLAUDE.md" "$ROOT/agents/$agent/"
done

install -d -m 755 "$ROOT/status"       # the host writes, the router reads (mounted read-only)

PROFILE="$ROOT/travel/profile.yml"
if [[ $TRAVEL_ON == 1 ]]; then
  install -d -m 755 "$ROOT/travel"
  # The owner's home and party: written by the owner, never by this script and never in git.
  [[ -f $PROFILE ]] || printf '%s\n' "# travel-ops profile: home_airports, travellers, currency, stays." \
    "# The fields are in profile.example.yml of travel-ops. Without home_airports the assistant asks." > "$PROFILE"
  chmod 644 "$PROFILE"   # read by travel-ops' own user in its container
fi

if [[ $MEMORY_ON == 1 ]]; then
  # The hub: every copy pushes here, and its pre-receive checks every push with the same rules (kbcheck.py).
  # A push writes objects/ and refs/ and nothing else: no gc after it (it would write packed-refs and gc.* in the
  # hub's own folder), no reflogs. So only objects/ and refs/ are the group's. The hub's folder and its config are
  # the owner's (git refuses a repository its user does not own, and he pushes over ssh) and not the group's;
  # hooks/ is root's. The containers' user can push, but cannot change what checks a push (core.hooksPath,
  # receive.*) or move hooks/ away.
  [[ -f $HUB/HEAD ]] || git init -q --bare -b main "$HUB"
  for setting in "core.sharedRepository group" "receive.denyNonFastForwards true" "receive.denyDeletes true" \
                 "receive.autogc false" "gc.auto 0" "core.logAllRefUpdates false"; do
    git -C "$HUB" config ${setting}
  done
  # HEAD names no branch: a push to the branch HEAD names also locks HEAD here, in the folder the group cannot
  # write (seen on the server: «cannot lock ref 'HEAD'»). Clone with -b main.
  git -C "$HUB" symbolic-ref HEAD refs/heads/none
  rm -rf "$HUB/kbcheck"                 # an earlier version kept the lint's copies here, writable by the group
  install -m 755 "$REPO/retinue/kbcheck.py" "$HUB/hooks/pre-receive"
  install -m 644 "$MEMORY_DIR/policy.json" "$HUB/hooks/kb-policy.json"
  chmod -R go-w "$HUB"
  chmod -R g+rwX "$HUB/objects" "$HUB/refs"
  find "$HUB/objects" "$HUB/refs" -type d -exec chmod g+s {} +
fi
if [[ $HOST_SETUP == 1 ]]; then
  # The containers' user owns the working copy; the hub's objects/ and refs/ are shared with the owner through
  # the group of that uid; the rest of the hub is the owner's, its hooks root's.
  chown -R "$MEMORY_UID:$MEMORY_UID" "$MEMORY_DIR/tree" "$MEMORY_DIR/git"
  chmod 700 "$MEMORY_DIR/git"
  if [[ $MEMORY_ON == 1 ]]; then
    getent group "$MEMORY_UID" >/dev/null || groupadd -g "$MEMORY_UID" retinue-memory
    [[ -n ${SUDO_USER:-} ]] && usermod -aG "$(getent group "$MEMORY_UID" | cut -d: -f1)" "$SUDO_USER"
    chown -R "${SUDO_USER:-root}:$MEMORY_UID" "$HUB"
    chown -R root:root "$HUB/hooks"
  fi
fi

# The pages: the router writes the data and the builder the releases (both as the containers' user); the service
# writes the public sites' files (its own user; its store is the volume pages-data, in the nightly backup). The router mounts its folders whether pages are
# on or not.
install -d -m 755 "$PAGES_DIR" "$PAGES_DIR/data" "$PAGES_DIR/site" "$PAGES_DIR/public"
if [[ $HOST_SETUP == 1 ]]; then
  chown "$MEMORY_UID:$MEMORY_UID" "$PAGES_DIR/data" "$PAGES_DIR/site"
  chown "$PAGES_UID:$PAGES_UID" "$PAGES_DIR/public"
fi
if [[ $PAGES_ON == 1 ]]; then
  RANGES=$(sed 's/.*/"&"/' "$DEVICES_FILE" | paste -sd, - | sed 's/,/, /g')
  (umask 077; sed -e "s|\${PAGES_KEY}|$PAGES_KEY|" -e "s|\${PAGES_RANGES}|$RANGES|" \
    "$REPO/deploy/pages/traefik.yml.template" > "$PAGES_DIR/traefik.yml")
  chmod 600 "$PAGES_DIR/traefik.yml"
  [[ $HOST_SETUP == 1 ]] && install -m 600 "$PAGES_DIR/traefik.yml" /etc/dokploy/traefik/dynamic/pages.yml
fi
# The face: its Traefik middlewares (the devices, the key) and nginx's check of the key, both rendered on every run —
# a changed list of devices reaches Traefik with the next run. nginx's own config is in the image.
if [[ $FACE_ON == 1 ]]; then
  RANGES=$(sed 's/.*/"&"/' "$DEVICES_FILE" | paste -sd, - | sed 's/,/, /g')
  (umask 077; sed -e "s|\${FACE_KEY}|$FACE_KEY|" -e "s|\${FACE_RANGES}|$RANGES|" \
    "$REPO/deploy/face/traefik.yml.template" > "$FACE_DIR/traefik.yml")
  chmod 600 "$FACE_DIR/traefik.yml"
  (umask 027; printf 'if ($http_x_face_key != "%s") { return 404; }\n' "$FACE_KEY" > "$FACE_DIR/key.conf")
  chmod 640 "$FACE_DIR/key.conf"
  if [[ $HOST_SETUP == 1 ]]; then
    chown "root:$NGINX_UID" "$FACE_DIR/key.conf"
    install -m 600 "$FACE_DIR/traefik.yml" /etc/dokploy/traefik/dynamic/face.yml   # Traefik runs as root
  fi
fi

install -d -m 755 "$ROOT/egress"
install -m 644 "$REPO/deploy/egress/squid.conf" "$ROOT/egress/squid.conf"
if [[ $MAIL_ON == 1 ]]; then
  # The owner's tokens (deploy/mail/login.py puts them here) and the collector's own state: its user's alone.
  install -d -m 700 "$MAIL_DIR" "$MAIL_DIR/keys" "$MAIL_DIR/state"
  [[ $HOST_SETUP == 1 ]] && chown -R "$MAIL_UID:$MAIL_UID" "$MAIL_DIR"
  # The collector's way out, like the assistant's list: written once, then the owner's.
  [[ -f $ROOT/egress/mail-hosts.txt ]] || install -m 644 "$REPO/deploy/egress/mail-hosts.txt" "$ROOT/egress/mail-hosts.txt"
fi
if [[ $SEND_ON == 1 ]]; then
  # The owner's gmail.send tokens (deploy/mail/login.py --kind send puts them here) and the sender's ledger.
  install -d -m 700 "$SEND_DIR" "$SEND_DIR/keys" "$SEND_DIR/state"
  [[ $HOST_SETUP == 1 ]] && chown -R "$SEND_UID:$SEND_UID" "$SEND_DIR"
fi
if [[ $TG_ON == 1 ]]; then
  install -d -m 700 "$TG_DIR" "$TG_DIR/state"   # the gateway's state: its user's alone
  [[ $HOST_SETUP == 1 ]] && chown -R "$TG_UID:$TG_UID" "$TG_DIR"
fi
if [[ $PLATFORM_ON == 1 ]]; then
  # The lanes and the owner's tracks with their always rules: written once from the example, then the owner's.
  install -d -m 755 "$PLATFORM_DIR"
  [[ -f $PLATFORM_DIR/platform.yaml ]] || \
    install -m 644 "$REPO/deploy/platform/platform.example.yaml" "$PLATFORM_DIR/platform.yaml"
  # The owner's skills, read-only in the room: empty until deploy/skills/install-skills.sh fills it.
  install -d -m 755 "$ROOT/skills"
  # The owner's memory snapshot, read-only in lane-private: empty until deploy/memory/install-owner-memory.sh fills it.
  install -d -m 755 "$ROOT/owner-memory"
  # The Base's clone (docs/base.md): lane-private writes its files, the platform puts the hub's changes over clean
  # ones, both through the group retinue-base. The folder itself is root's, setgid and the group's: no session can
  # take the group's write away from it, and everything made inside takes the group. Only this folder is touched:
  # what is inside is the lane's, and root never walks a tree a session can rearrange. Empty until `base:` is in
  # platform.yaml; the platform clones into it.
  install -d -m 755 "$ROOT/base"
  install -d -m 2775 "$ROOT/base/tree"
  if [[ $HOST_SETUP == 1 ]]; then
    getent group "$BASE_GID" >/dev/null || groupadd -g "$BASE_GID" retinue-base
    chown "root:$BASE_GID" "$ROOT/base/tree"
    chmod 2775 "$ROOT/base/tree"
  fi
  # The retired lane-code's way out (profile legacy-lanes), like the assistant's list: written once, then the owner's.
  [[ -f $ROOT/egress/lane-code-hosts.txt ]] || \
    install -m 644 "$REPO/deploy/egress/lane-code-hosts.txt" "$ROOT/egress/lane-code-hosts.txt"
  # The owner's repositories (docs/code.md): the platform's mirrors, read-only in lane-private, so world-readable;
  # the git tokens, root's alone (deploy/code/set-git-token.sh writes them; compose hands them to the platform);
  # the platform's way out to GitHub and GitLab, written once, then the owner's.
  install -d -m 755 "$ROOT/code"
  install -d -m 755 "$ROOT/code/mirrors"
  [[ $HOST_SETUP == 1 ]] && chown 10007:10007 "$ROOT/code/mirrors"
  [[ -f $ROOT/code/tokens.env ]] || (umask 077; : > "$ROOT/code/tokens.env")
  chmod 600 "$ROOT/code/tokens.env"
  [[ -f $ROOT/egress/platform-hosts.txt ]] || \
    install -m 644 "$REPO/deploy/egress/platform-hosts.txt" "$ROOT/egress/platform-hosts.txt"
  # The owner's voice in the app (docs/face.md «Voice»): ElevenLabs' API added to a list written before it — once,
  # remembered by a marker, so a line the owner removes later stays removed. A last line without its newline gets one.
  HOSTS=$ROOT/egress/platform-hosts.txt
  if [[ ! -e $ROOT/egress/.platform-hosts-speech ]]; then
    if ! grep -qx 'api.elevenlabs.io' "$HOSTS"; then
      [[ -s $HOSTS && -n $(tail -c 1 "$HOSTS") ]] && echo >> "$HOSTS"
      echo 'api.elevenlabs.io' >> "$HOSTS"
    fi
    : > "$ROOT/egress/.platform-hosts-speech"
  fi
  # The open proxy, the room's way out (lane-private-egress): its rules and the ranges no public host lives in are
  # ours, replaced every run; this server's public addresses and the owner's own domains are written once, then the
  # owner's. A compose `up` before this ever ran left Docker's stand-in for each missing bind source: an empty
  # directory. It goes, and the file is written; a directory with something in it stops the run (rmdir fails) rather
  # than be guessed at.
  OPEN_FILES=(open-squid.conf open-deny-nets.txt open-deny-ips.txt open-deny-domains.txt)
  open_before=$(cd "$ROOT/egress" && { cat "${OPEN_FILES[@]}" 2>/dev/null || true; } | cksum)
  for name in "${OPEN_FILES[@]}"; do
    if [[ -d $ROOT/egress/$name ]]; then rmdir "$ROOT/egress/$name"; fi
  done
  # The owner's two lists were lane-web's before the room: taken over once, as they are.
  for kind in ips domains; do
    if [[ ! -e $ROOT/egress/open-deny-$kind.txt ]] && has_entry "$ROOT/egress/lane-web-deny-$kind.txt"; then
      cp -p "$ROOT/egress/lane-web-deny-$kind.txt" "$ROOT/egress/open-deny-$kind.txt"
    fi
  done
  install -m 644 "$REPO/deploy/egress/open-squid.conf" "$ROOT/egress/open-squid.conf"
  install -m 644 "$REPO/deploy/egress/open-deny-nets.txt" "$ROOT/egress/open-deny-nets.txt"
  [[ -f $ROOT/egress/open-deny-domains.txt ]] || \
    install -m 644 "$REPO/deploy/egress/open-deny-domains.txt" "$ROOT/egress/open-deny-domains.txt"
  # This server's own public addresses, written whole or not at all (a temporary file, then moved into place; gone
  # if the filling fails), and again on every run while the list has no entry: an IPv4 address alone, an IPv6 one
  # with its prefix (the host's /64).
  if ! has_entry "$ROOT/egress/open-deny-ips.txt"; then
    ips_tmp=$(mktemp "$ROOT/egress/.open-deny-ips.XXXXXX")
    if ! fill_open_ips > "$ips_tmp"; then
      rm -f "$ips_tmp"
      echo "open proxy: this server's addresses could not be listed (ip, python3)" >&2
      exit 1
    fi
    chmod 644 "$ips_tmp"
    mv -f "$ips_tmp" "$ROOT/egress/open-deny-ips.txt"
  fi
  open_after=$(cd "$ROOT/egress" && cat "${OPEN_FILES[@]}" | cksum)
  [[ $open_before == "$open_after" ]] || OPEN_CHANGED=1
  # The lanes' security profiles, ours and replaced every run: seccomp for compose, AppArmor loaded now and at boot.
  install -m 644 "$REPO/deploy/lanes/seccomp.json" "$PLATFORM_DIR/lane-seccomp.json"
  if [[ $HOST_SETUP == 1 ]]; then
    install -m 644 "$REPO/deploy/lanes/apparmor" /etc/apparmor.d/retinue-lane
    apparmor_parser -r /etc/apparmor.d/retinue-lane
  fi
fi
# The list of allowed hosts belongs to the owner: written once, then edited only by hand.
[[ -f $ROOT/egress/allowed-hosts.txt ]] || install -m 644 "$REPO/deploy/egress/allowed-hosts.txt" "$ROOT/egress/allowed-hosts.txt"

if [[ -n $MATRIX_SERVER_NAME ]]; then
  PRIVATE_SUFFIX=${PRIVATE_SUFFIX:-in.${MATRIX_SERVER_NAME#*.}}   # e.g. in.example.com for matrix.example.com
  ESCAPED=${MATRIX_SERVER_NAME//./\\.}
  install -d -m 755 "$ROOT/tuwunel/appservices"
  cat > "$ROOT/tuwunel/appservices/retinue.yaml" <<YAML
id: retinue
url: http://router:29400
as_token: $RETINUE_AS_TOKEN
hs_token: $RETINUE_HS_TOKEN
sender_localpart: retinue
rate_limited: false
namespaces:
  users:
    - exclusive: true
      regex: '^@agent_[a-z0-9_]+:$ESCAPED\$'
YAML
  chmod 644 "$ROOT/tuwunel/appservices/retinue.yaml"   # read by the homeserver container
  if [[ $HOST_SETUP == 1 ]]; then
    install -m 644 "$REPO/deploy/traefik-retinue.yml" /etc/dokploy/traefik/dynamic/retinue.yml
    install -d -m 755 /opt/split-dns
    install -m 644 "$REPO/deploy/split-dns/compose.yml" /opt/split-dns/compose.yml
    sed -e "s/\${MATRIX_SERVER_NAME}/$MATRIX_SERVER_NAME/" -e "s/\${PRIVATE_SUFFIX}/$PRIVATE_SUFFIX/" \
      "$REPO/deploy/split-dns/dnsmasq.conf.template" > /opt/split-dns/dnsmasq.conf
    docker compose -f /opt/split-dns/compose.yml up -d --force-recreate
  fi
fi

# The stack's environment: generated values are written (and replaced after a rotation), what the owner pastes is
# kept as it is. Nothing here is printed.
STACK="$ROOT/stack.env"
[[ -f $STACK ]] || (umask 077; : > "$STACK")
chmod 600 "$STACK"
WRITTEN=() MISSING=()
put() { drop "$1" "$STACK"; printf '%s=%s\n' "$1" "$2" >> "$STACK"; WRITTEN+=("$1"); }
keep() { local file=${3:-$STACK}; grep -q "^$1=" "$file" || printf '%s=%s\n' "$1" "$2" >> "$file"; }
fill() { local file=${3:-$STACK}; keep "$1" "" "$file"; grep -q "^$1=." "$file" || MISSING+=("$file: $1 ($2)"); }
put RETINUE_BUS_SECRET "$RETINUE_BUS_SECRET"
for a in "${AGENTS[@]}"; do put "RETINUE_BUS_TOKEN_$(upper "$a")" "$(bus_token "$a")"; done
fill CLAUDE_CODE_OAUTH_TOKEN "claude setup-token"
fill ELEVENLABS_API_KEY "elevenlabs.io: a key with speech_to_text only and a credit limit; empty = voice refused"
[[ -n $TELEGRAM_OWNER_ID ]] && fill TELEGRAM_BOT_TOKEN "@BotFather"
PROFILES=()
[[ -n $MATRIX_SERVER_NAME ]] && PROFILES+=(matrix)
[[ $TRAVEL_ON == 1 ]] && PROFILES+=(travel)
[[ $MAIL_ON == 1 ]] && PROFILES+=(mail)
[[ $PAGES_ON == 1 ]] && PROFILES+=(pages)
[[ $TG_ON == 1 ]] && PROFILES+=(tgbusiness)
[[ $SEND_ON == 1 ]] && PROFILES+=(send)
[[ $PLATFORM_ON == 1 ]] && PROFILES+=(platform)
[[ $FACE_ON == 1 ]] && PROFILES+=(face)
if (( ${#PROFILES[@]} )); then
  put COMPOSE_PROFILES "$(IFS=,; echo "${PROFILES[*]}")"
else
  drop COMPOSE_PROFILES "$STACK"
fi
if [[ $TRAVEL_ON == 1 ]]; then put RETINUE_TRAVEL_URL "$TRAVEL_URL"; else put RETINUE_TRAVEL_URL ""; fi
if [[ $MEMORY_ON == 1 ]]; then put RETINUE_MEMORY /kb; else put RETINUE_MEMORY ""; fi
if [[ $MAIL_ON == 1 ]]; then put RETINUE_COLLECTOR_TOKEN "$RETINUE_COLLECTOR_TOKEN"; else drop RETINUE_COLLECTOR_TOKEN "$STACK"; fi
# The pages: the owner's host, its key, the public sites — and Traefik's rule for them, one Host() each (a rule that
# matches nothing when there is none: the public nginx then answers no one).
PAGES_ENV=(PAGES_HOST PAGES_ORIGIN PAGES_KEY PAGES_PUBLIC_HOSTS PAGES_PUBLIC_RULE PAGES_APP_ORIGIN)
if [[ $PAGES_ON == 1 ]]; then
  put PAGES_HOST "$PAGES_HOST"
  put PAGES_ORIGIN "https://$PAGES_HOST"
  put PAGES_KEY "$PAGES_KEY"
  put PAGES_PUBLIC_HOSTS "$(paste -sd, "$PAGES_DIR/public_hosts.txt")"
  RULE=$(sed 's/.*/Host(`&`)/' "$PAGES_DIR/public_hosts.txt" | paste -sd'|' - | sed 's/|/ || /g')
  put PAGES_PUBLIC_RULE "${RULE:-Host(\`pages.invalid\`)}"
  if [[ $FACE_ON == 1 ]]; then put PAGES_APP_ORIGIN "https://$FACE_HOST"; else drop PAGES_APP_ORIGIN "$STACK"; fi
else
  for name in "${PAGES_ENV[@]}"; do drop "$name" "$STACK"; done
fi
for name in LIFEHUB_HOST LIFEHUB_PAGES_HOST; do drop "$name" "$STACK"; done   # the retired life hub's
# The gateway serves this account's connection and no other; the face's Mini App logs in this account and no other.
[[ $TG_ON == 1 || $FACE_ON == 1 ]] && put TELEGRAM_OWNER_ID "$TELEGRAM_OWNER_ID"
if [[ $TG_ON == 1 ]]; then
  put RETINUE_GATEWAY_TOKEN "$RETINUE_GATEWAY_TOKEN"
  fill TELEGRAM_BUSINESS_TOKEN "@BotFather: a new bot of its own with Secretary Mode on — not TELEGRAM_BOT_TOKEN"
else
  drop RETINUE_GATEWAY_TOKEN "$STACK"
fi
if [[ $SEND_ON == 1 ]]; then put RETINUE_SENDER_TOKEN "$RETINUE_SENDER_TOKEN"; else drop RETINUE_SENDER_TOKEN "$STACK"; fi
PLATFORM_SECRETS=(RETINUE_PLATFORM_TOKEN RETINUE_HOOK_SECRET)
for lane in "${LANES[@]}"; do PLATFORM_SECRETS+=("RETINUE_LANE_TOKEN_$(upper "$lane")"); done
for name in "${PLATFORM_SECRETS[@]}"; do
  if [[ $PLATFORM_ON == 1 ]]; then put "$name" "${!name}"; else drop "$name" "$STACK"; fi
done
# The face: its origin, and the key Telegram signs a Mini App's data with — HMAC_SHA256(key="WebAppData", msg=<bot
# token>), derived here on every run (a new bot token, a new key), so the platform never holds the token. The token
# goes to openssl on stdin, not in its arguments; neither it nor the key is printed.
WEBAPP_KEY_STATE=""
if [[ $FACE_ON == 1 ]]; then
  put FACE_HOST "$FACE_HOST"
  put RETINUE_FACE_ORIGIN "https://$FACE_HOST"
  BOT_TOKEN=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$STACK" | tail -n 1)
  BOT_TOKEN=${BOT_TOKEN%\"}; BOT_TOKEN=${BOT_TOKEN#\"}; BOT_TOKEN=${BOT_TOKEN%\'}; BOT_TOKEN=${BOT_TOKEN#\'}
  if [[ -n $BOT_TOKEN ]]; then
    put RETINUE_WEBAPP_KEY "$(printf '%s' "$BOT_TOKEN" | openssl dgst -sha256 -hmac WebAppData -r | cut -d' ' -f1)"
    WEBAPP_KEY_STATE=on
  else
    drop RETINUE_WEBAPP_KEY "$STACK"
    WEBAPP_KEY_STATE=off
  fi
  unset BOT_TOKEN
else
  for name in FACE_HOST RETINUE_FACE_ORIGIN RETINUE_WEBAPP_KEY; do drop "$name" "$STACK"; done
fi
# travel-ops is built from the owner's own checkout: its address is his, never this repository's.
[[ $TRAVEL_ON == 1 ]] && fill TRAVEL_OPS_GIT "travel-ops' git URL at a pinned commit: https://…/travel-ops.git#<sha>"
if [[ $TRAVEL_ON == 1 ]] && ! grep -q '^home_airports:' "$PROFILE"; then
  MISSING+=("$PROFILE: home_airports (your airports, IATA)")
fi
if [[ -n $MATRIX_SERVER_NAME ]]; then
  put MATRIX_SERVER_NAME "$MATRIX_SERVER_NAME"
  keep MATRIX_ALLOW_REGISTRATION true   # the owner turns it off after creating the account
  put MATRIX_REGISTRATION_TOKEN "$MATRIX_REGISTRATION_TOKEN"
  put RETINUE_AS_TOKEN "$RETINUE_AS_TOKEN"
  put RETINUE_HS_TOKEN "$RETINUE_HS_TOKEN"
fi
chmod 600 "$STACK"

# Backup: restic on the host, not in the stack — it must outlive a broken deploy, and its keys stay out of Dokploy.
BACKUP_ENV="$ROOT/backup.env"
RESTIC_VERSION=0.19.1
# restic_0.19.1_linux_amd64.bz2 in the release's SHA256SUMS, signed by CF8F18F2844575973F79D4E191A6868BD3F7A907
RESTIC_SHA256=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c
install_restic() {
  [[ $(/usr/local/bin/restic version 2>/dev/null) == "restic $RESTIC_VERSION "* ]] && return 0
  [[ $(uname -m) == x86_64 ]] || { echo "restic: only linux amd64 is pinned here; install restic $RESTIC_VERSION" >&2; return 1; }
  local tmp; tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/restic.bz2" \
    "https://github.com/restic/restic/releases/download/v$RESTIC_VERSION/restic_${RESTIC_VERSION}_linux_amd64.bz2"
  echo "$RESTIC_SHA256  $tmp/restic.bz2" | sha256sum -c --quiet -
  python3 -c 'import bz2, sys; sys.stdout.buffer.write(bz2.open(sys.argv[1]).read())' "$tmp/restic.bz2" > "$tmp/restic"
  install -m 755 "$tmp/restic" /usr/local/bin/restic
  rm -rf "$tmp"
}
BACKUP_STATE=""
if [[ $BACKUP == 1 || -f $BACKUP_ENV ]]; then
  [[ -f $BACKUP_ENV ]] || (umask 077; : > "$BACKUP_ENV")
  chmod 600 "$BACKUP_ENV"
  before=${#MISSING[@]}
  fill RESTIC_REPOSITORY "s3:https://<endpoint>/<bucket>/retinue" "$BACKUP_ENV"
  fill RESTIC_PASSWORD "from your password manager; keep a copy off this server" "$BACKUP_ENV"
  fill AWS_ACCESS_KEY_ID "the server's S3 key: no delete except locks/" "$BACKUP_ENV"
  fill AWS_SECRET_ACCESS_KEY "the same key's secret" "$BACKUP_ENV"
  fill AWS_DEFAULT_REGION "the endpoint's region, e.g. eu-luxembourg-1" "$BACKUP_ENV"
  if (( ${#MISSING[@]} == before )); then BACKUP_STATE=on; else BACKUP_STATE=unfilled; fi
  # With travel on, travel-ops' searches and the owner's watches are backed up too; with the platform on, its
  # database (the sessions' transcripts are there; the lanes' volumes are not backed up: their work trees are the
  # owner's checkouts). Without either, the script's own list.
  drop RETINUE_VOLUMES "$BACKUP_ENV"
  VOLUMES=(retinue_router-data retinue_assistant-data)
  [[ $TRAVEL_ON == 1 ]] && VOLUMES+=(retinue_travel-data)
  [[ $PLATFORM_ON == 1 ]] && VOLUMES+=(retinue_platform-data)
[[ $PAGES_ON == 1 ]] && VOLUMES+=(retinue_pages-data)   # what the pages keep, and what is shared where
  if (( ${#VOLUMES[@]} > 2 )); then
    echo "RETINUE_VOLUMES=\"${VOLUMES[*]}\"" >> "$BACKUP_ENV"
  fi
  if [[ $HOST_SETUP == 1 ]]; then
    install_restic
    install -d -m 755 /usr/local/lib/retinue
    install -m 755 "$REPO/deploy/backup/retinue-backup.py" /usr/local/lib/retinue/retinue-backup.py
    for unit in retinue-backup.service retinue-backup.timer; do   # the timer at 03:30 in the owner's zone
      sed -e "s|/srv/retinue|$ROOT|g" -e "s|^OnCalendar=\(.*\) UTC$|OnCalendar=\1 $OWNER_TZ|" \
        "$REPO/deploy/backup/$unit" > "/etc/systemd/system/$unit"
      chmod 644 "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    if [[ $BACKUP_STATE == on ]]; then systemctl enable --now retinue-backup.timer; fi
  fi
fi

# Health: the containers' state every two minutes into $ROOT/status/health.json; the router tells the owner. On the
# host, like the backup: it must see a stack that is down. Always on — it holds no secret and sends nothing itself.
if [[ $HOST_SETUP == 1 ]]; then
  install -d -m 755 /usr/local/lib/retinue
  install -m 755 "$REPO/deploy/health/retinue-health.py" /usr/local/lib/retinue/retinue-health.py
  for unit in retinue-health.service retinue-health.timer; do
    sed "s|/srv/retinue|$ROOT|g" "$REPO/deploy/health/$unit" > "/etc/systemd/system/$unit"
    chmod 644 "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  systemctl enable --now retinue-health.timer
fi

echo
echo "Done. Stack environment: $STACK (mode 600; values are not printed)."
echo "Written: ${WRITTEN[*]}"
for missing in ${MISSING[@]+"${MISSING[@]}"}; do
  echo "Fill in $missing"
done
[[ $TRAVEL_ON == 1 ]] && echo "Travel: on — the travel-ops image (about 5 GB) is built from TRAVEL_OPS_GIT by \`up -d --build\`"
[[ $MEMORY_ON == 1 ]] && echo "Memory: on — hub $HUB; rules $MEMORY_DIR/policy.json and checkout.txt (see docs/memory.md)"
if [[ $MAIL_ON == 1 ]]; then
  echo "Mail: on — keys: $(find "$MAIL_DIR/keys" -name '*.json' 2>/dev/null | wc -l | tr -d ' ') files in $MAIL_DIR/keys" \
       "(put there by deploy/mail/login.py on your Mac; see docs/mail.md)"
fi
if [[ $PAGES_ON == 1 ]]; then
  echo "Pages: on — https://$PAGES_HOST for $(paste -sd' ' "$DEVICES_FILE"); public sites:" \
       "$(paste -sd' ' "$PAGES_DIR/public_hosts.txt" | sed 's/^$/none/'), each with a public DNS record (docs/pages.md)"
  echo "Pages from the bot: the base's policy.json must let */artifacts/**/*.html through (docs/pages.md)"
fi
if [[ -d $LIFEHUB_DIR ]]; then
  echo "The life hub is retired (docs/pages.md): its containers and $LIFEHUB_DIR stay until you remove them —" \
       "docker rm -f retinue-lifehub-1 retinue-lifehub-build-1; rm /etc/dokploy/traefik/dynamic/lifehub.yml;" \
       "rm -r $LIFEHUB_DIR"
fi
[[ $TG_ON == 1 ]] && echo "Telegram Business: on — connect the bot in Telegram → Settings → Chat Automation, only" \
  "the chats you choose, only «reply on your behalf» (docs/courier.md)"
if [[ $SEND_ON == 1 ]]; then
  echo "Send: on — keys: $(find "$SEND_DIR/keys" -name '*.send.json' 2>/dev/null | wc -l | tr -d ' ') in $SEND_DIR/keys" \
       "(deploy/mail/login.py --kind send on your Mac; docs/courier.md)"
fi
[[ $PLATFORM_ON == 1 ]] && echo "Platform: on — lanes and tracks in $PLATFORM_DIR/platform.yaml; before the first" \
  "session run the container probe (docs/platform.md)"
if [[ $PLATFORM_ON == 1 ]] && ! has_entry "$ROOT/egress/open-deny-domains.txt"; then
  echo "open proxy: write your own domains into $ROOT/egress/open-deny-domains.txt (e.g. .example.com)," \
    "then restart lane-private-egress (docs/platform.md, «The room»)"
fi
if [[ $OPEN_CHANGED == 1 ]]; then
  echo "open proxy: its files changed — docker compose -p retinue --env-file $ROOT/stack.env" \
    "-f deploy/compose.yml restart lane-private-egress"
fi
if [[ $PLATFORM_ON == 1 ]] && ! has_entry "$ROOT/egress/open-deny-ips.txt"; then
  echo "open proxy: no address of this server in $ROOT/egress/open-deny-ips.txt — write its public addresses" \
    "there (e.g. 203.0.113.7/32), then restart lane-private-egress"
fi
if [[ $FACE_ON == 1 ]]; then
  echo "Face: on — https://$FACE_HOST for $(paste -sd' ' "$DEVICES_FILE"); send /app to the bot" \
       "(docs/face.md)"
  [[ $WEBAPP_KEY_STATE == off ]] && echo "Face: the Mini App logs in once TELEGRAM_BOT_TOKEN is filled and this script" \
    "is run again; /app codes work without it"
fi
case $BACKUP_STATE in
  on) echo "Backup: on, every night at 03:30 $OWNER_TZ; the result goes to $ROOT/status/backup.json" ;;
  unfilled) echo "Backup: off until $BACKUP_ENV is filled; then run this script again, and see docs/backup.md" ;;
esac
echo "Health: the containers every 2 min into $ROOT/status/health.json; the router tells you (docs/health.md)"
echo "Start: docker compose -p retinue --env-file $STACK -f deploy/compose.yml up -d  (Dokploy: copy the file into Environment)"
if [[ -n $MATRIX_SERVER_NAME ]]; then
  echo "Split DNS check (from a VPN client): dig @10.8.0.1 $MATRIX_SERVER_NAME  → 10.8.0.1"
fi
