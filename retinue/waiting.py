"""What waits for the owner in the assistant's world, as the digest the router pushes to the platform for the one queue
«Waiting for you» (`feed` on the platform's side; the bridge pushes it).

Read from the router's own tables, by the rules they already have — nothing new is judged here:

- **letter / chat**: what her judgement said asks him to do something (`needs` reply, decide, pay, attend or act;
  «read» is only to know) and is still open — `MailStore.asks`: not marked «Done» or «Not a task» by him, not put off
  till tomorrow, not answered as code sees it, not run out. One row per person and ask: «pay» and «decide» of one
  sender are two rows, so a button closes exactly what the row says. The owner's private matter (`mail.Private`) is
  one row that names nothing but a count, in the sphere his config gives it.
- **draft**: a reply card whose «Send» still means something (`outbox.LIVE`, not expired). Its letter is not
  counted again.
- **deadline**: a `deadline` of any Record, or an `until` of a profile Record, at most SOON_DAYS away (the face's
  «Deadlines» on «Today»), from the heartbeat's last reading of the base; overdue `deadline`s up to PUSH_DAYS. Each
  carries its day (`when`) and the Record's path in the base (`path`), never its text; it waits for the owner
  («Waiting for you») only from PUSH_DAYS before the day — the heartbeat's own push rule (`proactive.Proactive.pushes`) —
  so its `since` may lie ahead, and the platform leaves it out of the queue until then.

The digest is minimal by contract: an id keyed by the router's own salt (it names no one), the kind, a display name
(never an address), a one-line title — the action and at most 80 characters of her words, with addresses, links,
phone numbers and runs of four digits masked (`said`) — a time, a sphere (the base's Space). No text of a letter or a
message. The platform checks it again.

A person's row also carries `t`, the token of its items: an HMAC under the same salt of their numbers, which never
leave the router. The router keeps `t → numbers` in its kv for ROW_S since it last showed the row, so a press in the
app (`press`) marks exactly the items the owner saw, whatever the router or the platform did since; a token it does
not know is refused. Beside the rows go two numbers (`counts`): asks closed today and asks that ran out unmarked in
the week — no list of them.

What a row is about goes only when the owner opens it in the app (`detail`), one row at a time and to that request
alone — the digest itself never widens: her words and a draft's text masked as the title is (`masked`), and where the
thing is, built from the ids the router holds — the Gmail thread in the mailbox it came to, the chat by the person's
@username. Never the letter's text, its verbatim quote or the sender's address; nothing of the private matter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from datetime import timedelta
from urllib.parse import quote

from . import clock
from .courier import INVISIBLE, line
from .feed import CHIP_CHARS, DIGEST_BYTES, DIGEST_ITEMS, EMAIL, PRIVATE_WHO, RECORD, SPHERE
from .pages import NEEDS, midnight, named, plural
from .mail import ASKS, MARKS, PRIVATE, Private, ask_row, later_until
from .outbox import LIVE
from .proactive import FIELDS, PUSH_DAYS

DAY = 86400
URGENCY = {need: n for n, need in enumerate(ASKS)}
BASE_WHO = "The Base"        # a deadline's row: a Record's date, no person
TEXT_CHARS = 120             # a Record's title
SOON_DAYS = 7                # «Deadlines» on the face's «Today»: a Record's date this many days ahead at most
SAID_CHARS = 80              # her words about somebody's message: a hint of what it is, not what it says
SALT_KEY = "waiting.salt"    # the router's kv: the key the digest's ids are made with; never leaves the router
ROWS_KEY = "waiting.rows"    # the router's kv: each row's token and its items' numbers, as JSON
ROW_S = 86400                # a token is known this long after the digest last showed its row…
ROW_SEEN_S = 3600            # …which is written down again at most this often
TOKEN_CHARS = 32
BYTES_ROOM = 1024            # the digest is cut this far below the platform's limit: the envelope and `bot`
URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?")
PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
DIGITS = re.compile(r"\d{4,}")
DETAIL_ASKS = 10             # a row's sheet names its newest items, this many at most
JUDGED_CHARS = 700           # her judgement, whole (`mail.EVENT_SCHEMA` caps it here)
DEADLINE_CHARS = 40
ADDRESS_CHARS = 120
DRAFT_CHARS = 4000           # a draft's text in the sheet
DRAFT_REF = re.compile(r"draft:([0-9a-f]{8})")
THREAD = re.compile(r"[0-9a-f]{10,24}")             # Gmail's threadId
MAILBOX = re.compile(r"[^\s@/?#&]{1,64}@[^\s@/?#&]{1,190}")
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")  # a Telegram @username, as t.me takes it
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
    out. The tokens of the people's rows sent are remembered for their presses."""
    drafts = _drafts(core, now)
    shown = {d.pop("_reply") for d in drafts} - {""}  # a letter whose draft waits is that draft's row
    people = _people(core, now, shown)
    seqs = {row["t"]: row.pop("_seqs") for row in people if "t" in row}
    rows = (drafts + people + _deadlines(core, now))[:DIGEST_ITEMS]
    while rows and len(json.dumps({"items": rows}, ensure_ascii=False).encode()) > DIGEST_BYTES - BYTES_ROOM:
        rows.pop()
    _remember(core, {row["t"]: seqs[row["t"]] for row in rows if "t" in row}, now)
    return rows


