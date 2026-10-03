"""End-to-end smoke: register an owner, join the agent room, send a message, expect the echo reply.

    docker compose -f tests/e2e/compose.yml up -d && python3 tests/e2e/smoke.py
"""

import json
import subprocess
import time
import urllib.parse
import urllib.request

HS = "http://127.0.0.1:16167"
HERE = __file__.rsplit("/", 1)[0]


def call(token, method, path, body=None):
    req = urllib.request.Request(HS + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.load(response)


def main():
    subprocess.run(["docker", "compose", "-f", f"{HERE}/compose.yml", "exec", "-T", "router", "python", "-c",
                    "from retinue.admin import register; register('http://tuwunel:6167','owner','e2e-token','pw-123456')"],
                   check=False)
    login = urllib.request.Request(HS + "/_matrix/client/v3/login", method="POST", data=json.dumps(
        {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": "owner"}, "password": "pw-123456"}
    ).encode(), headers={"Content-Type": "application/json"})
    token = json.load(urllib.request.urlopen(login))["access_token"]
    invites = call(token, "GET", "/_matrix/client/v3/sync?timeout=0").get("rooms", {}).get("invite", {})
    rooms = {}
    for room_id, invite in invites.items():
        call(token, "POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join", {})
        for event in invite["invite_state"]["events"]:
            if event["type"] == "m.room.name":
                rooms[event["content"]["name"]] = room_id
    room = urllib.parse.quote(rooms["Эхо"])
    call(token, "PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/t{time.time_ns()}",
         {"msgtype": "m.text", "body": "ping"})
    for _ in range(30):
        time.sleep(1)
        chunk = call(token, "GET", f"/_matrix/client/v3/rooms/{room}/messages?dir=b&limit=5")["chunk"]
        if any(e["type"] == "m.room.message" and e["content"].get("body") == "echo: ping" for e in chunk):
            print("OK: agent answered over Matrix + A2A")
            return
    raise SystemExit("FAIL: no answer from the agent")


if __name__ == "__main__":
    main()
