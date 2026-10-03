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
# Telegram HTML knows b, i, s, code, pre, a, blockquote — no lists, headings or tables. Lists become "•" lines,
# headings become bold lines. Output is split into messages of at most TELEGRAM_LIMIT characters on block borders.

TELEGRAM_LIMIT = 4000
_INLINE = {"strong_open": "<b>", "strong_close": "</b>", "em_open": "<i>", "em_close": "</i>",
           "s_open": "<s>", "s_close": "</s>", "link_close": "</a>"}


def _tg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_inline(children) -> str:
    out = []
    for t in children or []:
        if t.type == "text":
            out.append(_tg_escape(t.content))
        elif t.type == "code_inline":
            out.append(f"<code>{_tg_escape(t.content)}</code>")
        elif t.type in ("softbreak", "hardbreak"):
            out.append("\n")
        elif t.type == "link_open":
            out.append(f'<a href="{_tg_escape(t.attrGet("href") or "")}">')
        elif t.type == "image":
            src = _tg_escape(t.attrGet("src") or "")
            out.append(f'<a href="{src}">{_tg_escape(t.content or src)}</a>')
        else:
            out.append(_INLINE.get(t.type, _tg_escape(t.content) if t.content else ""))
    return "".join(out)


def _tg_blocks(tokens) -> list[str]:
    """Render each top-level block separately so long answers split between blocks, never inside a tag."""
    blocks, out, lists = [], [], []  # lists: stack of [kind, counter]
    for t in tokens:
        kind = t.type
        if kind == "heading_open":
            out.append("<b>")
        elif kind == "heading_close":
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
            out.append(_tg_inline(t.children))
        if t.level == 0 and t.nesting <= 0:  # a top-level block just ended
            block = "".join(out).strip()
            if block:
                blocks.append(block)
            out = []
    return blocks


def render_telegram(text: str) -> list[str]:
    """Agent Markdown -> Telegram HTML messages (each within TELEGRAM_LIMIT)."""
    blocks = _tg_blocks(_md.parse(tables_to_lists(text)))
    messages, current = [], ""
    for block in blocks:
        if len(block) > TELEGRAM_LIMIT:  # one huge block (e.g. code): send it as plain text pieces
            plain = re.sub(r"<[^>]+>", "", block).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            pieces = [_tg_escape(plain[i:i + TELEGRAM_LIMIT - 100]) for i in range(0, len(plain), TELEGRAM_LIMIT - 100)]
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
