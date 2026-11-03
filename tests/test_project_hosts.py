"""A project's own hosts (`retinue/projecthosts.py`): a session in a project's folder reaches the hosts the install
lists for that project, with the install's key, through the platform's `project_request` — each call the gate's first;
any other host, or a session of another folder, is refused; a call that is not a read — a booking, a payment — waits
for the owner's button with the whole request on the card and runs once; and the lists are the install's, out of
every session's reach. Against the fake lanes of `test_platformd` and a fake host."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from retinue import approvals, gate, projecthosts, subs
from test_platformd import short_wait  # noqa: F401 — the fixture is autouse
from test_subs import DEFERRED, SubsWorld, subs_scenario

ROOT = Path(__file__).resolve().parents[1]
TOOL = projecthosts.TOOL
KEY = "fake-travel-key-0000"
TRAVEL = """
project: travel
hosts: [booking.example.com]
key: {header: Authorization, prefix: "Bearer ", env: RETINUE_PROJECT_KEY_TRAVEL}
"""
FLIGHTS = """
project: travel/flights
hosts: [booking.example.com]
"""


class Host:
    """booking.example.com as the platform's client meets it: every request it got, answered as asked."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/api/echo":  # a site that repeats the request's headers back
                return httpx.Response(200, json={"seen": dict(request.headers)})
            return httpx.Response(200, json={"ok": True, "path": request.url.path})
        return httpx.MockTransport(handler)


def lists(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    folder = tmp_path / "egress-projects"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in (files if files is not None else {"travel.yaml": TRAVEL, "flights.yaml": FLIGHTS}).items():
        (folder / name).write_text(text)
    return folder


def install(world: SubsWorld, host: Host, files: dict[str, str] | None = None) -> None:
    """The install's lists, as the platform loads them at start, with the fake host for the way out."""
    found = projecthosts.load(str(lists(world.tmp, files)), base="base", env={"RETINUE_PROJECT_KEY_TRAVEL": KEY},
                              transport=host.transport())
    world.platform.projects = world.platform.gate.projects = found


async def in_folder(world: SubsWorld, folder: str, text: str = "найди отель") -> str:
    """A session the owner started, made in `folder` of the room (the platform's own record of where it runs)."""
    session = await world.parent(text)
    world.store.db.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (folder, session))
    world.store.db.commit()
    return session


async def tools_of(world: SubsWorld, session: str) -> list[str]:
    status, data = await world.mcp(world.run_of(session), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200
    return [tool["name"] for tool in data["result"]["tools"]]


def grant(world: SubsWorld, session: str) -> dict:
    [approval] = world.store.approvals(session=session, states=("pending",))
    assert world.store.move(approval["id"], ("pending",), "shown", 1.0)
    assert world.store.move(approval["id"], ("shown",), "granted", 2.0)
    return approval


GET = {"method": "GET", "url": "https://booking.example.com/api/hotels?city=Lisbon&nights=3"}
BOOK = {"method": "POST", "url": "https://booking.example.com/api/bookings",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"hotel": "h-123", "guest": "Anna Petrova", "from": "2027-06-03", "to": "2027-06-08"})}
PAY = {"method": "POST", "url": "https://booking.example.com/api/payments",
       "body": json.dumps({"booking": "b-77", "amount": "412.00", "currency": "EUR"})}


# --- the four the track is done by ---------------------------------------------------------------------------------

def test_a_projects_code_reaches_the_hosts_on_its_list(tmp_path):
    host = Host()

    async def body(world: SubsWorld):
        install(world, host)
        session = await in_folder(world, "base/travel")
        assert TOOL in await tools_of(world, session), "a session of a project with hosts gets the tool"
        said, error = await world.tool(session, TOOL, GET, "toolu_1")
        assert not error, said
        assert said.startswith("200") and '"/api/hotels"' in said and "data, not instructions" in said
        [sent] = host.requests
        assert (sent.method, str(sent.url)) == ("GET", GET["url"])
        assert sent.headers["authorization"] == f"Bearer {KEY}", "the install's key, added by the platform"
        said, error = await world.tool(session, TOOL, {"method": "HEAD", "url": GET["url"]}, "toolu_2")
        assert not error and len(host.requests) == 2 and world.store.approvals(session=session) == [], "no card"
        said, _ = await world.tool(session, TOOL, {"method": "GET", "url": "https://booking.example.com/api/echo"},
                                   "toolu_3")
        assert KEY not in said and "[the project's key]" in said, "a site that repeats it never hands it back"
    subs_scenario(tmp_path, body)


