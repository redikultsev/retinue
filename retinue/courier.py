"""The courier's own code: what may leave the system for a third party, in what form, and the card the owner presses
«Отправить» under. Standard library only — the router imports it to build a draft and its card, and each sender
(`sender.py` for Gmail, `tgbusiness.py` for Telegram) imports it to check the same envelope again before it sends.

An envelope is everything that decides where a message goes and what it says: the channel, the account it leaves
from, the one recipient, the subject and the thread, the text. The owner confirms the whole envelope — its digest is
bound to his button — and a sender sends exactly the envelope whose digest it is given, or nothing.

The text is refused, never repaired: a control, bidi, zero-width or unassigned character is a reason to say no,
because what the owner sees must be what is hashed and what leaves. What a person may not notice in a text he
approves — links, addresses, numbers next to «код» or «паспорт» — is named on the card by code (`findings`).
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, getaddresses
from urllib.parse import quote, urlsplit

MAIL, TELEGRAM = "mail", "telegram"
CHANNELS = {MAIL: "Gmail", TELEGRAM: "Telegram"}
MAX_TEXT = 2000           # v1: the card is one message, the text has no tail out of sight
MAX_SUBJECT = 200
MAX_REFERENCES = 30       # the newest ids of a thread's References are kept; Gmail threads by threadId anyway
ADDRESS = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")  # ASCII, lower case: one mailbox
MESSAGE_ID = re.compile(r"<[^<>\s\"]{1,250}>")
CHAT = re.compile(r"-?\d{1,20}")
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
DEEP_LINK_MAX = 2048      # a longer t.me link is not offered: the text is copied from the card instead
# Characters nobody sees, or that change how the rest is shown: controls, format characters (bidi overrides,
# zero-width, BOM, soft hyphen), private use, surrogates, unassigned, line and paragraph separators.
INVISIBLE = {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"}


class Refused(ValueError):
    """Not sent, not shown: the text says what to fix."""


def clean(text: str, cap: int = MAX_TEXT, what: str = "текст") -> str:
    """The text as it will be shown, hashed and sent: NFC, line ends as \\n, no spaces around. An invisible or
    control character (a tab too) is refused with its code and place, never stripped."""
    text = unicodedata.normalize("NFC", str(text)).replace("\r\n", "\n")
    for i, char in enumerate(text):
        if char != "\n" and (unicodedata.category(char) in INVISIBLE):
            raise Refused(f"{what}: невидимый или управляющий символ U+{ord(char):04X} на {i + 1}-м месте — убери его")
    text = text.strip()
    if not text:
        raise Refused(f"{what}: пусто")
    if len(text) > cap:
        raise Refused(f"{what}: {len(text)} знаков, можно не больше {cap}")
    return text


def line(text, cap: int = 80) -> str:
    """Somebody else's words for one line of a card — a name, a subject: invisible characters dropped, one line,
    cut. Shown, never obeyed."""
    kept = "".join(c for c in unicodedata.normalize("NFC", str(text or ""))
                   if unicodedata.category(c) not in INVISIBLE or c in "\t\n")
    kept = " ".join(kept.split())
    return kept if len(kept) <= cap else kept[:cap - 1] + "…"


def reply_subject(subject: str) -> str:
    """«Re: » and the parent's subject — one «Re:», as RFC 5322 §3.6.5 has it."""
    subject = line(subject, MAX_SUBJECT - 4)
    return subject if re.match(r"(?i)re\s*:", subject) else f"Re: {subject}".strip()


# --- the envelope ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Envelope:
    channel: str              # mail | telegram
    account: str              # mail: the mailbox it leaves from; telegram: the owner's user id
    to: str                   # mail: one address; telegram: the chat id
    text: str
    name: str = ""            # how the card names the recipient; shown, never a header
    subject: str = ""         # mail
    thread: str = ""          # mail: Gmail's threadId of a reply
    in_reply_to: str = ""     # mail: the parent's Message-ID
    references: tuple[str, ...] = field(default_factory=tuple)
    reply_to_message: int = 0  # telegram: the message answered

    def as_dict(self) -> dict:
        data = asdict(self)
        data["references"] = list(self.references)
        return data

    @classmethod
    def of(cls, data) -> Envelope:
        """From JSON — a sender's request. Anything of another shape is refused."""
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise Refused("конверт: не те поля")
        try:
            refs = tuple(str(r) for r in data["references"])
            values = {k: v for k, v in data.items() if k != "references"}
            if not all(isinstance(v, str) for k, v in values.items() if k != "reply_to_message") or \
                    type(values["reply_to_message"]) is not int:
                raise TypeError
            return cls(**values, references=refs)
        except (TypeError, ValueError):
            raise Refused("конверт: не те поля") from None


