"""What waits for the owner in the assistant's world, as the digest the router pushes to the platform for the one queue
«Waiting for you» (`feed` on the platform's side; the bridge pushes it).

Read from the router's own tables, by the rules they already have — nothing new is judged here:

- **letter / chat**: what her judgement said asks him to do something (`needs` reply, decide, pay, attend or act;
  «read» is only to know) in the last MAIL_DAYS — the «now» page's `MailStore.needing`. A chat he wrote in after it
  is seen, unless she asked him to do or decide something, which a reply does not prove done (the morning summary's
  rule, `mailroom.STILL_OPEN`). One row per person; the owner's private matter (`mail.Private`) is one row that
  names nothing but a count, in the sphere his config gives it.
- **draft**: a reply card whose «Send» still means something (`outbox.LIVE`, not expired). Its letter is not
  counted again, nor a letter a draft already answered (queued, sending or sent).
- **deadline**: a `deadline` of any Record, or an `until` of a profile Record, at most SOON_DAYS away (the face's
  «Deadlines» on «Today»), from the heartbeat's last reading of the base; overdue `deadline`s up to PUSH_DAYS. Each
  carries its day (`when`) and the Record's path in the base (`path`), never its text; it waits for the owner
  («Waiting for you») only from PUSH_DAYS before the day — the heartbeat's own push rule (`proactive.Proactive.pushes`) —
  so its `since` may lie ahead, and the platform leaves it out of the queue until then.

The digest is minimal by contract: an id keyed by the router's own salt (it names no one), the kind, a display name
(never an address), a one-line title — the action and at most 80 characters of her words, with addresses, links,
phone numbers and runs of four digits masked (`said`) — a time, a sphere (the base's Space). No text of a letter or a
message. The platform checks it again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from datetime import timedelta

from . import clock
from .courier import line
from .feed import CHIP_CHARS, DIGEST_BYTES, DIGEST_ITEMS, EMAIL, RECORD, SPHERE
from .lifehub import MAIL_DAYS, NEEDS, WAITING_ITEMS, midnight, named, plural
from .mail import PRIVATE, Private
from .mailroom import STILL_OPEN
from .outbox import LIVE
from .proactive import FIELDS, PUSH_DAYS

DAY = 86400
ASKS = ("reply", "decide", "pay", "attend", "act")  # her `needs` that ask him to act; «read» does not
URGENCY = {need: n for n, need in enumerate(ASKS)}
PRIVATE_WHO = "Private"      # the private matter's one row: no name, no sender
BASE_WHO = "The Base"        # a deadline's row: a Record's date, no person
TEXT_CHARS = 120             # a Record's title
SOON_DAYS = 7                # «Deadlines» on the face's «Today»: a Record's date this many days ahead at most
SAID_CHARS = 80              # her words about somebody's message: a hint of what it is, not what it says
SENT = ("queued", "sending", "sent")  # a draft that answered its letter: the letter waits no more
SALT_KEY = "waiting.salt"    # the router's kv: the key the digest's ids are made with; never leaves the router
BYTES_ROOM = 1024            # the digest is cut this far below the platform's limit: the envelope and `bot`
URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?")
PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
DIGITS = re.compile(r"\d{4,}")
LINK_DRAFT = "draft: send it yourself by the link"
DRAFT = "a reply draft waits for «Send»"


def _salt(core) -> bytes:
    """The router's own key for the ids, made once and kept in its kv: an id is stable across restarts, and without
    the key no guess of an address or a path can be checked against it."""
    salt = core.store.get(SALT_KEY)
    if not salt:
        salt = secrets.token_hex(32)
        core.store.set(SALT_KEY, salt)
    return salt.encode()


def _id(core, kind: str, key: str) -> str:
    """A row's id: stable while the row is the same, and naming no one — keyed by the router's own salt."""
    return f"{kind}:{hmac.new(_salt(core), key.encode(), hashlib.sha256).hexdigest()[:16]}"


def said(text: str) -> str:
    """Her words about somebody's message, as the digest may carry them: one short line with every address, link,
    phone number and run of four digits or more masked — a code, an amount, an account never cross to the
    platform."""
    text = unicodedata.normalize("NFKC", line(text, 2000))
    text = EMAIL.sub("‹address›", text)
    text = URL.sub("‹link›", text)
    text = PHONE.sub("‹number›", text)
    return line(DIGITS.sub("••••", text), SAID_CHARS)


def digest(core, now: float) -> list[dict]:
    """The rows, the most pressing first: drafts (they expire within hours), then people by what they ask, then the
    base's dates by day — cut to what the platform takes (DIGEST_ITEMS, DIGEST_BYTES), the least pressing left
    out."""
    drafts = _drafts(core, now)
    answered = {d.pop("_reply") for d in drafts} - {""}
    if core.outbox:  # a letter a draft answered is done, not waiting
        answered |= {reply for reply, state in core.outbox.store.by_reply().items() if state in SENT}
    rows = (drafts + _people(core, now, answered) + _deadlines(core, now))[:DIGEST_ITEMS]
    while rows and len(json.dumps({"items": rows}, ensure_ascii=False).encode()) > DIGEST_BYTES - BYTES_ROOM:
        rows.pop()
    return rows


def _sphere(core) -> str | None:
    """The sphere of the private matter's row: the owner's config (`memory.private.sphere`), none without it."""
    return Private.of(getattr(core, "memory", None)).sphere or None


def _drafts(core, now: float) -> list[dict]:
    if not core.outbox:
        return []
    rows = []
    for draft in core.outbox.store.in_state(*LIVE):
        if draft is None or draft["expires"] <= now:
            continue
        envelope, private = draft["envelope"] or {}, bool(draft["private"])
        mail = draft["channel"] == "mail"
        who = PRIVATE_WHO if private else named(str(envelope.get("name") or ""), str(envelope.get("to") or "")
                                                if mail else "")
        rows.append({"id": f"draft:{draft['id']}", "kind": "draft", "who": who,
                     "title": LINK_DRAFT if draft["state"] == "link" else DRAFT, "since": float(draft["created"]),
                     "sphere": _sphere(core) if private else None, "_reply": str(draft["reply_to"] or "")})
    return rows


def _people(core, now: float, answered: set[str]) -> list[dict]:
    if not core.mail:
        return []
    store, groups = core.mail.store, {}
    for item in store.needing(now - MAIL_DAYS * DAY, WAITING_ITEMS):
        card, data = item["card"] or {}, item["data"] or {}
        need, sender = card.get("needs_now"), str(item["sender"] or "")
        if need not in ASKS or (item["event_id"] or "") in answered:
            continue
        chat = item["source"] == "telegram"
        ts = float(item["ts"] or item["at"] or now)
        if chat and need not in STILL_OPEN and store.answered_after(sender, ts):
            continue
        private = item["kind"] == PRIVATE or bool(card.get(PRIVATE))
        key = PRIVATE if private else f"{item['source']}:{sender or card.get('who') or '?'}"
        letter = private or not chat
        group = groups.setdefault(key, {
            "id": _id(core, "letter" if letter else "chat", key), "kind": "letter" if letter else "chat",
            "who": PRIVATE_WHO if private else named(str(data.get("name") or ""), "") if chat
            else named(str(card.get("who") or ""), sender),
            "since": ts, "sphere": _sphere(core) if private else None, "count": 0, "need": need, "last": ts,
            "text": "", "deadline": "", "private": private})
        group["count"] += 1
        group["since"] = min(group["since"], ts)
        if URGENCY[need] < URGENCY[group["need"]]:
            group["need"] = need
        if ts >= group["last"]:
            group["last"] = ts
            group["text"] = str(card.get("text") or card.get("summary") or "")
        group["deadline"] = group["deadline"] or str(card.get("deadline_now") or "")
    rows = []
    for group in sorted(groups.values(), key=lambda g: (URGENCY[g["need"]], g["since"])):
        count = group["count"]
        if group["private"]:
            title = f"{count} {plural(count, 'private letter waits', 'private letters wait')} for a reply"
        else:
            title = f"{NEEDS[group['need']]}: {said(group['text'])}"
            title += f" · deadline {said(group['deadline'])}" if group["deadline"] else ""
            title += f" (+{count - 1})" if count > 1 else ""
        rows.append({"id": group["id"], "kind": group["kind"], "who": group["who"], "title": title,
                     "since": group["since"], "sphere": group["sphere"]})
    return rows


def _deadlines(core, now: float) -> list[dict]:
    """The heartbeat's last reading of the base (at most an hour old): nothing read yet, nothing said."""
    scanned = getattr(core.proactive, "scanned", None) if core.proactive else None
    if not scanned:
        return []
    today, rows = clock.local(now, core.tz).date(), []
    for item in scanned[2]:
        days = (item.day - today).days
        due = (item.field == "deadline" and -PUSH_DAYS <= days <= SOON_DAYS) or \
            (item.field == "until" and item.nature == "profile" and 0 <= days <= SOON_DAYS)
        if not due:
            continue
        when = ("today" if days == 0 else "tomorrow" if days == 1 else
                f"in {days} {plural(days, 'day', 'days')}" if days > 0 else
                f"overdue by {-days} {plural(-days, 'day', 'days')}")
        sphere = item.path.split("/")[0]
        rows.append({"id": _id(core, "deadline", f"{item.path}:{item.field}:{item.day.isoformat()}"), "kind": "deadline",
                     "who": BASE_WHO, "title": f"{when}: {line(item.title, TEXT_CHARS)} — {FIELDS[item.field]}",
                     "since": midnight(item.day - timedelta(days=PUSH_DAYS), core.tz),
                     "sphere": sphere if SPHERE.fullmatch(sphere) else None, "when": item.day.isoformat(),
                     **({"path": item.path} if RECORD.fullmatch(item.path) else {})})
    return rows


def chips(core, now: float) -> list[dict]:
    """The hub's status chips (`Lifehub.chips`: anything broken, the backup, the limit, the hub's build) for the
    face's «Today», each `{key, text, level}`. The build's age is left out of its words — «hub built» or not —
    so the digest does not change every minute. No hub here: none."""
    if not getattr(core, "lifehub", None):
        return []
    out = []
    for chip in core.lifehub.chips(now):
        if chip.get("key") == "build":
            chip = {**chip, "text": "hub built" if chip.get("level") == "ok" else "hub not built"}
        out.append({"key": str(chip.get("key")), "text": line(str(chip.get("text") or ""), CHIP_CHARS),
                    "level": str(chip.get("level"))})
    return out