def counts(core, now: float) -> dict:
    """The two numbers beside the rows: asks closed since the owner's midnight — by his buttons or by what code saw
    him do — and asks that ran out with no mark in the last 7 days."""
    if not core.mail:
        return {"closed_today": 0, "expired_week": 0}
    today = midnight(clock.local(now, core.tz).date(), core.tz)
    closed, expired = core.mail.store.ask_counts(now, today, now - 7 * DAY, _sent(core))
    return {"closed_today": closed, "expired_week": expired}


def press(core, token: str, action: str, now: float) -> bool:
    """A press on a person's row in the app (the platform's `waiting-press` note): the owner's mark on the items the
    token names, by the router's own record of it. False — nothing applied — when the token is not known: the list
    he pressed in is older than ROW_S, or the token is not one of the router's. A mark that is none of his buttons, or
    no mail here, is a fault (ValueError), not an old list."""
    if action not in MARKS or not core.mail:
        raise ValueError(f"no such press here: {action}")
    seqs = _rows(core, now).get(token)
    if not seqs:
        return False
    core.mail.store.mark(seqs, action, now, later_until(now, core.tz) if action == "later" else None)
    return True


def detail(core, ref: str, token: str, now: float) -> dict | None:
    """What a row is about, for the app's sheet (`feed.Details`): asked for one row when the owner opens it, answered to
    that request and to nothing else. A person's row by its token — those of the items the router showed under it that
    still ask, newest first; a draft by its id. Her judgement and a draft's text with codes, numbers, links and
    addresses masked as in the digest (`masked`), and where the thing is (`open`: the Gmail thread, the Telegram chat by
    its @username) — never the letter's text, its verbatim quote or the sender's address. The private matter's row —
    nameless on the list — opens as its letters one by one (`_private_detail`). None when the token is not known,
    nothing under it asks any more, or the draft no longer waits or is the private matter's."""
    if match := DRAFT_REF.fullmatch(ref):
        return _draft_detail(core, match[1], now)
    seqs = set(_rows(core, now).get(token) or ()) if token and core.mail else set()
    items = [item for item in core.mail.store.asks(now, _sent(core)) if item["seq"] in seqs] if seqs else []
    if not items:
        return None
    items.sort(key=lambda i: -float(i["ts"] or i["at"] or 0))
    if any(ask_row(item) == PRIVATE for item in items):
        return _private_detail(core, items, now)
    first = items[0]
    chat = first["source"] == "telegram"
    card, data = first["card"] or {}, first["data"] or {}
    who = named(str(data.get("name") or ""), "") if chat else named(str(card.get("who") or ""), str(first["sender"] or ""))
    return {"kind": "chat" if chat else "letter", "who": who, "account": "" if chat else line(first["account"], ADDRESS_CHARS),
            "asks": [_ask(item, now) for item in items[:DETAIL_ASKS]], "more": max(0, len(items) - DETAIL_ASKS),
            **({"open": link} if (link := _open(first)) else {})}

