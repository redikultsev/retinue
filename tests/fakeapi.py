"""A fake Anthropic Messages API and a hook endpoint on one ephemeral port, for running the real lane CLI without a
credential (`ANTHROPIC_API_KEY=sk-ant-fake`, `ANTHROPIC_BASE_URL=api.url`).

The model is scripted: while the conversation holds no `tool_use` yet, it calls `api.tools` — a list of
(name, input), all in one message — and once it does, it answers plain text. The hook endpoint (`api.hook_url`)
records every request and answers PreToolUse with `api.mode`; PermissionRequest is always denied. A request to
`/hook/mark/<n>` records a mark, and in mode `marker` the general PreToolUse hook waits up to `MARK_WAIT_S` for one:
a mark → no decision (the CLI's own rule decides), none → `defer`.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
import uuid
from dataclasses import dataclass

MARK_WAIT_S = 1.0
PERMISSION_DENIED = "probe: denied"


@dataclass
class Hook:
    path: str
    event: str | None
    tool: str | None
    input: dict | None
    tool_use_id: str | None
    authorization: str | None


def _sse(events: list[tuple[str, dict]]) -> bytes:
    return b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events)


def _block(index: int, start: dict, delta: dict) -> list[tuple[str, dict]]:
    return [("content_block_start", {"type": "content_block_start", "index": index, "content_block": start}),
            ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": index})]


class FakeApi:
    def __init__(self) -> None:
        self.tools: list[tuple[str, dict]] = []
        self.mode = "defer"  # defer | allow | deny | none | marker
        self.hooks: list[Hook] = []
        self.marks: set[str] = set()
        self._lock = threading.Lock()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self.hook_url = f"{self.url}/hook"

    def __enter__(self) -> FakeApi:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def events(self, event: str) -> list[Hook]:
        return [h for h in self.hooks if h.event == event and "/mark/" not in h.path]

    def reply(self, body: dict) -> bytes:
        messages = body.get("messages", [])
        called = any(m.get("role") == "assistant" and isinstance(m.get("content"), list) and
                     any(isinstance(c, dict) and c.get("type") == "tool_use" for c in m["content"]) for m in messages)
        offered = {t.get("name") for t in body.get("tools", [])}
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        events = [("message_start", {"type": "message_start", "message": {
            "id": "msg_" + uuid.uuid4().hex[:12], "type": "message", "role": "assistant",
            "model": body.get("model", "x"), "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": usage}})]
        if called or not self.tools or not {name for name, _ in self.tools} <= offered:
            events += _block(0, {"type": "text", "text": ""}, {"type": "text_delta", "text": "done"})
            stop = "end_turn"
        else:
            for i, (name, tool_input) in enumerate(self.tools):
                events += _block(i, {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": name,
                                     "input": {}},
                                 {"type": "input_json_delta", "partial_json": json.dumps(tool_input)})
            stop = "tool_use"
        events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                      "usage": {"output_tokens": 1}}),
                   ("message_stop", {"type": "message_stop"})]
        return _sse(events)

    def hook(self, path: str, body: dict, authorization: str | None) -> dict:
        event, tool_use_id = body.get("hook_event_name"), body.get("tool_use_id")
        with self._lock:
            self.hooks.append(Hook(path, event, body.get("tool_name"), body.get("tool_input"), tool_use_id,
                                   authorization))
        if path.startswith("/hook/mark/"):
            with self._lock:
                self.marks.add(tool_use_id)
            return {}
        if event == "PermissionRequest":
            return {"hookSpecificOutput": {"hookEventName": event,
                                           "decision": {"behavior": "deny", "message": PERMISSION_DENIED}}}
        if event != "PreToolUse":
            return {}
        mode = self.mode
        if mode == "marker":
            end = time.monotonic() + MARK_WAIT_S
            while time.monotonic() < end and tool_use_id not in self.marks:
                time.sleep(0.01)
            mode = "none" if tool_use_id in self.marks else "defer"
        if mode == "none":
            return {}
        return {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": mode,
                                       "permissionDecisionReason": "probe"}}

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, status: int, data: bytes = b"", kind: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_HEAD(self) -> None:
                self._send(200)

            def do_GET(self) -> None:
                self._send(404)

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                if self.path.startswith("/v1/messages/count_tokens"):
                    self._send(200, b'{"input_tokens": 10}')
                elif self.path.startswith("/v1/messages") and body.get("stream"):
                    self._send(200, api.reply(body), "text/event-stream")
                elif self.path.startswith("/v1/messages"):
                    self._send(200, json.dumps({
                        "id": "msg_x", "type": "message", "role": "assistant", "model": "x",
                        "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                        "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode())
                elif self.path.startswith("/hook/"):
                    self._send(200, json.dumps(api.hook(self.path, body, self.headers.get("authorization"))).encode())
                else:
                    self._send(404)

        return Handler