def digest(envelope: Envelope) -> str:
    """What the owner's button is bound to: the whole envelope, canonical JSON, SHA-256."""
    canonical = json.dumps(envelope.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def check(envelope: Envelope) -> Envelope:
    """Everything checked again by the one who sends: the channel, one recipient of the right form, the account,
    the thread's ids, the text in its canonical form."""
    e = envelope
    if e.channel == MAIL:
        if not ADDRESS.fullmatch(e.to) or not ADDRESS.fullmatch(e.account):
            raise Refused("адрес: один адрес латиницей, без имени")
        if not e.subject or e.subject != line(e.subject, MAX_SUBJECT):
            raise Refused("тема: пустая или не в одну строку")
        if (e.in_reply_to and not MESSAGE_ID.fullmatch(e.in_reply_to)) or len(e.references) > MAX_REFERENCES or \
                not all(MESSAGE_ID.fullmatch(r) for r in e.references) or (e.references and not e.in_reply_to):
            raise Refused("тред: не те Message-ID")
        if e.thread and not re.fullmatch(r"[0-9a-f]{1,32}", e.thread):
            raise Refused("тред: не тот threadId")
        if e.reply_to_message:
            raise Refused("у письма нет reply_to_message")
    elif e.channel == TELEGRAM:
        if not CHAT.fullmatch(e.to) or not CHAT.fullmatch(e.account) or e.reply_to_message < 0:
            raise Refused("чат: не тот id")
        if e.subject or e.thread or e.in_reply_to or e.references:
            raise Refused("у сообщения Telegram нет темы и треда")
    else:
        raise Refused(f"канал: {e.channel!r}")
    if clean(e.text) != e.text:
        raise Refused("текст: не в каноническом виде")
    return e


def message_id(key: str, account: str) -> str:
    """Our own Message-ID: the draft's key in it, so the letter is recognised when it comes back from Sent."""
    return f"<retinue.{key}@{account.rsplit('@', 1)[-1]}>"


def mail_bytes(envelope: Envelope, key: str) -> bytes:
    """The letter, RFC 5322, plain text only: From and To are bare addresses, the subject and the thread's
    headers from the envelope. Read back after it is built: exactly one recipient, the envelope's, and no copy."""
    e = check(envelope)
    if e.channel != MAIL:
        raise Refused("не письмо")
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = Address(addr_spec=e.account)
    message["To"] = Address(addr_spec=e.to)
    message["Subject"] = e.subject
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = message_id(key, e.account)
    if e.in_reply_to:
        message["In-Reply-To"] = e.in_reply_to
        message["References"] = " ".join(e.references)
    message.set_content(e.text)
    raw = message.as_bytes()
    back = email.message_from_bytes(raw, policy=email.policy.default)
    to = [address.lower() for _, address in getaddresses([str(v) for v in back.get_all("To", [])])]
    if to != [e.to] or back.get_all("Cc") or back.get_all("Bcc") or back.get_content().replace("\r\n", "\n").rstrip("\n") != e.text:
        raise Refused("письмо собралось не так, как в конверте")
    return raw


# --- sending once -------------------------------------------------------------------------------------------------

KEY = re.compile(r"[0-9a-f]{8}")  # a draft's id: what the router asks a sender to send, once and only once
DAY = 86400


class Ledger:
    """A sender's own SQLite: every key it was asked to send and what became of it. A key is sent at most once —
    asked again (a double press, the router's restart, its look at a doubtful one), the sender answers what it knows
    and sends nothing. There is no idempotency key in Gmail or the Bot API (research 60 §2, §4): this is it."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sends (
                key TEXT PRIMARY KEY,
                digest TEXT NOT NULL,
                recipient TEXT NOT NULL,
                state TEXT NOT NULL,         -- sending | sent | failed | unknown
                result TEXT NOT NULL DEFAULT '{}',
                at REAL NOT NULL
            );""")
        self.db.commit()

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT state, result FROM sends WHERE key = ?", (key,)).fetchone()
        return {"state": row[0], "result": json.loads(row[1])} if row else None

    def claim(self, key: str, digest_: str, recipient: str, now: float) -> bool:
        """Before the call that sends: the key is taken, so a crash during the call leaves `sending` — unknown."""
        cursor = self.db.execute("INSERT OR IGNORE INTO sends (key, digest, recipient, state, at) VALUES (?, ?, ?, "
                                 "'sending', ?)", (key, digest_, recipient, now))
        self.db.commit()
        return cursor.rowcount == 1

    def finish(self, key: str, state: str, result: dict) -> None:
        self.db.execute("UPDATE sends SET state = ?, result = ? WHERE key = ?",
                        (state, json.dumps(result, ensure_ascii=False), key))
        self.db.commit()

    def since(self, ts: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM sends WHERE at >= ?", (ts,)).fetchone()[0]


Transport = Callable[[Envelope, str], Awaitable[tuple[str, dict]]]  # -> ("sent" | "failed" | "unknown", result)


async def send_once(ledger: Ledger, body, channel: str, transport: Transport, now: float | None = None) -> dict:
    """The one way a sender sends: the key, the envelope checked again, its digest equal to the one the owner's
    button was bound to, the key never seen — then the key is taken and the transport called once. No ceiling of
    its own: every send is the owner's press under its card (his decision of 2026-10-10). {"status": sent | failed | unknown | refused, ...}; `repeat` when the key was
    known. A refusal sends nothing and keeps nothing."""
    now = time.time() if now is None else now
    body = body if isinstance(body, dict) else {}
    key = str(body.get("key") or "")
    if not KEY.fullmatch(key):
        return {"status": "refused", "error": "ключ: не тот"}
    try:
        envelope = check(Envelope.of(body.get("envelope")))
    except Refused as exc:
        return {"status": "refused", "error": str(exc)}
    if envelope.channel != channel:
        return {"status": "refused", "error": f"этот отправитель не шлёт в {envelope.channel}"}
    if digest(envelope) != body.get("digest"):
        return {"status": "refused", "error": "конверт не совпал с подтверждённым"}
    if known := ledger.get(key):
        state = "unknown" if known["state"] == "sending" else known["state"]
        return {**known["result"], "status": state, "repeat": True}
    if not ledger.claim(key, digest(envelope), envelope.to, now):
        return {"status": "unknown", "repeat": True}
    try:
        state, result = await transport(envelope, key)
    except Exception as exc:  # whatever broke during the call: it may have gone — never «failed», never again
        state, result = "unknown", {"error": f"сбой отправителя: {type(exc).__name__}"}
    ledger.finish(key, state, result)
    return {**result, "status": state}


# --- what the owner might not notice ------------------------------------------------------------------------------

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"]+|(?<![\w@.-])(?:[\w-]+\.)+[^\W\d_]{2,}(?:[/?#][^\s<>\"]*)?")
PHONE = re.compile(r"(?<![\w+])\+?\d[\d ().-]{7,}\d(?!\w)")
NUMBER = re.compile(r"(?<![\d.,])\d{4,}(?![\d])")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
SECRET = re.compile(r"(?i)код|парол|pin|пин|cvv|cvc|паспорт|карт|сч[её]т|iban|password|code|passport|card|account")
FINDINGS = 12


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def findings(text: str, known: set[str] = frozenset()) -> list[str]:
    """«В тексте найдено»: every link whole (and that it carries parameters, and a domain not in Latin letters),
    an address not among the thread's participants, a phone, a number of four digits or more next to a word like
    «код», «пароль», «паспорт», «карта», an IBAN, a card number. In the order found, each once."""
    found: list[str] = []
    rest = text
    for match in EMAIL.finditer(text):
        address = match.group().lower()
        if address not in known:
            found.append(f"адрес {address}")
        rest = rest.replace(match.group(), " " * len(match.group()))
    for match in URL.finditer(rest):
        url = match.group().rstrip(".,;:!?)»\"'")
        said = f"ссылка {url}"
        host = urlsplit(url if "://" in url else f"https://{url}").hostname or ""
        if host and not host.isascii():
            try:
                said += f" (домен не латиницей: {host.encode('idna').decode()})"
            except UnicodeError:
                said += " (домен не латиницей)"
        if "?" in url:
            said += " — с параметрами"
        found.append(said)
    for match in IBAN.finditer(text):
        found.append(f"похоже на IBAN: {match.group()}")
    for match in CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append(f"похоже на номер карты: {match.group()}")
    for match in PHONE.finditer(rest):
        if sum(c.isdigit() for c in match.group()) >= 9:
            found.append(f"телефон {match.group().strip()}")
    for match in NUMBER.finditer(text):
        around = text[max(0, match.start() - 40):match.end() + 40]
        if word := SECRET.search(around):
            found.append(f"число {match.group()} рядом с «{word.group().lower()}»")
    unique = list(dict.fromkeys(f[:160] for f in found))
    return unique[:FINDINGS] + ([f"и ещё {len(unique) - FINDINGS}"] if len(unique) > FINDINGS else [])


