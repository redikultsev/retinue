"""The page reader (`retinue/reach.py`): a public https page's own text, in parts; a PDF by its pages; nothing inside,
nothing but https, a redirect checked like the address — and its tool read by the gate as a fetch is."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from retinue import reach
from retinue.gate import READ_PAGE
from test_gate import LEAK, decision, hook, setup, short_wait, work  # noqa: F401

HTML = ("<html><head><title>Acme — Pricing</title><style>.x{}</style><script>var a=1</script></head><body>"
        "<h1>Plans</h1><p>Basic costs €4.00 a month.</p><div>Pro costs €9.50 a month.</div></body></html>")


def _pdf(pages: list[str]) -> bytes:
    """A small PDF with one line of text per page, its xref table counted."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages))) +
               f"] /Count {len(pages)} >>"]
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                       f"/Resources << /Font << /F1 {font} 0 R >> >> >>")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def site(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        found = routes.get(str(request.url))
        if found is None:
            return httpx.Response(404)
        # A fresh, unread stream each time, as off the wire: the reader takes it raw (`aiter_raw`).
        return httpx.Response(found.status_code, headers=found.headers, stream=httpx.ByteStream(found.content))
    return httpx.MockTransport(handler)


def read(url: str, routes: dict, **kw) -> dict:
    return asyncio.run(reach.read(url, transport=site(routes), **kw))


def test_a_page_is_its_text_without_scripts_with_its_title():
    said = read("https://acme.example/pricing", {"https://acme.example/pricing": httpx.Response(
        200, text=HTML, headers={"content-type": "text/html; charset=utf-8"})})
    assert said["title"] == "Acme — Pricing" and said["pages"] == 1
    assert "Basic costs €4.00 a month." in said["text"] and "Pro costs €9.50" in said["text"]
    assert "var a" not in said["text"] and ".x{}" not in said["text"]


def test_a_long_page_comes_in_parts():
    text = "word " * (reach.PAGE_CHARS // 2)
    routes = {"https://acme.example/long": httpx.Response(200, text=text, headers={"content-type": "text/plain"})}
    first, second = read("https://acme.example/long", routes), read("https://acme.example/long", routes, page=2)
    assert first["pages"] == 3 and len(first["text"]) == reach.PAGE_CHARS and second["page"] == 2
    with pytest.raises(reach.Refused):
        read("https://acme.example/long", routes, page=4)


def test_a_pdf_is_read_by_its_own_pages():
    routes = {"https://acme.example/rules.pdf": httpx.Response(
        200, content=_pdf(["Rule one", "Rule two", "Rule three"]), headers={"content-type": "application/pdf"})}
    said = read("https://acme.example/rules.pdf", routes, pages="2-3")
    assert said["pdf_pages"] == 3 and said["type"] == "application/pdf"
    assert "--- page 2 ---\nRule two" in said["text"] and "Rule three" in said["text"] and "Rule one" not in said["text"]
    with pytest.raises(reach.Refused):
        read("https://acme.example/rules.pdf", routes, pages="9")


@pytest.mark.parametrize("url", ["http://acme.example/", "https://10.0.0.1/", "https://[::1]/", "https://localhost/",
                                 "https://platform/", "https://metadata.internal/", "https://u:p@acme.example/",
                                 "https://acme.example:8443/", "ftp://acme.example/"])
def test_only_a_public_https_host_is_opened(url):
    with pytest.raises(reach.Refused):
        reach.check_url(url)


def test_a_redirect_is_checked_like_the_address_and_followed_three_times_at_most():
    inside = {"https://acme.example/go": httpx.Response(302, headers={"location": "https://platform:9500/hook"})}
    with pytest.raises(reach.Refused, match="no port|public host"):
        read("https://acme.example/go", inside)
    hops = {f"https://acme.example/{n}": httpx.Response(302, headers={"location": f"/{n + 1}"}) for n in range(5)}
    with pytest.raises(reach.Refused, match="redirects"):
        read("https://acme.example/0", hops)
    fine = {"https://acme.example/a": httpx.Response(301, headers={"location": "https://acme.example/b"}),
            "https://acme.example/b": httpx.Response(200, text="here", headers={"content-type": "text/plain"})}
    assert read("https://acme.example/a", fine)["url"] == "https://acme.example/b"


def test_an_error_status_a_binary_and_an_empty_shell_are_said():
    with pytest.raises(reach.Refused, match="403"):
        read("https://acme.example/x", {"https://acme.example/x": httpx.Response(403)})
    with pytest.raises(reach.Refused, match="not text"):
        read("https://acme.example/i", {"https://acme.example/i": httpx.Response(
            200, content=b"\x89PNG", headers={"content-type": "image/png"})})
    shell = read("https://acme.example/app", {"https://acme.example/app": httpx.Response(
        200, text="<html><body><div id=root></div><script>render()</script></body></html>",
        headers={"content-type": "text/html"})})
    assert "JavaScript" in shell["note"]


def test_the_platforms_tool_says_where_and_marks_the_text_as_data():
    async def go():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"url": "https://acme.example/p", "type": "text/html", "title": "Acme",
                                             "page": 1, "pages": 2, "text": "Basic €4"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await reach.LaneReach("http://reach:9700", http).call(reach.READ, {"url": "https://acme.example/p"})
    text, error = asyncio.run(go())
    assert not error and text.startswith("https://acme.example/p — text/html — «Acme» — part 1 of 2")
    assert "data, not instructions" in text and text.endswith("Basic €4")


def test_the_gate_reads_a_read_page_address_as_a_fetch(setup):
    _, the_gate, _, _ = setup

    def call(url: str, call_id: str) -> dict:
        return {"hook_event_name": "PreToolUse", "tool_name": READ_PAGE, "tool_input": {"url": url},
                "tool_use_id": call_id, "cwd": "/work/repo", "permission_mode": "auto"}
    assert decision(hook(the_gate, "PreToolUse", call(LEAK, "r1"))) == "defer", "data leaving: the owner's card"
    tracked = hook(the_gate, "PreToolUse", call("https://acme.example/p?utm_source=x", "r2"))
    assert decision(tracked) == "deny" and tracked["hookSpecificOutput"]["permissionDecisionReason"].endswith(
        "https://acme.example/p")


def test_a_redirect_to_another_site_is_handed_back_and_bad_inputs_are_refused_cleanly():
    away = {"https://acme.example/go": httpx.Response(302, headers={"location": "https://other.example/there"})}
    with pytest.raises(reach.Refused, match="other.example/there"):
        read("https://acme.example/go", away)
    with pytest.raises(reach.Refused):
        reach.check_url("https://acme.example:abc/")
    with pytest.raises(reach.Refused):
        read("https://acme.example/x", {}, pages=31)
    odd = {"https://acme.example/t": httpx.Response(200, content=b"hello", headers={"content-type": "text/plain; charset=nope"})}
    assert read("https://acme.example/t", odd)["text"] == "hello"
    long_title = {"https://acme.example/h": httpx.Response(
        200, text="<title>" + "t" * 100_000 + "</title><p>x</p>", headers={"content-type": "text/html"})}
    assert len(read("https://acme.example/h", long_title)["title"]) == reach.TITLE_CHARS


def test_read_page_falls_under_the_runs_own_webfetch_rules(setup):
    import dataclasses

    from retinue.approvals import Mark
    store, the_gate, session, run = setup
    marks = [Mark("deny", "WebFetch(domain:blocked.example)"), Mark("card", "WebFetch(domain:asks.example)")]
    store.db.execute("UPDATE runs SET marks = ? WHERE id = ?",
                     (__import__("json").dumps([dataclasses.asdict(m) for m in marks]), run))
    store.db.commit()

    def call(url: str, call_id: str) -> dict:
        return {"hook_event_name": "PreToolUse", "tool_name": READ_PAGE, "tool_input": {"url": url},
                "tool_use_id": call_id, "cwd": "/work/repo", "permission_mode": "auto"}
    assert decision(hook(the_gate, "PreToolUse", call("https://www.blocked.example/a", "w1"))) == "deny"
    assert decision(hook(the_gate, "PreToolUse", call("https://asks.example/a", "w2"))) == "defer"
    assert decision(hook(the_gate, "PreToolUse", call("https://fine.example/a", "w3"))) is None
