#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["google-auth-oauthlib>=1.2,<2"]
# ///
"""One consent of the owner, on his Mac: a refresh token for one account and one kind — `gmail` (read mail) or
`calendar` (read calendars) — checked to be that very account, and put on the server for the mail collector.
Prints names, never values: a terminal ends up in logs and transcripts.

    uv run deploy/mail/login.py --account you@gmail.com --kind gmail
    uv run deploy/mail/login.py --account you@gmail.com --kind calendar

A browser opens on Google's consent screen (the unverified-app warning is expected: the app is yours — docs/mail.md).
The Desktop client's JSON from Google Cloud console is read from ~/.config/retinue/google-client.json (or --client);
the server is an ssh host (--host, or RETINUE_SSH_HOST). Gmail and Calendar are two tokens on purpose: a password
change kills every token that holds a Gmail scope, and the calendar should outlive it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

# The same as retinue.google.SCOPES (a test keeps them equal): read-only, nothing that sends or changes.
SCOPES = {"gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
          "calendar": ["https://www.googleapis.com/auth/calendar.events.readonly",
                       "https://www.googleapis.com/auth/calendar.calendarlist.readonly"]}
WHO = {"gmail": ("https://gmail.googleapis.com/gmail/v1/users/me/profile", "emailAddress"),
       "calendar": ("https://www.googleapis.com/calendar/v3/calendars/primary", "id")}
KEYS = "/srv/retinue/mail/keys"     # the collector's keys folder (setup.sh MAIL=1)
COLLECTOR_UID = 10002               # the collector's own user (deploy/compose.yml)
CLIENT = Path("~/.config/retinue/google-client.json").expanduser()
ACCOUNT = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")


class Refused(Exception):
    """Nothing is put on the server; the text says why."""


def account_of(text: str) -> str:
    account = text.strip().lower()
    if not ACCOUNT.fullmatch(account):
        raise Refused(f"«{text}» — не адрес почты")
    return account


def client_of(path: Path) -> tuple[dict, bytes]:
    """The Desktop client from Google's JSON: the config the consent needs, and what the collector keeps of it."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"{path}: не читается JSON клиента ({type(exc).__name__})") from None
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict) or not installed.get("client_id") or not installed.get("client_secret"):
        raise Refused(f"{path}: это не Desktop-клиент (нет раздела «installed»): в консоли тип — Desktop app")
    kept = {"client_id": installed["client_id"], "client_secret": installed["client_secret"]}
    return data, json.dumps(kept).encode()


def key_of(account: str, kind: str, refresh_token: str) -> tuple[str, bytes]:
    """The token file's name and contents, as the collector reads them (retinue.google.keys)."""
    return f"{account}.{kind}.json", json.dumps({"account": account, "kind": kind,
                                                 "refresh_token": refresh_token}).encode()


def who(kind: str, access_token: str, opener=urllib.request.urlopen) -> str:
    """Which account the token really reads: Gmail's profile, or the primary calendar's id."""
    url, field = WHO[kind]
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with opener(request, timeout=30) as response:
        return str(json.loads(response.read()).get(field) or "").lower()


def remote(name: str) -> str:
    """The server's side of one file: the folder, the file written from stdin as the collector's user, mode 600,
    replaced at once (a half-written token is never read)."""
    if not re.fullmatch(r"[a-z0-9._%+@-]+\.json", name):
        raise Refused(f"имя файла «{name}»")
    path = f"{KEYS}/{name}"
    return (f"sudo install -d -m 700 -o {COLLECTOR_UID} -g {COLLECTOR_UID} {KEYS} && "
            f"sudo sh -c 'umask 077; cat > {path}.new' && sudo chown {COLLECTOR_UID}:{COLLECTOR_UID} {path}.new && "
            f"sudo chmod 600 {path}.new && sudo mv {path}.new {path}")


def put(host: str, name: str, data: bytes, run=subprocess.run) -> None:
    done = run(["ssh", host, remote(name)], input=data, capture_output=True)
    if done.returncode != 0:
        raise Refused(f"{host}: не записано {name} (ssh: код {done.returncode})")
    print(f"Сервер {host}: {KEYS}/{name} (600, uid {COLLECTOR_UID})")


def consent(config: dict, kind: str, account: str):
    """Google's consent in the browser, through a one-time server on 127.0.0.1 (loopback: the only way left for a
    Desktop client). Returns the credentials; checks that every scope asked was granted."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(config, scopes=SCOPES[kind])
    credentials = flow.run_local_server(port=0, open_browser=True, login_hint=account, prompt="consent")
    granted = set(getattr(credentials, "granted_scopes", None) or credentials.scopes or [])
    missing = [scope for scope in SCOPES[kind] if scope not in granted]
    if missing:
        raise Refused(f"Google не выдал: {', '.join(missing)} — на экране согласия отметь все галочки")
    if not credentials.refresh_token:
        raise Refused("Google не выдал refresh token")
    return credentials


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A read-only Google token for the mail collector, put on the server.")
    parser.add_argument("--account", required=True)
    parser.add_argument("--kind", required=True, choices=sorted(SCOPES))
    parser.add_argument("--client", type=Path, default=CLIENT)
    parser.add_argument("--host", default=os.environ.get("RETINUE_SSH_HOST", ""))
    args = parser.parse_args(argv)
    try:
        if not args.host:
            raise Refused("сервер не назван: --host или RETINUE_SSH_HOST")
        account = account_of(args.account)
        config, client = client_of(args.client)
        credentials = consent(config, args.kind, account)
        seen = who(args.kind, credentials.token)
        if seen != account:
            raise Refused(f"вход выполнен в {seen or 'неизвестный аккаунт'}, а не в {account}: токен не записан. "
                          "Повтори и выбери нужный аккаунт")
        put(args.host, "client.json", client)
        put(args.host, *key_of(account, args.kind, credentials.refresh_token))
    except Refused as exc:
        print(f"Не сделано: {exc}", file=sys.stderr)
        return 1
    print(f"Готово: {account}, {args.kind}. Сборщик возьмёт ключ при следующем опросе (до 10 минут).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