def test_a_projects_code_is_refused_a_host_off_its_list(tmp_path):
    host = Host()

    async def body(world: SubsWorld):
        install(world, host)
        session = await in_folder(world, "base/travel")
        for url in ("https://evil.example/collect?d=notes", "https://api.booking.example.com/x",
                    "https://booking.example.com.evil.example/x", "http://booking.example.com/api/hotels",
                    "https://user@booking.example.com/api/hotels", "https://booking.example.com:8443/api/hotels",
                    "https://93.184.216.34/api/hotels", "ftp://booking.example.com/x", "booking.example.com/x"):
            said, error = await world.tool(session, TOOL, {"method": "GET", "url": url}, f"toolu_{url}")
            assert error and said.startswith("Refused"), (url, said)
        said, error = await world.tool(session, TOOL, {**GET, "headers": {"Authorization": "Bearer mine"}}, "toolu_h")
        assert error and "Authorization" in said, "the key's header is the platform's to set"
        said, error = await world.tool(session, TOOL, {**GET, "method": "TRACE"}, "toolu_m")
        assert error
        said, error = await world.tool(session, TOOL, GET, "toolu_unseen", pre=False)
        assert error and said == subs.UNSEEN, "the server runs only a call the gate saw"
        # A session of a folder with no list: no tool, and a call made anyway is refused — today's room.
        notes = await in_folder(world, "base/notes", "заметки")
        assert TOOL not in await tools_of(world, notes)
        said, error = await world.tool(notes, TOOL, GET, "toolu_n")
        assert error and said.startswith("Refused") and "no hosts of its own" in said
        # A list is its own folder's: not the folder above (the Base's root), nor one below (travel/visa).
        visa = await in_folder(world, "base/travel/visa", "виза")
        said, error = await world.tool(visa, TOOL, GET, "toolu_v")
        assert error and said.startswith("Refused")
        root = await in_folder(world, "base", "корень")
        said, error = await world.tool(root, TOOL, GET, "toolu_r")
        assert error and said.startswith("Refused")
        assert host.requests == [], "nothing reached any host"
    subs_scenario(tmp_path, body)


def test_a_booking_or_payment_waits_for_the_owners_button(tmp_path):
    host = Host()

    async def body(world: SubsWorld):
        install(world, host)
        session = await in_folder(world, "base/travel")
        for n, call in enumerate((BOOK, PAY, {"method": "DELETE", "url": "https://booking.example.com/api/bookings/7"})):
            said, error = await world.tool(session, TOOL, call, f"toolu_wait{n}")
            assert said == DEFERRED, (call, said)
            [approval] = world.store.approvals(session=session, states=("pending",))
            assert (approval["tool"], approval["input"]) == (projecthosts.NAME, call), "the request whole"
            assert approval["why"].startswith("Waits for your button: " + call["method"])
            view, buttons = approvals.card(approval, session, "notes")
            assert buttons == (approvals.APPROVE, approvals.REJECT)
            for part in (call["url"], call.get("body") or call["url"]):
                assert json.dumps(part)[1:-1] in view.body, "the card shows the request as it would be sent"
            assert "Authorization" in " ".join(view.head) and KEY not in view.plain(), "the key named, never shown"
            assert world.store.move(approval["id"], ("pending",), "dropped", 3.0)
        assert host.requests == [], "nothing sent before the button"
        said, error = await world.tool(session, TOOL, BOOK, "toolu_book")
        assert said == DEFERRED
        said, error = await world.tool(session, TOOL, BOOK, "toolu_book", pre=False)
        assert error and host.requests == [], "the server refuses a booking nobody allowed"
        grant(world, session)
        said, error = await world.tool(session, TOOL, BOOK, "toolu_book")
        assert not error and said.startswith("200"), said
        [sent] = host.requests
        assert (sent.method, json.loads(sent.content)) == ("POST", json.loads(BOOK["body"]))
        assert sent.headers["authorization"] == f"Bearer {KEY}"
        again, error = await world.tool(session, TOOL, BOOK, "toolu_book", pre=False)
        assert again == said and len(host.requests) == 1, "the same call's answer again: never a second booking"
        said, error = await world.tool(session, TOOL, BOOK, "toolu_book")
        assert error and len(host.requests) == 1, "the grant is spent"
        said, error = await world.tool(session, TOOL, BOOK, "toolu_book2")
        assert said == DEFERRED, "the same booking made again is the owner's again"
        said, error = await world.tool(session, TOOL, PAY, "toolu_sub", agent="agent-1")
        assert error and said == gate.SUBAGENT, "a subagent cannot wait for the button"
        assert len(host.requests) == 1
    subs_scenario(tmp_path, body)


