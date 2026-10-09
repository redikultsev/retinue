"""The owner's login script for the mail collector (deploy/mail/login.py), without Google and without a server:
read-only scopes, the account checked, the files as the collector reads them, names printed and never values."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from retinue import google

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mail_login", ROOT / "deploy" / "mail" / "login.py")
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


def test_the_script_asks_for_read_only_scopes_and_writes_what_the_collector_reads(tmp_path):
    assert login.SCOPES == google.SCOPES, "the script and the collector ask for the same"
    assert all(scope.endswith("readonly") for scopes in login.SCOPES.values() for scope in scopes)
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs",
                                                "redirect_uris": ["http://localhost"]}}))
    config, kept = login.client_of(client)
    assert config["installed"]["client_id"] == "cid" and json.loads(kept) == {"client_id": "cid", "client_secret": "cs"}
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "client.json").write_bytes(kept)
    name, data = login.key_of(login.account_of(" Owner@Example.org "), "gmail", "rt-1")
    (keys / name).write_bytes(data)
    (found,) = google.keys(keys)
    assert (found.account, found.kind, found.refresh_token, found.client_secret) == ("owner@example.org", "gmail",
                                                                                     "rt-1", "cs")
    client.write_text(json.dumps({"web": {"client_id": "cid", "client_secret": "cs"}}))
    with pytest.raises(login.Refused, match="Desktop"):
        login.client_of(client)
    for bad in ("x; rm -rf /", "a@b", "$(id)@x.org"):
        with pytest.raises(login.Refused):
            login.account_of(bad)


def test_the_account_is_checked_by_what_the_token_reads():
    asked = []

    def opener(request, timeout):
        asked.append((request.full_url, request.get_header("Authorization")))
        body = {"emailAddress": "Owner@Example.org"} if "gmail" in request.full_url else {"id": "other@example.org"}
        return io.BytesIO(json.dumps(body).encode())

    assert login.who("gmail", "at", opener) == "owner@example.org"
    assert login.who("calendar", "at", opener) == "other@example.org"
    assert asked == [("https://gmail.googleapis.com/gmail/v1/users/me/profile", "Bearer at"),
                     ("https://www.googleapis.com/calendar/v3/users/me/calendarList/primary", "Bearer at")]


def test_a_token_goes_to_the_server_through_stdin_and_only_names_are_printed(capsys, monkeypatch, tmp_path):
    ran = []

    def run(command, input, capture_output):
        ran.append((command, input))
        return SimpleNamespace(returncode=0)

    login.put("netcup", "owner@example.org.gmail.json", b'{"refresh_token": "secret-rt"}', run)
    (command, data), = ran
    assert command[:2] == ["ssh", "netcup"] and "secret-rt" not in command[2] and data == b'{"refresh_token": "secret-rt"}'
    assert "-o 10002 -g 10002 /srv/retinue/mail/keys" in command[2] and "chmod 600" in command[2]
    assert command[2].endswith("sudo mv /srv/retinue/mail/keys/owner@example.org.gmail.json.new "
                               "/srv/retinue/mail/keys/owner@example.org.gmail.json"), "replaced at once"
    assert capsys.readouterr().out == "Сервер netcup: /srv/retinue/mail/keys/owner@example.org.gmail.json (600, uid 10002)\n"
    with pytest.raises(login.Refused):
        login.remote("a'; reboot; '.json")

    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    credentials = SimpleNamespace(token="at", refresh_token="secret-rt")
    monkeypatch.setattr(login, "consent", lambda config, kind, account: credentials)
    monkeypatch.setattr(login, "who", lambda kind, token: "someone@else.org")
    monkeypatch.setattr(login, "put", lambda *a: ran.append(a))
    assert login.main(["--account", "owner@example.org", "--kind", "gmail", "--client", str(client),
                       "--host", "netcup"]) == 1
    out = capsys.readouterr()
    assert "вход выполнен в someone@else.org, а не в owner@example.org: токен не записан" in out.err
    assert len(ran) == 1, "the wrong account: nothing is put on the server"
    monkeypatch.setattr(login, "who", lambda kind, token: "owner@example.org")
    assert login.main(["--account", "owner@example.org", "--kind", "gmail", "--client", str(client),
                       "--host", "netcup"]) == 0
    assert [a[1] for a in ran[1:]] == ["client.json", "owner@example.org.gmail.json"]
    out = capsys.readouterr()
    assert "secret-rt" not in out.out + out.err and out.out.endswith("Готово: owner@example.org, gmail. Сборщик возьмёт "
                                                                     "ключ при следующем опросе (до 10 минут).\n")
