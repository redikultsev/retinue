"""`/mcp/device`: the owner's collections for the agents on his own computer — macOS, Windows or Linux
(docs/collections.md#from-the-owners-computer) — the same four
tools a session gets (`colltools`), as an MCP server on the face's own address, behind the face's Traefik (his
devices' WireGuard addresses only) and nginx.

**Who** is a device's token alone: `Authorization: Bearer <token>`, compared by its SHA-256 with the `devices` the
owner keeps from the server's terminal (`retinue-platform collections device …`); a revoked one opens nothing at its
next call. The face's cookie and its `X-Retinue` header play no part here — the face's guard lets this app's requests
through to its own check (`Face._guarded`) — and a token opens nothing of the face's `/api/*`.

**What** is the token's scope: `read` lists and serves `collection_describe` and `collection_query`; `write` the four.
Beyond that the collection's rights decide, for the class `device` (by default it adds and sets every field and
never deletes); the store checks them, never this module. A session on his computer is not marked as having read
foreign text: the owner runs it himself and sees it as it works (spec §3.7), and the assistant counts its records
as foreign.

**Writer** `device:<device>/<session>` via `device`: the session is the MCP session this server gives at `initialize`
(`Mcp-Session-Id`), so the feed can tell one conversation from another. The id is signed for its device (an HMAC
keyed by the device's token's SHA-256): one this server did not give that device is 404, and the client starts a
session again, as MCP has it — a device cannot name its sessions, nor take another device's. A tool call's own idem
(`tool:<claudecode/toolUseId>`) is the device's (`idem_scope`): Claude Code makes an idempotent call again on a new
connection, which is a new MCP session of the same device. An idem the model names is its session's alone, as the
assistant's is her turn's: two conversations on one computer that both say `add-1` are two calls.

Unreachable — the computer off the VPN, the platform down — the tools are missing from its session, and nothing is
kept to send later: the agent says what it could not record.

**The device's own methods**, beside the tools and never listed among them, so a Claude Code session on the computer neither
sees nor calls them: `retinue/sessions` takes the device's whole list of its local sessions (`computers.py`), any scope —
they are the device's, not the owner's records —, and `retinue/projects` gives the Base's project tree (folder, title,
parent), which the computer's helper files its sidebar groups by. Neither puts a title or a folder in a log.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from typing import TYPE_CHECKING

from aiohttp import web

from . import colltools, computers, subs
from .collections import Refused, Writer
from .spaces import Refused as SpacesRefused, Unreadable

if TYPE_CHECKING:
    from .platformd import Platform
    from .spaces import Spaces

PATH = "/mcp/device"
ALIAS = "/mcp/mac"             # its old name (#66): the clients set up with it keep working; the docs name PATH only
BODY_MAX = 4 * 2**20        # 200 operations of records up to 16 KB each, and their JSON; nginx's 4m says the same
SESSION_HEADER = "Mcp-Session-Id"
SESSION = re.compile(r"[A-Za-z0-9_-]{1,64}")  # as `collections.ACTOR` takes a device's session
READS = (colltools.DESCRIBE, colltools.QUERY)
TOOLS = {"read": [t for t in colltools.TOOLS if t["name"] in READS], "write": colltools.TOOLS}
UNAUTHORIZED = "A device's token is needed: Authorization: Bearer <token> (retinue-platform collections device add)."
READ_ONLY = ("This device's token only reads ({tool} is not in its scope): describe and query. The owner gives a "
             "device that writes on the server's terminal.")
DEVICE = web.RequestKey("device", dict)  # the device the token is: `{name, scope, sha256}`
NO_SESSION = f"No {SESSION_HEADER}: initialize first, then send the id it gave with every request."
UNKNOWN_SESSION = f"No such session: initialize again, then send the {SESSION_HEADER} it gives."
TOO_LONG = f"A request of {BODY_MAX // 2**20} MB at most, its Content-Length given: split the operations."
SESSIONS, PROJECTS = "retinue/sessions", "retinue/projects"  # the device's own methods, not tools
NO_BASE = "This platform has no Base: no projects to file sessions under."


class Device:
    def __init__(self, platform: Platform, spaces: Spaces | None = None) -> None:
        self.platform = platform
        self.spaces = spaces  # the face's reader of the Base: `retinue/projects`

    def app(self) -> web.Application:
        # aiohttp reads a body by its root app's `client_max_size` (the face's), never a sub-app's: `mcp` checks its own.
        app = web.Application(middlewares=[self._token])
        app.add_routes([web.post("", self.mcp), web.get("", self.no_stream), web.delete("", self.no_stream)])
        return app

    @web.middleware
    async def _token(self, request: web.Request, handler):
        """Every request of this app, a 404 included, is a device's: its token, else 401 before anything is read. The
        token is looked up by its SHA-256, never compared as itself."""
        given = request.headers.get("Authorization", "")
        token = given[7:] if given[:7].lower() == "bearer " else ""
        try:
            device = await self.platform.collections.device(token) if token else None
        except Refused as exc:
            return web.json_response({"error": str(exc)}, status=503)
        if device is None:
            return web.json_response({"error": UNAUTHORIZED}, status=401,
                                     headers={"WWW-Authenticate": 'Bearer realm="retinue"'})
        request[DEVICE] = device
        return await handler(request)

    async def no_stream(self, request: web.Request) -> web.Response:
        """No server-sent stream and no session to end: every answer comes in its POST's (MCP's streamable HTTP allows
        405 for both)."""
        return web.Response(status=405, headers={"Allow": "POST"})

    async def mcp(self, request: web.Request) -> web.Response:
        device = request[DEVICE]
        if request.content_length is None or request.content_length > BODY_MAX:
            return _refused(TOO_LONG, 413)
        try:
            body = await request.json()
        except ValueError:
            body = None
        starting = isinstance(body, dict) and body.get("method") == "initialize"
        if starting:
            session = signed(device, secrets.token_hex(8))
        else:
            session = request.headers.get(SESSION_HEADER, "")
            if not session:
                return _refused(NO_SESSION, 400)
            if not SESSION.fullmatch(session) or signed(device, session.split("-")[0]) != session:
                return _refused(UNKNOWN_SESSION, 404)
        if isinstance(body, dict) and body.get("method") in (SESSIONS, PROJECTS) and "id" in body:
            status, answer = await self._own(device, body)
            return web.json_response(answer, status=status)
        writer = Writer(f"device:{device['name']}/{session}", "device")
        tools = TOOLS[device["scope"]]

        async def call(name: str, arguments: dict, params: dict) -> tuple[str, bool]:
            if name not in {t["name"] for t in tools}:
                return (READ_ONLY.format(tool=name) if colltools.has(name) else f"No such tool: {name}."), True
            # The engine's id of the call, else the request's own within this MCP session: the idem of an apply, and
            # the one proposal a call makes.
            call_id = subs.call_id_of(params) or f"rpc:{session}/{body.get('id')}"
            if isinstance(arguments.get("idem"), str):
                arguments = {**arguments, "idem": f"{session}:{arguments['idem']}"}  # the model's, its session's alone
            return await self.platform.colltools.call(writer, None, name, arguments, call_id)

        status, answer = await subs.rpc(body, colltools.INSTRUCTIONS, call, tools)
        headers = {SESSION_HEADER: session} if starting else None
        if answer is None:
            return web.Response(status=status, headers=headers)
        return web.json_response(answer, status=status, headers=headers)


    async def _own(self, device: dict, body: dict) -> tuple[int, dict]:
        """`retinue/sessions` and `retinue/projects`: JSON-RPC answers of their own, no tool's content blocks."""
        params = body.get("params") if isinstance(body.get("params"), dict) else {}

        def error(message: str, code: int = -32602) -> tuple[int, dict]:
            return 200, {"jsonrpc": "2.0", "id": body["id"], "error": {"code": code, "message": message}}

        if body["method"] == SESSIONS:
            try:
                taken, left_out = self.platform.computers.report(device["name"], params, self.platform.clock())
            except computers.Refused as exc:
                return error(str(exc))
            answers = self.platform.computers.answers(device["name"])  # the owner's, until it names them delivered
            return 200, {"jsonrpc": "2.0", "id": body["id"],
                         "result": {"taken": taken, "again_s": computers.BEAT_S,
                                    **({"left_out": left_out} if left_out else {}),
                                    **({"answers": answers} if answers else {})}}
        if self.spaces is None:
            return error(NO_BASE, -32000)
        try:
            head, rows = await asyncio.to_thread(lambda: self.spaces.tree().every())
        except (SpacesRefused, Unreadable):
            return error(NO_BASE, -32000)
        return 200, {"jsonrpc": "2.0", "id": body["id"], "result": {
            "head": head, "projects": [{"path": r["name"], "title": r["title"], "parent": r["parent"]} for r in rows]}}


def signed(device: dict, nonce: str) -> str:
    """A session's id for its device: `<nonce>-<HMAC of the device and the nonce, keyed by its token's SHA-256>`."""
    digest = hmac.new(bytes.fromhex(device["sha256"]), f"{device['name']}/{nonce}".encode(), hashlib.sha256)
    return f"{nonce}-{digest.hexdigest()[:24]}"


def _refused(message: str, status: int) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": message}},
                             status=status)
