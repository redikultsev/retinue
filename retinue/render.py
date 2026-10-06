"""Agent Markdown -> Matrix message (plain body + HTML), limited to what Element X renders on every platform.

Element X shows bold, italic, strikethrough, code, links, lists, quotes and headings, but no tables and no
external images. Tables become lists here, in code, so the result does not depend on the model obeying a prompt.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

_md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def _image_as_link(self, tokens, idx, options, env) -> str:
    # Matrix clients load only mxc:// images; an external URL becomes a plain link.
    token = tokens[idx]
    alt = escapeHtml(token.content or token.attrGet("src") or "")
    return f'<a href="{escapeHtml(token.attrGet("src") or "")}">{alt}</a>'


_md.add_render_rule("image", _image_as_link)


def _row(cells: list[str]) -> str:
    cells = [c for c in cells if c]
    if not cells:
        return ""
    head = cells[0].strip("*_ ")
    return f"- **{head}**" + (" — " + " · ".join(cells[1:]) if cells[1:] else "")


def tables_to_lists(text: str) -> str:
    """Replace each Markdown table with a bullet list: first cell bold, the rest after a dash. Header row dropped."""
    lines = text.split("\n")
    tokens = _md.parse(text)
    out: list[str] = []
    pos = i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.type == "table_open" and token.map and token.level == 0:
            start, end = token.map
            rows: list[list[str]] = []
            row: list[str] = []
            in_header = False
            i += 1
            while tokens[i].type != "table_close":
                t = tokens[i]
                if t.type in ("thead_open", "thead_close"):
                    in_header = t.type == "thead_open"
                elif t.type == "tr_open":
                    row = []
                elif t.type == "inline":
                    row.append(t.content.strip())
                elif t.type == "tr_close" and not in_header:
                    rows.append(row)
                i += 1
            out += lines[pos:start] + [_row(r) for r in rows] + [""]
            pos = end
        i += 1
    out += lines[pos:]
    return "\n".join(out)


def render(text: str) -> tuple[str, str]:
    """Return (body, formatted_body) for an m.room.message."""
    body = tables_to_lists(text).strip() or "(пусто)"
    return body, _md.render(body).strip()


# --- Telegram -------------------------------------------------------------------------------------------------
# Telegram HTML knows b, i, s, code, pre, blockquote — no lists, headings or tables. Lists become "•" lines,
# headings become bold lines. Output is split into messages of at most TELEGRAM_LIMIT characters on block borders.
#
# No address leaves here as a link. Telegram makes any bare address clickable, and a click (or a link preview)
# carries whatever the model wrote into the address to a stranger's server. So every address — a Markdown
# link's target, a bare URL, a domain, an e-mail — is wrapped in <code>: shown, copyable, not clickable.

TELEGRAM_LIMIT = 4000
_FORMAT = {"strong": "b", "em": "i", "s": "s"}
_ADDRESS = re.compile(
    r"""(?ix)
      (?:[a-z][a-z0-9+.\-]*://|www\.|mailto:|tel:)[^\s<>]+                    # anything with a scheme, or www.
    | [\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+                                          # e-mail
    | (?<![\w@])(?:[\w\-]+\.)+[^\W\d_]{2,}(?::\d+)?(?:[/?\#][^\s<>]*)?         # bare domain, optional port and path
    """)
_TRAILING = ".,;:!?)»\"'"


def _tg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_code(text: str, outer: list[str]) -> str:
    """<code> may not sit inside b/i/s in Telegram: close the open tags around it and open them again."""
    closing = "".join(f"</{tag}>" for tag in reversed(outer))
    opening = "".join(f"<{tag}>" for tag in outer)
    return f"{closing}<code>{_tg_escape(text)}</code>{opening}"


def _tg_text(text: str, outer: list[str] | tuple = ()) -> str:
    """Escape plain text and wrap every address in <code>."""
    out, pos = [], 0
    for match in _ADDRESS.finditer(text):
        address = match.group().rstrip(_TRAILING)
        if not address:
            continue
        out.append(_tg_escape(text[pos:match.start()]))
        out.append(_tg_code(address, list(outer)))
        pos = match.start() + len(address)
    out.append(_tg_escape(text[pos:]))
    return "".join(out)


def _tg_inline(children, outer: list[str] | tuple = ()) -> str:
    out, open_tags, links = [], list(outer), []  # links: stack of (target, position in out where the text starts)
    for t in children or []:
        if t.type == "text":
            out.append(_tg_text(t.content, open_tags))
        elif t.type == "code_inline":
            out.append(_tg_code(t.content, open_tags))
        elif t.type in ("softbreak", "hardbreak"):
            out.append("\n")
        elif t.type == "link_open":
            links.append((t.attrGet("href") or "", len(out)))
        elif t.type == "link_close":
            target, start = links.pop()
            # The link's words stay as text; its target follows in <code>, unless the words already are the target.
            if target and _tg_escape(target) not in "".join(out[start:]):
                out.append(" (" + _tg_code(target, open_tags) + ")")
        elif t.type == "image":
            src = t.attrGet("src") or ""
            label = _tg_text(t.content, open_tags) if t.content and t.content != src else ""
            out.append(label + (" (" if label else "") + _tg_code(src, open_tags) + (")" if label else ""))
        elif t.type.removesuffix("_open") in _FORMAT and t.type.endswith("_open"):
            tag = _FORMAT[t.type.removesuffix("_open")]
            open_tags.append(tag)
            out.append(f"<{tag}>")
        elif t.type.removesuffix("_close") in _FORMAT and t.type.endswith("_close"):
            tag = _FORMAT[t.type.removesuffix("_close")]
            open_tags.remove(tag)
            out.append(f"</{tag}>")
        elif t.content:
            out.append(_tg_text(t.content, open_tags))
    return "".join(out)


def _tg_blocks(tokens) -> list[str]:
    """Render each top-level block separately so long answers split between blocks, never inside a tag."""
    blocks, out, lists = [], [], []  # lists: stack of [kind, counter]
    heading = False
    for t in tokens:
        kind = t.type
        if kind == "heading_open":
            heading = True
            out.append("<b>")
        elif kind == "heading_close":
            heading = False
            out.append("</b>")
        elif kind in ("bullet_list_open", "ordered_list_open"):
            lists.append(["ol" if kind.startswith("ordered") else "ul", int(t.attrGet("start") or 1) - 1])
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            lists[-1][1] += 1
            marker = f"{lists[-1][1]}." if lists[-1][0] == "ol" else "•"
            out.append("\n" * bool(out and not out[-1].endswith("\n")) + "   " * (len(lists) - 1) + marker + " ")
        elif kind == "paragraph_close" and not t.hidden:
            out.append("\n")
        elif kind == "blockquote_open":
            out.append("<blockquote>")
        elif kind == "blockquote_close":
            if out and out[-1] == "\n":
                out.pop()
            out.append("</blockquote>")
        elif kind in ("fence", "code_block"):
            lang = (t.info or "").split()[0] if t.info else ""
            cls = f' class="language-{_tg_escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{_tg_escape(t.content.rstrip())}</code></pre>")
        elif kind == "hr":
            out.append("———")
        elif kind == "inline":
            out.append(_tg_inline(t.children, ["b"] if heading else []))
        if t.level == 0 and t.nesting <= 0:  # a top-level block just ended
            block = re.sub(r"<(b|i|s)></\1>", "", "".join(out)).strip()  # pairs left empty around a <code>
            if block:
                blocks.append(block)
            out = []
    return blocks


def tg_plain(html: str) -> str:
    """Telegram HTML -> the same text with no markup except <code> around addresses. The way out when Telegram
    refuses our markup: still valid HTML, and still no clickable address."""
    plain = re.sub(r"<[^>]+>", "", html).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return _tg_text(plain)


def render_telegram(text: str) -> list[str]:
    """Agent Markdown -> Telegram HTML messages (each within TELEGRAM_LIMIT)."""
    blocks = _tg_blocks(_md.parse(tables_to_lists(text)))
    messages, current = [], ""
    for block in blocks:
        if len(block) > TELEGRAM_LIMIT:  # one huge block (e.g. code): send it as plain text pieces
            plain = re.sub(r"<[^>]+>", "", block).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            step = TELEGRAM_LIMIT // 2  # room for escaping and for <code> around addresses
            pieces = [_tg_text(plain[i:i + step]) for i in range(0, len(plain), step)]
        else:
            pieces = [block]
        for piece in pieces:
            if current and len(current) + 2 + len(piece) > TELEGRAM_LIMIT:
                messages.append(current)
                current = ""
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        messages.append(current)
    return messages or ["(пусто)"]
