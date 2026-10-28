"""The SNI guard (`retinue/sniguard.py`) between a client and a stand-in upstream proxy: a tunnel is spliced only
when the TLS ClientHello names the CONNECT host; another name, none, or not TLS is cut before a byte goes upstream."""

from __future__ import annotations

import asyncio
import ssl

import pytest

from retinue import sniguard


def client_hello(name: str | None) -> bytes:
    """A real ClientHello, as Python's TLS writes it for `name` (no SNI for None)."""
    context = ssl.create_default_context()
    context.check_hostname = name is not None
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


class Upstream:
    """A stand-in Squid: answers every CONNECT with `status`, then keeps what the tunnel brings."""

    def __init__(self, status: bytes = b"200 Connection established") -> None:
        self.status, self.connects, self.got = status, [], b""

    async def handle(self, reader, writer) -> None:
        self.connects.append((await reader.readuntil(b"\r\n\r\n")).split(b"\r\n")[0].decode())
        writer.write(b"HTTP/1.1 " + self.status + b"\r\n\r\n")
        await writer.drain()
        while data := await reader.read(65536):
            self.got += data
            if data.endswith(b"PING"):
                writer.write(b"PONG")
                await writer.drain()
        writer.close()


async def tunnel(target: str, payload: bytes, status: bytes = b"200 Connection established",
                 head: bytes | None = None) -> tuple[bytes, bytes, Upstream]:
    """What the client read back, what reached the upstream through the tunnel, the upstream."""
    upstream = Upstream(status)
    up = await asyncio.start_server(upstream.handle, "127.0.0.1", 0)
    guard = await sniguard.serve(("127.0.0.1", 0), ("127.0.0.1", up.sockets[0].getsockname()[1]))
    reader, writer = await asyncio.open_connection("127.0.0.1", guard.sockets[0].getsockname()[1])
    writer.write(head or f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    answer = await reader.readuntil(b"\r\n\r\n")
    if answer.startswith(b"HTTP/1.1 200"):
        writer.write(payload + b"PING")
        await writer.drain()
        answer += await asyncio.wait_for(reader.read(4), 2)
    writer.close()
    await asyncio.sleep(0.05)
    for server in (guard, up):
        server.close()
    return answer, upstream.got, upstream


def run(coro):
    return asyncio.run(coro)


def test_a_hello_that_names_the_connect_host_is_spliced_whole():
    hello = client_hello("registry.npmjs.org")
    answer, got, upstream = run(tunnel("registry.npmjs.org:443", hello))
    assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"PONG"), "the tunnel carries both ways"
    assert got == hello + b"PING" and upstream.connects == ["CONNECT registry.npmjs.org:443 HTTP/1.1"]
    assert sniguard.sni(hello[5:]) == "registry.npmjs.org"


@pytest.mark.parametrize("name", ["developers.cloudflare.com", "www.python.org", None])
def test_another_name_or_none_behind_an_allowed_connect_is_cut_before_anything_goes_upstream(name):
    """Domain fronting: CONNECT to a host Bash may reach, then TLS to another site of the same CDN."""
    answer, got, _ = run(tunnel("registry.npmjs.org:443", client_hello(name)))
    assert answer.startswith(b"HTTP/1.1 200") and not answer.endswith(b"PONG")
    assert got == b"", "not one byte of the hello reached the upstream"


def test_not_tls_an_address_or_not_a_connect_is_refused():
    answer, got, _ = run(tunnel("github.com:443", b"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n"))
    assert got == b"" and not answer.endswith(b"PONG")
    answer, _, upstream = run(tunnel("203.0.113.7:443", client_hello(None)))
    assert answer.startswith(b"HTTP/1.1 403") and upstream.connects == []
    answer, _, upstream = run(tunnel("", b"", head=b"GET http://evil.example/ HTTP/1.1\r\nHost: evil.example\r\n\r\n"))
    assert answer.startswith(b"HTTP/1.1 405") and upstream.connects == []


def test_the_upstreams_refusal_is_passed_on_and_nothing_more():
    answer, got, _ = run(tunnel("10.8.0.1.nip.io:443", client_hello("10.8.0.1.nip.io"), status=b"403 Forbidden"))
    assert answer.startswith(b"HTTP/1.1 403") and got == b""


def test_a_hello_split_over_records_is_read_whole_and_forwarded_as_sent():
    hello = client_hello("pypi.org")
    body = hello[5:]
    split = b"".join(b"\x16\x03\x01" + len(body[i:i + 100]).to_bytes(2, "big") + body[i:i + 100]
                     for i in range(0, len(body), 100))
    answer, got, _ = run(tunnel("pypi.org:443", split))
    assert answer.endswith(b"PONG") and got == split + b"PING"


def test_a_name_compares_without_case_or_a_trailing_dot():
    assert sniguard.same_host("PyPI.org.", "pypi.org") and not sniguard.same_host("pypi.org.evil", "pypi.org")