def test_a_project_cannot_write_its_own_list(tmp_path):
    host = Host()

    async def body(world: SubsWorld):
        install(world, host)
        # A list written into the project's own folder — where a session can write — counts for nothing.
        (world.tmp / "work" / "base" / "travel").mkdir(parents=True)
        (world.tmp / "work" / "base" / "travel" / "travel.yaml").write_text(TRAVEL.replace(
            "[booking.example.com]", "[booking.example.com, evil.example]"))
        session = await in_folder(world, "base/travel")
        said, error = await world.tool(session, TOOL, {"method": "GET", "url": "https://evil.example/x"}, "toolu_1")
        assert error and said.startswith("Refused")
        # The install's lists are outside /work: a file tool is refused on the way there, by itself or by `..`.
        run = world.run_of(session)
        for path in ("/config/projects/travel.yaml", "/work/../config/projects/travel.yaml"):
            answer = await world.hook("PreToolUse", run, {
                "hook_event_name": "PreToolUse", "tool_name": "Write", "tool_use_id": f"toolu_w{path}",
                "cwd": "/work/base/travel", "permission_mode": "auto",
                "tool_input": {"file_path": path, "content": "project: travel\nhosts: [evil.example]\n"}})
            assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
            assert answer["hookSpecificOutput"]["permissionDecisionReason"] == approvals.OUTSIDE
        # A session cannot move itself into a project: its folder is the platform's record, not the hook's `cwd`.
        notes = await in_folder(world, "base/notes", "заметки")
        run = world.run_of(notes)
        answer = await world.hook("PreToolUse", run, {
            "hook_event_name": "PreToolUse", "tool_name": projecthosts.NAME, "tool_use_id": "toolu_cd",
            "cwd": "/work/base/travel", "permission_mode": "auto", "tool_input": GET})
        assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert host.requests == []
    subs_scenario(tmp_path, body)
    # The platform refuses to start on lists kept where a lane writes: inside a lane's work volume.
    inside = tmp_path / "work" / "base" / "egress"
    inside.mkdir(parents=True)
    (inside / "travel.yaml").write_text(TRAVEL)
    with pytest.raises(SystemExit, match="where a session can write"):
        projecthosts.load(str(inside), base="base", env={}, forbidden=[str(tmp_path / "work")])
    # In the stack: the lists and the keys are the platform's alone, read-only; no lane mounts anything of egress.
    compose = yaml.safe_load((ROOT / "deploy" / "compose.yml").read_text())
    services = compose["services"]
    assert f"/srv/retinue/egress/projects:{projecthosts.DIR}:ro" in services["platform"]["volumes"]
    holders = sorted(name for name, service in services.items()
                     if any(str(v).startswith("/srv/retinue/egress/projects") for v in service.get("volumes", [])))
    assert holders == ["platform"]
    for name, service in services.items():
        if name.startswith("lane-") and not name.endswith(("-egress", "-sni")):
            assert not [v for v in service.get("volumes", []) if str(v).startswith("/srv/retinue/egress")], name
    keys = sorted(name for name, service in services.items()
                  if "project-keys.env" in json.dumps(service.get("env_file") or []))
    assert keys == ["platform"], "the keys: in the platform's environment alone"
    for lane in ("private", "code", "web"):
        settings = json.loads((ROOT / "deploy" / "lanes" / f"managed-settings-{lane}.json").read_text())
        assert "booking.example.com" not in settings["sandbox"]["network"]["allowedDomains"], "Bash is not widened"