# --- the card -----------------------------------------------------------------------------------------------------

@dataclass
class CardView:
    """A card as code built it: header lines, the exact text (shown verbatim, never as markup), a status line, and a
    link code made (a deep link to a chat) — or none."""
    head: list[str]
    body: str = ""
    status: str = ""
    link: tuple[str, str] | tuple = ()   # (words, https address)

    def plain(self) -> str:
        """The card as the archive keeps it."""
        parts = ["\n".join(self.head)]
        if self.body:
            parts.append(self.body)
        if self.status:
            parts.append(self.status)
        if self.link:
            parts.append(f"{self.link[0]}: {self.link[1]}")
        return "\n\n".join(parts)


def head(draft_id: str, envelope: Envelope, about: str, flags: list[str], found: list[str]) -> list[str]:
    """The card's header: the channel and the account, the recipient, the subject, what it answers, the flags that
    fired, what was found in the text, and how long the text is. Every value is one cleaned line."""
    e = envelope
    if e.channel == MAIL:
        lines = [f"Черновик {draft_id} · {CHANNELS[MAIL]} · {e.account}",
                 f"Кому: {line(e.name) + ' ' if e.name else ''}<{e.to}>", f"Тема: {line(e.subject, MAX_SUBJECT)}"]
    else:
        lines = [f"Черновик {draft_id} · {CHANNELS[TELEGRAM]} · от твоего имени", f"Кому: {line(e.name) or e.to}"]
    if about:
        lines.append(line(about, 160))
    if flags:
        lines.append("Внимание: " + "; ".join(flags))
    if found:
        lines.append("В тексте найдено: " + "; ".join(found))
    lines.append(f"Текст, {len(e.text)} знаков:")
    return lines


