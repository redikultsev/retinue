"""What waits for the owner in the assistant's world, as the digest the router pushes to the platform for the one queue
«Ждут тебя» (`feed` on the platform's side; the bridge pushes it).

Read from the router's own tables, by the rules they already have — nothing new is judged here:

- **letter / chat**: what her judgement said asks him to do something (`needs` reply, decide, pay, attend or act;
  «read» is only to know) and is still open — `MailStore.asks`: not marked «Сделано» or «Не дело» by him, not put off
  till tomorrow, not answered as code sees it, not run out. One row per person and ask: «оплатить» and «решить» of one
  sender are two rows, so a button closes exactly what the row says. The owner's private matter (`mail.Private`) is
  one row that names nothing but a count, in the sphere his config gives it.
- **draft**: a reply card whose «Отправить» still means something (`outbox.LIVE`, not expired). Its letter is not
  counted again.
- **deadline**: a `deadline` of any Record, or an `until` of a profile Record, at most SOON_DAYS away (the face's
  «Сроки» on «Сегодня»), from the heartbeat's last reading of the base; overdue `deadline`s up to PUSH_DAYS. Each
  carries its day (`when`) and the Record's path in the base (`path`), never its text; it waits for the owner
  («Ждут тебя») only from PUSH_DAYS before the day — the heartbeat's own push rule (`proactive.Proactive.pushes`) —
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
from .lifehub import NEEDS, midnight, named, plural
from .mail import ASKS, MARKS, PRIVATE, Private, ask_row, later_until
from .outbox import LIVE
from .proactive import FIELDS, PUSH_DAYS

DAY = 86400
URGENCY = {need: n for n, need in enumerate(ASKS)}
PRIVATE_WHO = "Личное"       # the private matter's one row: no name, no sender
TEXT_CHARS = 120             # a Record's title
SOON_DAYS = 7                # «Сроки» on the face's «Сегодня»: a Record's date this many days ahead at most
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
LINK_DRAFT = "черновик: отправить самому по ссылке"
DRAFT = "черновик ответа ждёт «Отправить»"


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
    text = EMAIL.sub("‹адрес›", text)
    text = URL.sub("‹ссылка›", text)
    text = PHONE.sub("‹номер›", text)
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
        if group["private"]:  # no buttons in the app: its words and marks are behind «Показать» in Telegram
            row["title"] = f"{count} {plural(count, 'письмо', 'письма', 'писем')} о личном ждут ответа"
        else:
            title = f"{NEEDS[group['need']]}: {said(group['text'])}"
            title += f" · срок {said(group['deadline'])}" if group["deadline"] else ""
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
        when = ("сегодня" if days == 0 else "завтра" if days == 1 else
                f"через {days} {plural(days, 'день', 'дня', 'дней')}" if days > 0 else
                f"просрочено {-days} {plural(-days, 'день', 'дня', 'дней')}")
        sphere = item.path.split("/")[0]
        rows.append({"id": _id(core, "deadline", f"{item.path}:{item.field}:{item.day.isoformat()}"), "kind": "deadline",
                     "who": "База", "title": f"{when}: {line(item.title, TEXT_CHARS)} — {FIELDS[item.field]}",
                     "since": midnight(item.day - timedelta(days=PUSH_DAYS), core.tz),
                     "sphere": sphere if SPHERE.fullmatch(sphere) else None, "when": item.day.isoformat(),
                     **({"path": item.path} if RECORD.fullmatch(item.path) else {})})
    return rows


def chips(core, now: float) -> list[dict]:
    """The hub's status chips (`Lifehub.chips`: anything broken, the backup, the limit, the hub's build) for the
    face's «Сегодня», each `{key, text, level}`. The build's age is left out of its words — «хаб собран» or not —
    so the digest does not change every minute. No hub here: none."""
    if not getattr(core, "lifehub", None):
        return []
    out = []
    for chip in core.lifehub.chips(now):
        if chip.get("key") == "build":
            chip = {**chip, "text": "хаб собран" if chip.get("level") == "ok" else "хаб не собран"}
        out.append({"key": str(chip.get("key")), "text": line(str(chip.get("text") or ""), CHIP_CHARS),
                    "level": str(chip.get("level"))})
    return out
