"""Small admin helpers. `retinue-admin register` creates the owner account with a registration token."""

from __future__ import annotations

import argparse
import getpass
import sys

import httpx


def register(homeserver: str, username: str, token: str, password: str) -> str:
    """Walk the user-interactive auth flow of /register using a registration token."""
    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/register"
    body: dict = {"username": username, "password": password, "inhibit_login": True}
    with httpx.Client(timeout=30) as http:
        response = http.post(url, json=body)
        for _ in range(5):
            if response.status_code == 200:
                return response.json()["user_id"]
            data = response.json()
            if response.status_code != 401 or "session" not in data:
                raise SystemExit(f"registration failed: {response.status_code} {data}")
            done = set(data.get("completed", []))
            flow = next((f["stages"] for f in data.get("flows", []) if "m.login.registration_token" in f["stages"]),
                        None)
            if flow is None:
                raise SystemExit(f"server offers no registration-token flow: {data.get('flows')}")
            stage = next(s for s in flow if s not in done)
            auth = {"type": stage, "session": data["session"]}
            if stage == "m.login.registration_token":
                auth["token"] = token
            body["auth"] = auth
            response = http.post(url, json=body)
    raise SystemExit("registration did not finish")


def main() -> None:
    parser = argparse.ArgumentParser(description="Retinue admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    reg = sub.add_parser("register", help="create a Matrix account with a registration token")
    reg.add_argument("--homeserver", default="http://tuwunel:6167")
    reg.add_argument("--username", required=True)
    args = parser.parse_args()
    if args.cmd == "register":
        token = getpass.getpass("registration token: ")
        password = getpass.getpass("new password: ")
        if password != getpass.getpass("repeat password: "):
            sys.exit("passwords differ")
        print(register(args.homeserver, args.username, token, password))


if __name__ == "__main__":
    main()
