"""Agent Markdown -> Matrix message (plain body + HTML), limited to what Element X renders on every platform.

Element X shows bold, italic, strikethrough, code, links, lists, quotes and headings, but no tables and no
external images. Tables become lists here, in code, so the result does not depend on the model obeying a prompt.
"""

from __future__ import annotations

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