def deep_link(username: str, text: str) -> str:
    """t.me/<username>?text=… — the chat opens with the text in the owner's own field; he sends it himself. Empty
    when there is no username or the link would be too long."""
    if not USERNAME.fullmatch(username or ""):
        return ""
    link = f"https://t.me/{username}?text={quote(text, safe='')}"
    return link if len(link) <= DEEP_LINK_MAX else ""


# --- what the assistant sends ------------------------------------------------------------------------------------

DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["text"],
    "properties": {
        "reply_to": {"type": "string", "maxLength": 300,
                     "description": "id из архива (mail:… или tgb:…) входящего письма или сообщения, на которое "
                                    "отвечаешь. Адрес, тему и тред Роутер возьмёт оттуда"},
        "to": {"type": "string", "maxLength": 254,
               "description": "только для нового письма: адрес, с которым Владелец уже переписывался (есть в архиве)"},
        "subject": {"type": "string", "maxLength": MAX_SUBJECT, "description": "только для нового письма: тема"},
        "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT,
                 "description": "текст целиком, как он уйдёт: обычный текст без разметки, от лица Владельца"},
        "replaces": {"type": "string", "pattern": "^[0-9a-f]{8}$",
                     "description": "номер черновика, который этот заменяет: Владелец попросил поправить"},
    },
}
