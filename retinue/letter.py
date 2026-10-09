"""A letter as code reads it, before any model does: who wrote it, the subject, the text a person sees, how much of
its HTML a person does not see, the attachments by number. Standard `email` with the modern policy: the bytes are
somebody else's, so nothing here raises on a broken letter — it reads what it can.

The collector parses (it fetched the bytes from the mailbox); the router gets the result as JSON.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser

TEXT_CHARS = 50_000   # the visible text kept of one letter
LINE_CHARS = 300      # a header kept as one line: subject, names
NO_TEXT = ("script", "style", "head", "title", "template")  # never text a person reads
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCKS = {"p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article",
          "blockquote", "pre", "ul", "ol", "hr", "header", "footer"}
# CSS that keeps text from a person's eyes: a mailing's preheader, a prompt for the model in white zero-size text.
HIDES = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all", re.IGNORECASE)
ZERO = re.compile(r"(?:font-size|opacity|max-height)\s*:\s*([0-9.]+)", re.IGNORECASE)


@dataclass
class Letter:
    sender: str = ""          # the address in From, lower case
    name: str = ""            # the display name in From
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    date: str = ""            # the Date header as written; the mailbox's own time comes with the item
    message_id: str = ""
    in_reply_to: str = ""
    text: str = ""            # what a person sees: the plain part, else the visible text of the HTML
    hidden: int = 0           # characters of HTML text a person does not see; never in `text`
    unsubscribe: bool = False  # List-Unsubscribe: the sender says itself that this is a mailing
    auto: str = ""            # Auto-Submitted (RFC 3834) when it is not «no»
    attachments: list[dict] = field(default_factory=list)  # {"part": n, "name", "type", "size"}

    def as_dict(self) -> dict:
        return asdict(self)


def _hides(style: str) -> bool:
    if HIDES.search(style):
        return True
    for value in ZERO.findall(style):
        try:
            if float(value) == 0:
                return True
        except ValueError:
            continue
    return False


class _Visible(HTMLParser):
    """The text of an HTML letter as a person sees it, and how much of it is hidden by CSS or `hidden`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, bool]] = []  # (tag, hides)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in BLOCKS:
            self.parts.append("\n")
        if tag in VOID:
            return
        found = dict(attrs)
        self.open.append((tag, tag in NO_TEXT or "hidden" in found or _hides(found.get("style") or "")))

    def handle_startendtag(self, tag, attrs):
        if tag in BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in BLOCKS:
            self.parts.append("\n")
        for i in range(len(self.open) - 1, -1, -1):  # unclosed tags inside are closed with it
            if self.open[i][0] == tag:
                del self.open[i:]
                return

    def handle_data(self, data):
        if not any(hides for _, hides in self.open):
            self.parts.append(data)
        elif not any(tag in NO_TEXT for tag, _ in self.open):
            self.hidden += len(" ".join(data.split()))


def tidy(text: str) -> str:
    """Spaces collapsed in each line, at most one empty line in a row."""
    lines, empty = [], False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(line.split())
        if line or not empty:
            lines.append(line)
        empty = not line
    return "\n".join(lines).strip()


def visible(html: str) -> tuple[str, int]:
    """(the text a person sees, characters hidden from them)."""
    parser = _Visible()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # a broken tail: what was read stands
        pass
    return tidy("".join(parser.parts)), parser.hidden


def _line(value) -> str:
    return " ".join(str(value or "").split())[:LINE_CHARS]


def _header(message, name: str) -> str:
    try:
        return _line(message.get(name, ""))
    except Exception:  # a header the modern policy cannot parse is left out, not the letter
        return ""


def _addresses(message, name: str) -> list[tuple[str, str]]:
    try:
        return [(_line(n), a.strip().lower()) for n, a in email.utils.getaddresses([_header(message, name)]) if a]
    except Exception:
        return []


def _content(part) -> str:
    try:
        return str(part.get_content())
    except Exception:  # an unknown or lying charset
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def _parts(message):
    """Every part with its number: the number names an attachment for as long as the letter does not change."""
    return list(enumerate(message.walk()))


def _is_attachment(part, body_parts) -> bool:
    if part.is_multipart() or any(part is b for b in body_parts):
        return False
    return part.get_content_disposition() == "attachment" or bool(part.get_filename())


def parse(raw: bytes) -> Letter:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    letter = Letter(subject=_header(message, "Subject"), date=_header(message, "Date"),
                    message_id=_header(message, "Message-ID"), in_reply_to=_header(message, "In-Reply-To"))
    sender = _addresses(message, "From")
    if sender:
        letter.name, letter.sender = sender[0]
    letter.to = [address for _, address in _addresses(message, "To")]
    letter.cc = [address for _, address in _addresses(message, "Cc")]
    letter.unsubscribe = bool(_header(message, "List-Unsubscribe"))
    auto = _header(message, "Auto-Submitted").lower()
    letter.auto = "" if auto in ("", "no") else auto
    plain, html = message.get_body(preferencelist=("plain",)), message.get_body(preferencelist=("html",))
    if plain is not None:
        letter.text = tidy(_content(plain))
    if not letter.text and html is not None:  # no plain part, or an empty one beside a real HTML
        letter.text, letter.hidden = visible(_content(html))
    letter.text = letter.text[:TEXT_CHARS]
    for number, part in _parts(message):
        if _is_attachment(part, [plain, html]):
            payload = part.get_payload(decode=True)
            letter.attachments.append({"part": number, "name": _line(part.get_filename() or f"вложение {number}"),
                                       "type": part.get_content_type(), "size": len(payload or b"")})
    return letter


def attachment(raw: bytes, number: int) -> tuple[str, str, bytes]:
    """(name, media type, bytes) of the attachment `number` of `parse`. KeyError when there is no such one."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    plain, html = message.get_body(preferencelist=("plain",)), message.get_body(preferencelist=("html",))
    for n, part in _parts(message):
        if n == number and _is_attachment(part, [plain, html]):
            return (_line(part.get_filename() or f"вложение {number}"), part.get_content_type(),
                    part.get_payload(decode=True) or b"")
    raise KeyError(number)
