"""The SNI guard: the room's proxy in front of its open Squid, a tunnel only to the host the TLS handshake names.

The room's CLI and its Bash sandbox go out through one proxy (`HTTPS_PROXY`): the CLI's own model calls and
`WebFetch` directly, Bash's traffic through the CLI's sandbox proxy, which checks the CONNECT host against the managed
list (GitHub, PyPI, npm) and then tunnels through the same `HTTPS_PROXY`. Squid and that list see the CONNECT host
only. A command could ask for `CONNECT registry.npmjs.org:443` and then open TLS with the SNI of any other site on
the same CDN — measured: npm's address answers 200 for `developers.cloudflare.com` by SNI,
PyPI's (Fastly) for `www.python.org` — and so reach any site of the CDN, which is the open web. This guard closes
that: after the upstream Squid has accepted the CONNECT (its own address and name checks), it reads the client's
TLS ClientHello and splices the tunnel only when the SNI is the CONNECT host itself; anything else — another name, no
SNI, not TLS, a handshake it cannot read — is cut before a byte reaches the upstream. TLS is never opened.

What it cannot see: the HTTP `Host` inside TLS. A CDN that routes by `Host` whatever the SNI (Fastly did: SNI
`pypi.org` with `Host: www.fastly.com` answered fastly.com) still lets a command reach its other customers through
an allowed name (docs/platform.md, «The room»).

Standard library only; run as `python -m retinue.sniguard` with `RETINUE_SNI_UPSTREAM=host:port`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re

log = logging.getLogger("retinue.sniguard")

HEAD_MAX = 8192             # bytes of a CONNECT request's head
HELLO_MAX = 65536           # bytes of a ClientHello, all its records
READ_TIMEOUT_S = 15.0       # the client's request and its ClientHello; the upstream's answer
CONNECTIONS_MAX = 512       # open tunnels at once; one over is refused
CHUNK = 65536
_CONNECT = re.compile(rb"CONNECT ([A-Za-z0-9.-]{1,253}):(\d{1,5}) HTTP/1\.[01]\r\n")


class Refused(Exception):
    """Why a tunnel is cut: said in the log, never to the client beyond a status line."""


def sni(hello: bytes) -> str | None:
    """The host name of a TLS ClientHello's server_name extension; None when it has none. `hello` is the handshake
    message whole (type, length, body). Raises Refused when it is not a ClientHello or does not parse."""
    try:
        if hello[0] != 1:
            raise Refused("not a ClientHello")
        body = hello[4:4 + int.from_bytes(hello[1:4], "big")]
        at = 2 + 32                                         # client_version, random
        at += 1 + body[at]                                  # session_id
        at += 2 + int.from_bytes(body[at:at + 2], "big")    # cipher_suites
        at += 1 + body[at]                                  # compression_methods
        if at == len(body):
            return None                                     # no extensions at all
        end = at + 2 + int.from_bytes(body[at:at + 2], "big")
        at += 2
        if end > len(body):
            raise Refused("extensions past the message")
        while at + 4 <= end:
            kind, size = int.from_bytes(body[at:at + 2], "big"), int.from_bytes(body[at + 2:at + 4], "big")
            data = body[at + 4:at + 4 + size]
            at += 4 + size
            if kind != 0:
                continue
            names, n = data[2:2 + int.from_bytes(data[:2], "big")], 0
            while n + 3 <= len(names):
                length = int.from_bytes(names[n + 1:n + 3], "big")
                if names[n] == 0:
                    return names[n + 3:n + 3 + length].decode("ascii")
                n += 3 + length
            raise Refused("a server_name extension without a host name")
        return None
    except (IndexError, UnicodeDecodeError):
        raise Refused("a ClientHello that does not parse") from None


async def read_hello(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """The client's first TLS records, as read (to forward), and the handshake message they carry (to read). A
    ClientHello may span records; each must be a handshake record."""
    raw, handshake = b"", b""
    while len(handshake) < 4 or len(handshake) < 4 + int.from_bytes(handshake[1:4], "big"):
        header = await reader.readexactly(5)
        if header[0] != 22 or header[1] != 3:
            raise Refused("not TLS")
        payload = await reader.readexactly(int.from_bytes(header[3:5], "big"))
        raw, handshake = raw + header + payload, handshake + payload
        if len(raw) > HELLO_MAX:
            raise Refused("a ClientHello too long")
    return raw, handshake


def same_host(name: str, host: str) -> bool:
    return name.lower().rstrip(".") == host.lower().rstrip(".")


class Guard:
    def __init__(self, upstream: tuple[str, int]) -> None:
        self.upstream = upstream
        self.open = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_writer = None
        host = "?"
        counted = self.open < CONNECTIONS_MAX
        self.open += counted
        try:
            if not counted:
                raise Refused("too many tunnels")
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT_S)
            found = _CONNECT.match(head)
            if found is None:
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                raise Refused("not a CONNECT")
            host, port = found.group(1).decode(), int(found.group(2))
            with contextlib.suppress(ValueError):
                ipaddress.ip_address(host)
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                raise Refused("an address, not a name: no SNI to hold it to")
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(*self.upstream), READ_TIMEOUT_S)
            up_writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            answer = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT_S)
            if not re.match(rb"HTTP/1\.[01] 200", answer):
                writer.write(answer.split(b"\r\n", 1)[0] + b"\r\nConnection: close\r\n\r\n")
                raise Refused(f"upstream: {answer.split(b' ', 2)[1].decode(errors='replace')}")
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            raw, hello = await asyncio.wait_for(read_hello(reader), READ_TIMEOUT_S)
            name = sni(hello)
            if name is None or not same_host(name, host):
                raise Refused(f"SNI {name!r} is not the CONNECT host")
            log.info("tunnel %s", host)
            up_writer.write(raw)
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        except Refused as exc:
            log.warning("refused %s: %s", host, exc)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as exc:
            log.info("closed %s: %s", host, type(exc).__name__)
        finally:
            self.open -= counted
            for w in (writer, up_writer):
                if w is not None:
                    w.close()
                    with contextlib.suppress(Exception):
                        await w.wait_closed()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


async def serve(listen: tuple[str, int], upstream: tuple[str, int]) -> asyncio.Server:
    guard = Guard(upstream)
    return await asyncio.start_server(guard.handle, *listen, limit=HEAD_MAX)


def _address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"not host:port: {text!r}")
    return host, int(port)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    listen = _address(os.environ.get("RETINUE_SNI_LISTEN", "0.0.0.0:3128"))
    upstream = _address(os.environ.get("RETINUE_SNI_UPSTREAM", ""))

    async def run() -> None:
        server = await serve(listen, upstream)
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