def _private_detail(core, items: list[dict], now: float) -> dict:
    """The private matter's letters, newest first: each its sender's name, her ask and words, and its own token — so
    «Done», «Not a task», «Tomorrow» close one letter (`press`), kept as any row's token is."""
    shown = items[:DETAIL_ASKS]
    tokens = {item["seq"]: _token(core, [item["seq"]]) for item in shown}
    _remember(core, {token: [seq] for seq, token in tokens.items()}, now)
    return {"kind": "letter", "who": PRIVATE_WHO, "account": "",
            "asks": [{**_ask(item, now), "who": named(str((item["card"] or {}).get("who") or ""),
                                                      str(item["sender"] or "")), "t": tokens[item["seq"]]}
                     for item in shown], "more": max(0, len(items) - DETAIL_ASKS)}

def _ask(item: dict, now: float, link: bool = True) -> dict:
    card = item["card"] or {}
    return {"need": str(card.get("needs_now") or "reply"), "text": masked(_judged(item), JUDGED_CHARS),
            "deadline": said(str(card.get("deadline_now") or ""))[:DEADLINE_CHARS], "at": float(item["ts"] or item["at"] or now),
            **({"open": found} if link and (found := _open(item)) else {})}

def _draft_detail(core, draft_id: str, now: float) -> dict | None:
    """A draft waiting for «Send»: to whom, its text, until when it waits, and the letter or chat it answers."""
    found = core.outbox.store.get(draft_id) if core.outbox else None
    if not found or found["state"] not in LIVE or found["expires"] <= now or found["private"]:
        return None
    envelope, mail = found["envelope"] or {}, found["channel"] == "mail"
    answered = core.mail.store.by_event(str(found["reply_to"] or "")) if core.mail else None
    if answered and ask_row(answered) == PRIVATE:
        return None
    link = _open(answered) if answered else \
        _mail_link(str(envelope.get("account") or ""), str(envelope.get("thread") or "")) if mail else None
    return {"kind": "draft", "who": named(str(envelope.get("name") or ""), str(envelope.get("to") or "") if mail else ""),
            "account": line(str(envelope.get("account") or ""), ADDRESS_CHARS) if mail else "",
            "asks": [_ask(answered, now, link=False)] if answered and (answered["card"] or {}).get("needs_now") in ASKS
            else [], "more": 0,
            "draft": {"text": masked(str(envelope.get("text") or ""), DRAFT_CHARS), "channel": "mail" if mail else "telegram",
                      "subject": said(str(envelope.get("subject") or "")), "expires": float(found["expires"]),
                      "state": found["state"]}, **({"open": link} if link else {})}

def _judged(item: dict) -> str:
    """Her words for the owner about an item: her judgement, else triage's summary."""
    card = item["card"] or {}
    return str(card.get("text") or card.get("summary") or "")