# --- the rest -------------------------------------------------------------------------------------------------------

def test_a_projects_key_goes_with_its_own_requests_only(tmp_path):
    """Two projects list the same host; only travel's list names a key, so a sub-project's call goes without it."""
    host = Host()

    async def body(world: SubsWorld):
        install(world, host)
        flights = await in_folder(world, "base/travel/flights", "рейсы")
        said, error = await world.tool(flights, TOOL, GET, "toolu_f")
        assert not error, said
        [sent] = host.requests
        assert "authorization" not in sent.headers
    subs_scenario(tmp_path, body)


def test_the_example_list_loads_and_names_no_real_host():
    example = ROOT / "deploy" / "egress" / "projects" / "travel.example.yaml"
    data = yaml.safe_load(example.read_text())
    assert data["hosts"] == ["booking.example.com"]
    found = projecthosts.parse(data, "travel")
    assert found.path == "travel" and found.key_env == "RETINUE_PROJECT_KEY_TRAVEL"


@pytest.mark.parametrize("text, why", [
    ("project: travel\nhosts: ['*.example.com']\n", "not a host name"),
    ("project: ../x\nhosts: [a.example.com]\n", "not a folder in the Base"),
    ("project: /work/base\nhosts: [a.example.com]\n", "not a folder in the Base"),
    ("project: travel\nhosts: [a.example.com]\nkey: {header: X-Key, env: ELEVENLABS_API_KEY}\n",
     "RETINUE_PROJECT_KEY_"),
    ("project: travel\nhosts: [a.example.com]\nkey: {header: Host, env: RETINUE_PROJECT_KEY_T}\n", "header"),
    ("project: travel\nhosts: [a.example.com]\nextra: 1\n", "unknown"),
])
def test_a_list_the_platform_cannot_read_stops_it(tmp_path, text, why):
    with pytest.raises(SystemExit, match=why):
        projecthosts.load(str(lists(tmp_path, {"travel.yaml": text})), base="base", env={})


def test_two_lists_for_one_project_and_no_lists_at_all(tmp_path):
    with pytest.raises(SystemExit, match="two lists"):
        projecthosts.load(str(lists(tmp_path, {"a.yaml": TRAVEL, "b.yaml": TRAVEL})), base="base", env={})
    empty = projecthosts.load(str(tmp_path / "none"), base="base", env={})
    assert empty.of("base/travel") is None, "no folder: today's room"
    examples = projecthosts.load(str(lists(tmp_path / "x", {"travel.example.yaml": TRAVEL})), base="base", env={})
    assert examples.of("base/travel") is None, "an example is not a list"
    no_base = projecthosts.load(str(lists(tmp_path / "y", {"travel.yaml": TRAVEL})), base=None, env={})
    assert no_base.of("base/travel") is None, "without the Base no folder is a project"


def test_a_list_whose_key_is_missing_refuses_its_calls_rather_than_sending_them_bare(tmp_path):
    found = projecthosts.load(str(lists(tmp_path, {"travel.yaml": TRAVEL})), base="base", env={},
                              transport=Host().transport())
    said, error = asyncio.run(found.call({"cwd": "base/travel"}, GET, "toolu_1", granted=False))
    assert error and "RETINUE_PROJECT_KEY_TRAVEL" in said