def masked(text: str, cap: int) -> str:
    """Longer words for the sheet, masked as `said` masks the digest's — every address, link, phone number and run of
    four digits — with their lines kept and invisible characters dropped."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if unicodedata.category(c) not in INVISIBLE or c == "\n")
    lines = [DIGITS.sub("••••", PHONE.sub("‹number›", URL.sub("‹link›", EMAIL.sub("‹address›", " ".join(part.split())))))
             for part in text.strip().splitlines()]  # line by line: numbers on two lines are two numbers
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= cap else text[:cap - 1] + "…"

def _open(item: dict) -> dict | None:
    """Where an item is: its Gmail thread in the mailbox it came to, or the Telegram chat by the person's @username —
    built from the ids the router holds, never from anything in her words. A chat with no @username has no link that
    opens it (Telegram's `tg://user?id=` is ignored by its apps): none."""
    data = item["data"] or {}
    if item["source"] == "telegram":
        username = str(data.get("username") or "")
        return {"url": f"https://t.me/{username}", "label": "Open the chat"} if USERNAME.fullmatch(username) else None
    return _mail_link(str(item["account"] or ""), str(data.get("thread") or "")) if item["source"] == "mail" else None

def _mail_link(account: str, thread: str) -> dict | None:
    if not THREAD.fullmatch(thread) or not MAILBOX.fullmatch(account):
        return None
    return {"url": f"https://mail.google.com/mail/?authuser={quote(account, safe='@')}#all/{thread}",
            "label": "Open in Gmail"}

def _token(core, seqs: list[int]) -> str:
    """A row's token: its items' numbers under the router's salt — the same items, the same token, after a restart
    too; other items, another."""
    key = "row:" + ",".join(str(seq) for seq in sorted(seqs))
    return hmac.new(_salt(core), key.encode(), hashlib.sha256).hexdigest()[:TOKEN_CHARS]


def _rows(core, now: float) -> dict[str, list[int]]:
    """The tokens the router showed in the last ROW_S, and their items."""
    try:
        known = json.loads(core.store.get(ROWS_KEY) or "{}")
    except ValueError:
        return {}
    return {token: [int(seq) for seq in row["seqs"]] for token, row in known.items() if isinstance(row, dict)
            and isinstance(row.get("seqs"), list) and now - float(row.get("at") or 0) < ROW_S} \
        if isinstance(known, dict) else {}


def _remember(core, shown: dict[str, list[int]], now: float) -> None:
    """The tokens just sent, kept with their items; written only when a token is new, is due to be seen again or has
    run out — not on every look."""
    try:
        known = json.loads(core.store.get(ROWS_KEY) or "{}")
    except ValueError:
        known = {}
    known = known if isinstance(known, dict) else {}
    kept = {token: row for token, row in known.items()
            if isinstance(row, dict) and now - float(row.get("at") or 0) < ROW_S}
    for token, seqs in shown.items():
        if token not in kept or now - float(kept[token].get("at") or 0) >= ROW_SEEN_S:
            kept[token] = {"seqs": seqs, "at": now}
    if kept != known:
        core.store.set(ROWS_KEY, json.dumps(kept))


def _sent(core) -> dict[str, float]:
    return core.outbox.store.answered() if core.outbox else {}


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


def _people(core, now: float, shown: set[str]) -> list[dict]:
    if not core.mail:
        return []
    groups = {}
    for item in core.mail.store.asks(now, _sent(core)):
        card, data = item["card"] or {}, item["data"] or {}
        need, sender = card.get("needs_now"), str(item["sender"] or "")
        if (item["event_id"] or "") in shown:
            continue
        chat = item["source"] == "telegram"
        ts = float(item["ts"] or item["at"] or now)
        key = ask_row(item)
        private = key == PRIVATE
        letter = private or not chat
        group = groups.setdefault(key, {
            "id": _id(core, "letter" if letter else "chat", key), "kind": "letter" if letter else "chat",
            "who": PRIVATE_WHO if private else named(str(data.get("name") or ""), "") if chat
            else named(str(card.get("who") or ""), sender),
            "since": ts, "sphere": _sphere(core) if private else None, "count": 0, "need": need, "last": ts,
            "text": "", "deadline": "", "private": private, "seqs": []})
        group["count"] += 1
        group["seqs"].append(item["seq"])
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
        row = {"id": group["id"], "kind": group["kind"], "who": group["who"], "since": group["since"],
               "sphere": group["sphere"]}
        if group["private"]:  # names nothing on the list: who and what only in its sheet, a letter at a time
            row.update(title=f"{count} {plural(count, 'private letter waits', 'private letters wait')} for a reply",
                       t=_token(core, group["seqs"]), _seqs=group["seqs"], private=True)
        else:
            title = f"{NEEDS[group['need']]}: {said(group['text'])}"
            title += f" · deadline {said(group['deadline'])}" if group["deadline"] else ""
            title += f" (+{count - 1})" if count > 1 else ""
            row.update(title=title, t=_token(core, group["seqs"]), _seqs=group["seqs"])
        rows.append(row)
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
    """The status chips (`Pages.chips`: anything broken, the backup, the limit, the pages' build) for the face's
    «Today», each `{key, text, level}`. The build's age is left out of its words — «pages built» or not — so the
    digest does not change every minute. No pages here: none."""
    if not getattr(core, "pages", None):
        return []
    out = []
    for chip in core.pages.chips(now):
        if chip.get("key") == "build":
            chip = {**chip, "text": "pages built" if chip.get("level") == "ok" else "pages not built"}
        out.append({"key": str(chip.get("key")), "text": line(str(chip.get("text") or ""), CHIP_CHARS),
                    "level": str(chip.get("level"))})
    return out
