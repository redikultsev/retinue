"""The router's side of what leaves the system for a third party (architecture §12, research 60).

The assistant proposes a reply with the bus tool `draft_reply`: the letter or the Telegram message she answers by its
archive id, or — for a new letter — an address the owner has already written to or heard from, and the text. Code
does the rest: the address, the subject and the thread from the archive's metadata, never from her; the flags («новый
получатель», «по просьбе собеседника», Reply-To that is not From, the Telegram window); what is in the text; the
envelope and its digest; the card, built by the courier's code (`courier.head`) and shown with the text verbatim.

The owner's «да» is the «Отправить» button under that very card and nothing else — never the word in the chat. The
button carries only an id; the draft, its digest and its lifetime are in the router's table, and who pressed is
checked by the channel. The neighbours that hold the keys — the mail sender, the Telegram Business gateway — send
exactly the envelope whose digest they are given, each key once.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

import httpx

from . import clock, courier, lifehub
from .archive import CHAT, MAIL
from .courier import MAIL as BY_MAIL, TELEGRAM as BY_TELEGRAM, CardView, Envelope, Refused
from .mail import JOB, Unavailable, safe_label

log = logging.getLogger("retinue.outbox")

TTL_S = 3 * 3600          # a card lives this long: a long meeting or a short flight, not the next day
HOLD_S = 10               # after «Отправить», this long to «Отменить» before anything leaves
STALE = "Устарела: пришло новое сообщение."  # a card whose exchange moved on: it never sends
WINDOW_MARGIN_S = 600     # a Telegram reply is offered only while the 24-hour window has this much left
DAY = 86400
LIVE = ("shown", "hidden", "link")   # a card whose buttons still mean something
HOW = {"link": "Открой чат по ссылке внизу: текст подставится, отправишь сам.",
       "copy": "У собеседника нет username: открой чат с ним и вставь текст — нажми на него, он скопируется.",
       "mailto": "Отправь сам из Gmail: адрес и тема выше, текст копируется нажатием."}


class Neighbour:
    """A process with a key the router has not got: on an internal network it shares with the router alone, every
    call signed with their token. Its refusals come back as answers; only silence is Unavailable."""

    def __init__(self, url: str, token: str, name: str, http: httpx.AsyncClient | None = None) -> None:
        self.url, self.token, self.name = url.rstrip("/"), token, name
        self.http = http or httpx.AsyncClient(trust_env=False)  # a neighbour, never through a proxy

    async def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
        try:
            response = await self.http.request(method, f"{self.url}{path}", json=body, timeout=timeout,
                                               headers={"Authorization": f"Bearer {self.token}"})
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise Unavailable(f"{self.name} недоступен ({type(exc).__name__})") from None
        if response.status_code not in (200, 400) or not isinstance(data, dict):
            raise Unavailable(f"{self.name} ответил HTTP {response.status_code}")
        return data

    async def items(self, limit: int = 50) -> list[dict]:
        data = await self._call("POST", "/items", {"limit": limit})
        return [i for i in data.get("items") or [] if isinstance(i, dict) and isinstance(i.get("seq"), int)]

    async def confirm(self, upto: int) -> None:
        await self._call("POST", "/items/confirm", {"upto": upto})

    async def status(self) -> dict:
        return await self._call("GET", "/status")

    async def send(self, key: str, envelope: dict, digest: str) -> dict:
        """One send. Unavailable here means the request may have reached it: the caller calls that «unknown»."""
        return await self._call("POST", "/send", {"key": key, "envelope": envelope, "digest": digest}, 120)

    async def file(self, file_id: str) -> bytes:
        """A file of the other person's from a chosen chat, by the id its item carried (the gateway downloads it)."""
        data = await self._call("POST", "/file", {"id": file_id}, 300)
        if not isinstance(data.get("data"), str):
            raise Unavailable(str(data.get("error") or "файла нет"))
        return base64.b64decode(data["data"])


class OutboxStore:
    """The drafts, in the router's SQLite (never mounted into an agent): each card, its envelope and digest, how
    long it lives, and what became of it. A state moves only from the states named (`move`): two presses, a press
    and the clock, a restart — exactly one of them wins."""

    COLUMNS = ("id", "created", "state", "channel", "envelope", "digest", "reply_to", "thread", "head", "job",
               "card", "expires", "send_after", "result", "at")

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,                -- 8 hex: the key a sender sends once
                created REAL NOT NULL,
                state TEXT NOT NULL,                -- shown | hidden | link | queued | sending | sent | failed | unknown
                                                    -- | cancelled | dropped | fixing | replaced | expired | stale
                channel TEXT NOT NULL,              -- mail | telegram
                envelope TEXT NOT NULL,             -- courier.Envelope as JSON
                digest TEXT NOT NULL,
                reply_to TEXT NOT NULL DEFAULT '',  -- the archive id answered
                thread TEXT NOT NULL DEFAULT '',    -- where a newer message makes the card stale: mail:<thread>, chat:<id>
                head TEXT NOT NULL,                 -- the card's header lines, JSON
                job INTEGER NOT NULL DEFAULT 0,     -- about the job search: the card waits behind «Показать»
                card TEXT NOT NULL DEFAULT '',      -- the archive id of the card shown
                expires REAL NOT NULL,
                send_after REAL,                    -- queued: the end of the hold
                result TEXT NOT NULL DEFAULT '{}',  -- what the sender answered
                at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS drafts_state ON drafts (state);
            """)
        db.commit()

    def add(self, draft: dict) -> None:
        values = {**draft, "envelope": json.dumps(draft["envelope"], ensure_ascii=False),
                  "head": json.dumps(draft["head"], ensure_ascii=False), "result": "{}"}
        self.db.execute(f"INSERT INTO drafts ({', '.join(self.COLUMNS)}) VALUES ({', '.join('?' * len(self.COLUMNS))})",
                        tuple(values.get(c) for c in self.COLUMNS))
        self.db.commit()

    def get(self, draft_id: str) -> dict | None:
        row = self.db.execute(f"SELECT {', '.join(self.COLUMNS)} FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            return None
        draft = dict(zip(self.COLUMNS, row))
        draft["envelope"], draft["head"], draft["result"] = (json.loads(draft[k]) for k in ("envelope", "head", "result"))
        return draft

    def move(self, draft_id: str, states: tuple[str, ...], to: str, now: float, **fields) -> bool:
        """`to` only from one of `states`; True when this call moved it."""
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
        sets = ", ".join(["state = ?", "at = ?", *(f"{name} = ?" for name in fields)])
        cursor = self.db.execute(f"UPDATE drafts SET {sets} WHERE id = ? AND state IN ({', '.join('?' * len(states))})",
                                 (to, now, *fields.values(), draft_id, *states))
        self.db.commit()
        return cursor.rowcount == 1

    def in_state(self, *states: str) -> list[dict]:
        rows = self.db.execute(f"SELECT id FROM drafts WHERE state IN ({', '.join('?' * len(states))}) ORDER BY created",
                               states).fetchall()
        return [self.get(row[0]) for row in rows]

    def sent(self, since: float, channel: str) -> int:
        """Sent for certain since `since` on this channel: what the status page counts."""
        return self.db.execute("SELECT COUNT(*) FROM drafts WHERE state = 'sent' AND channel = ? AND at >= ?",
                               (channel, since)).fetchone()[0]


@dataclass
class Built:
    """A draft as code made it from the archive: the envelope and what the card says about it."""
    envelope: Envelope
    about: str
    reply_to: str = ""
    thread: str = ""
    known: set[str] = field(default_factory=set)   # the thread's participants: their addresses are not findings
    flags: list[str] = field(default_factory=list)
    new: bool = False
    job: bool = False
    mode: str = "send"            # send | link (t.me deep link) | copy (no link) | mailto (no working sender)
    why: str = ""                 # why not «send»
    window_end: float | None = None


class Outbox:
    def __init__(self, store: OutboxStore, sender: Neighbour | None = None, gateway: Neighbour | None = None,
                 owner_tg: int | None = None) -> None:
        self.store, self.sender, self.gateway, self.owner_tg = store, sender, gateway, owner_tg
        self.core = None
        self.clock = time.time  # the router's clock; a test moves it

    def attach(self, core) -> None:
        self.core = core
        core.actions.update({"courier_show": self._show, "courier_send": self._send, "courier_cancel": self._cancel,
                             "courier_fix": self._fix, "courier_skip": self._skip})
        self.tasks: set[asyncio.Task] = set()

    # --- a draft ------------------------------------------------------------------------------------------------

    async def propose(self, args, foreign: set[str], now: float) -> tuple[bool, str]:
        """Her draft, checked by code, made into an envelope from the archive and shown to the owner as a card. The
        answer tells her what happened — never that anything was sent."""
        try:
            args = lifehub.check(courier.DRAFT_SCHEMA, args)
            if bool(args.get("reply_to")) == bool(args.get("to")):
                raise Refused("нужен ровно один из reply_to (ответ) и to (новое письмо)")
            text = courier.clean(args["text"])
            replaced = self.store.get(args["replaces"]) if args.get("replaces") else None
            if args.get("replaces") and (not replaced or replaced["state"] not in (*LIVE, "fixing")):
                raise Refused(f"черновика {args['replaces']} нет или он уже не действует")
            built = await (self._reply(str(args["reply_to"]), text, now) if args.get("reply_to") else
                           self._letter(str(args["to"]), str(args.get("subject") or ""), text))
        except (Refused, lifehub.Refused) as exc:
            return False, f"Не принято: {exc}"
        flags = list(built.flags) + (["по просьбе собеседника"] if foreign & {"mail", "telegram"} else [])
        draft_id = secrets.token_hex(4)
        while self.store.get(draft_id):
            draft_id = secrets.token_hex(4)
        envelope = built.envelope
        expires = now + TTL_S
        if built.window_end:
            expires = min(expires, built.window_end - WINDOW_MARGIN_S)
        if replaced:
            flags.append(f"вместо черновика {replaced['id']}")
        draft = {"id": draft_id, "created": now, "state": "shown" if built.mode == "send" else "link",
                 "channel": envelope.channel, "envelope": envelope.as_dict(), "digest": courier.digest(envelope),
                 "reply_to": built.reply_to, "thread": built.thread, "job": int(built.job),
                 "head": courier.head(draft_id, envelope, built.about, flags, courier.findings(text, built.known)),
                 "card": "", "expires": expires, "send_after": None, "at": now}
        if built.mode != "send":
            draft["head"].insert(1, f"{built.why} {HOW[built.mode]}")
        self.store.add(draft)
        if replaced and self.store.move(replaced["id"], (*LIVE, "fixing"), "replaced", now):
            await self._retire(replaced, f"Заменён черновиком {draft_id}.")
        await self.show(self.store.get(draft_id), now)
        until = clock.until(expires, now, self.core.tz)
        if built.mode == "send":
            return True, (f"Черновик {draft_id} показан Владельцу карточкой с текстом целиком. Уйдёт, только если он "
                          f"нажмёт «Отправить» (до {until}); слово «да» в чате — не согласие. Не пиши, что отправлено.")
        return True, (f"Черновик {draft_id} показан Владельцу, но отправить его сама система не может: {built.why} "
                      "Владелец отправит сам, если захочет.")

    async def _reply(self, reply_to: str, text: str, now: float) -> Built:
        event = self.core.archive.get(reply_to)
        if not event or event.kind not in (MAIL, CHAT) or event.meta.get("direction") != "in":
            raise Refused("reply_to: такого входящего письма или сообщения в архиве нет — возьми id из поиска или "
                          "из справки")
        meta, said = event.meta, clock.day(event.ts, self.core.tz)
        if event.kind == MAIL:
            sender = str(meta.get("sender") or "").lower()
            address = str(meta.get("reply_to") or sender).lower()
            if not courier.ADDRESS.fullmatch(address):
                raise Refused(f"адрес ответа «{courier.line(address)}» не годится для отправки")
            flags = [] if address == sender else [f"ответ уйдёт на Reply-To {address}, а письмо от {sender}"]
            mid = str(meta.get("message_id") or "")
            refs = [r for r in (meta.get("references") or ([meta["in_reply_to"]] if meta.get("in_reply_to") else []))
                    if courier.MESSAGE_ID.fullmatch(str(r))]
            thread = str(meta.get("thread") or "")
            envelope = Envelope(BY_MAIL, str(meta.get("account") or ""), address, text,
                                subject=courier.reply_subject(str(meta.get("subject") or "")),
                                thread=thread if re.fullmatch(r"[0-9a-f]{1,32}", thread) else "",
                                in_reply_to=mid if courier.MESSAGE_ID.fullmatch(mid) else "",
                                references=tuple((refs + [mid])[-courier.MAX_REFERENCES:])
                                if courier.MESSAGE_ID.fullmatch(mid) else ())
            built = Built(envelope, f"Ответ на письмо от {said}", event.id, f"mail:{thread}",
                          {sender, address, envelope.account, *(meta.get("to") or []), *(meta.get("cc") or [])}, flags,
                          new=not self._wrote_to(address), job=self._job(event.id))
            built.mode, built.why = await self._mailbox(envelope.account)
            courier.check(envelope)
        else:
            if self.owner_tg is None:
                raise Refused("Telegram Владельца не подключён к Роутеру")
            chat = str(meta.get("chat") or "")
            envelope = Envelope(BY_TELEGRAM, str(self.owner_tg), chat, text,
                                name=courier.line(meta.get("name") or meta.get("username") or ""),
                                reply_to_message=int(meta.get("message_id") or 0))
            courier.check(envelope)
            last = self._last_in(chat) or event.ts
            built = Built(envelope, f"Ответ на сообщение от {said}", event.id, f"chat:{chat}", set(),
                          [f"окно Telegram до {clock.until(last + DAY, now, self.core.tz)}"],
                          new=not self._chatted(chat), job=self._job(event.id), window_end=last + DAY)
            if built.window_end - now < WINDOW_MARGIN_S + 60:
                built.mode, built.why = "link", "окно Telegram закрыто: прошло больше суток с её последнего сообщения."
            elif not self.gateway:
                built.mode, built.why = "link", "Business-бот не подключён к Роутеру."
            if built.mode == "link":
                built.window_end = None
                if not courier.deep_link(str(meta.get("username") or ""), text):
                    built.mode = "copy"
        if built.new:
            built.flags.insert(0, "новый получатель")
        return built

    async def _letter(self, to: str, subject: str, text: str) -> Built:
        """A new letter: only to an address the owner has already written to or heard from, from the mailbox that
        talked to it last. A new address is impossible in the first version."""
        address = to.strip().lower()
        if not courier.ADDRESS.fullmatch(address):
            raise Refused("to: один адрес латиницей, без имени")
        rows = self.core.archive.db.execute(
            "SELECT meta FROM events WHERE kind = 'mail' AND (json_extract(meta, '$.sender') = ?1 OR EXISTS (SELECT 1 "
            "FROM json_each(events.meta, '$.to') WHERE value = ?1) OR EXISTS (SELECT 1 FROM json_each(events.meta, "
            "'$.cc') WHERE value = ?1)) ORDER BY ts DESC LIMIT 1", (address,)).fetchall()
        if not rows:
            raise Refused(f"to: с {address} Владелец не переписывался — писать на новые адреса нельзя")
        subject = courier.clean(subject, courier.MAX_SUBJECT, "тема")
        if "\n" in subject:
            raise Refused("тема: одна строка")
        envelope = Envelope(BY_MAIL, str(json.loads(rows[0][0]).get("account") or ""), address, text, subject=subject)
        courier.check(envelope)
        built = Built(envelope, "Новое письмо", "", f"from:{address}", {address, envelope.account},
                      new=not self._wrote_to(address), job=self._job_sender(address))
        built.mode, built.why = await self._mailbox(envelope.account)
        if built.new:
            built.flags.insert(0, "новый получатель")
        return built

    async def _mailbox(self, account: str) -> tuple[str, str]:
        """Whether the sender can send from this mailbox now; if not, the card says why and the owner sends himself."""
        if not self.sender:
            return "mailto", "отправка почты не подключена."
        try:
            status = await self.sender.status()
        except Unavailable:
            return "mailto", "отправка почты не отвечает."
        if account not in (status.get("accounts") or []):
            return "mailto", f"для ящика {account} нет ключа отправки."
        if account in (status.get("dead") or {}):
            return "mailto", f"Google не пускает отправку из {account}: нужен вход заново (--kind send)."
        return "send", ""

    def _wrote_to(self, address: str) -> bool:
        """Has the owner ever written to this address: his letters in the archive, or a letter the courier sent."""
        found = self.core.archive.db.execute(
            "SELECT 1 FROM events WHERE kind = 'mail' AND json_extract(meta, '$.direction') = 'out' AND (EXISTS (SELECT "
            "1 FROM json_each(events.meta, '$.to') WHERE value = ?1) OR EXISTS (SELECT 1 FROM json_each(events.meta, "
            "'$.cc') WHERE value = ?1)) LIMIT 1", (address,)).fetchone()
        return found is not None or self.store.db.execute(
            "SELECT 1 FROM drafts WHERE channel = 'mail' AND state = 'sent' AND json_extract(envelope, '$.to') = ? "
            "LIMIT 1", (address,)).fetchone() is not None

    def _chatted(self, chat: str) -> bool:
        return self.core.archive.db.execute(
            "SELECT 1 FROM events WHERE kind = 'chat' AND conversation_id = ? AND json_extract(meta, '$.direction') = "
            "'out' LIMIT 1", (f"chat:{chat}",)).fetchone() is not None

    def _last_in(self, chat: str) -> float | None:
        return self.core.archive.db.execute(
            "SELECT MAX(ts) FROM events WHERE kind = 'chat' AND conversation_id = ? AND json_extract(meta, "
            "'$.direction') = 'in' AND NOT json_extract(meta, '$.edit')", (f"chat:{chat}",)).fetchone()[0]

    def _job(self, event_id: str) -> bool:
        if not self.core.mail:
            return False
        row = self.core.mail.store.db.execute("SELECT kind, card FROM mail_items WHERE event_id = ?",
                                              (event_id,)).fetchone()
        return bool(row) and (row[0] == JOB or bool(json.loads(row[1] or "{}").get("job")))

    def _job_sender(self, address: str) -> bool:
        if not self.core.mail:
            return False
        return self.core.mail.store.db.execute(
            "SELECT 1 FROM mail_items WHERE sender = ? AND (kind = ? OR json_extract(card, '$.job')) LIMIT 1",
            (address, JOB)).fetchone() is not None

    # --- the card -------------------------------------------------------------------------------------------------

    def view(self, draft: dict, status: str = "") -> CardView:
        envelope = draft["envelope"]
        link = ()
        if draft["channel"] == BY_TELEGRAM and draft["state"] == "link":
            username = self._username(draft)
            if url := courier.deep_link(username, envelope["text"]):
                link = ("Открыть чат с этим текстом", url)
        return CardView(list(draft["head"]), envelope["text"], status, link)

    def _username(self, draft: dict) -> str:
        event = self.core.archive.get(draft["reply_to"]) if draft["reply_to"] else None
        return str(event.meta.get("username") or "") if event else ""

    def buttons(self, draft: dict) -> list:
        from .core import Button

        envelope = draft["envelope"]
        out = []
        if draft["state"] in ("shown", "hidden"):
            flagged = any(line.startswith("Внимание:") for line in draft["head"])
            whom = envelope.get("name") or envelope["to"]
            label = safe_label(f"Отправить: {whom}", 60) if flagged else "Отправить"
            out.append(Button(label, "courier_send", f"{draft['id']}:{draft['digest']}"))
        return out + [Button("Поправить", "courier_fix", draft["id"]), Button("Не отвечать", "courier_skip", draft["id"])]

    async def show(self, draft: dict, now: float, status: str = "") -> None:
        """The card — or, about the job search, a line that says no more than that a draft waits, and the card on
        «Показать»: a notification shows the start of a message on the screen, during a call too."""
        ttl = max(60.0, draft["expires"] - now)
        if draft["job"] and draft["state"] in ("shown", "link"):
            if draft["state"] == "shown":
                self.store.move(draft["id"], ("shown",), "hidden", now)
            await self.core.tell_owner("Черновик на подтверждение: 1.", ttl_s=ttl, meta={"draft": draft["id"]},
                                       buttons=[self._button("Показать", "courier_show", draft["id"])])
            return
        card = await self.core.tell_card(self.view(draft, status), buttons=self.buttons(draft), ttl_s=ttl,
                                         meta={"draft": draft["id"]})
        self.store.db.execute("UPDATE drafts SET card = ? WHERE id = ?", (card, draft["id"]))
        self.store.db.commit()

    @staticmethod
    def _button(label: str, action: str, value: str):
        from .core import Button

        return Button(label, action, value)

    async def _show(self, value: str) -> str:
        """«Показать»: the card itself, once."""
        draft = self.store.get(value)
        now = self.clock()
        if not draft or draft["card"] or draft["state"] not in ("hidden", "link") or draft["expires"] <= now:
            return "Черновик уже не действует."
        if draft["state"] == "hidden":
            self.store.move(draft["id"], ("hidden",), "shown", now)
        draft = {**self.store.get(value), "job": 0}
        asyncio.create_task(self.show(draft, now))
        return "Показываю"

    # --- the owner's presses --------------------------------------------------------------------------------------

    def _pressed(self, ok: bool, toast: str, draft: dict, status: str, keep: list | tuple = ()):
        from .core import Pressed

        return Pressed(ok, toast, view=self.view(self.store.get(draft["id"]), status), keep=list(keep))

    async def _send(self, value: str):
        """«Отправить». The card is spent already (`Core.press`). Checked again here, in code: the draft is the one
        on the card and still alive, its envelope has the digest the button was bound to, nothing newer came in the
        thread since (the router kills such a card when the message comes — `supersede`; this is the second line).
        Then the hold: «Отменить» for HOLD_S seconds, and only then the sender is asked — once, with the draft's
        id as its key."""
        from .core import Pressed

        draft_id, _, digest = value.partition(":")
        draft, now = self.store.get(draft_id), self.clock()
        if not draft or draft["state"] != "shown":
            return Pressed(False, "Черновик уже не действует.")
        if now >= draft["expires"]:
            self.store.move(draft_id, ("shown",), "expired", now)
            return self._pressed(False, "Устарел", draft, "Устарел: не отправлено.")
        if courier.digest(Envelope.of(draft["envelope"])) != digest or digest != draft["digest"]:
            log.error("draft %s: the envelope does not match the card's digest; not sent", draft_id)
            self.store.move(draft_id, ("shown",), "failed", now, result={"error": "конверт не совпал с карточкой"})
            return self._pressed(False, "Не отправлено", draft, "Не отправлено: черновик изменился после показа.")
        if self._newer(draft):
            self.store.move(draft_id, ("shown",), "stale", now)
            return self._pressed(False, "Устарела", draft, STALE)
        if not self.store.move(draft_id, ("shown",), "queued", now, send_after=now + HOLD_S):
            return Pressed(False, "Черновик уже не действует.")
        cancel = self.core.store.add_button(draft["card"], "Отменить", "courier_cancel", draft_id,
                                            time.time() + HOLD_S + 60)
        task = asyncio.create_task(self._release(draft_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self._pressed(True, f"Отправлю через {HOLD_S} с", draft, f"Отправлю через {HOLD_S} с — можно отменить.",
                             [("Отменить", cancel)])

    async def _cancel(self, value: str):
        from .core import Pressed

        draft = self.store.get(value)
        if draft and self.store.move(value, ("queued",), "cancelled", self.clock()):
            return self._pressed(True, "Отменено", draft, "Отменено — не отправлено.")
        return Pressed(False, "Поздно: уже отправляется.")

    async def _fix(self, value: str):
        from .core import Pressed

        draft = self.store.get(value)
        if draft and self.store.move(value, LIVE, "fixing", self.clock()):
            return self._pressed(True, "Скажи словами, что поправить", draft,
                                 "Ждёт правки: скажи ей словами, что поменять — придёт новая карточка.")
        return Pressed(False, "Черновик уже не действует.")

    async def _skip(self, value: str):
        from .core import Pressed

        draft = self.store.get(value)
        if draft and self.store.move(value, LIVE, "dropped", self.clock()):
            return self._pressed(True, "Не отвечаю", draft, "Не отвечаю.")
        return Pressed(False, "Черновик уже не действует.")

    def _newer(self, draft: dict) -> bool:
        """Has the other side written since the draft was made: the card answers what is no longer the last word."""
        kind, _, key = draft["thread"].partition(":")
        query = {"mail": "kind = 'mail' AND json_extract(meta, '$.thread') = ?",
                 "from": "kind = 'mail' AND json_extract(meta, '$.sender') = ?",
                 "chat": "kind = 'chat' AND conversation_id = 'chat:' || ?"}.get(kind)
        if not query or not key:
            return False
        return self.core.archive.db.execute(
            f"SELECT 1 FROM events WHERE {query} AND json_extract(meta, '$.direction') = 'in' AND ts > ? LIMIT 1",
            (key, draft["created"])).fetchone() is not None

    async def supersede(self, keys: list[str], now: float | None = None) -> None:
        """The other side wrote again in an exchange that has an open card: the card no longer answers the last
        word. It dies at once — in the hold too, so nothing leaves — rewritten in place with its buttons gone. The
        new message goes to her judgement as usual, and she drafts afresh if a reply is needed."""
        keys = [key for key in keys if not key.endswith(":")]
        now = self.clock() if now is None else now
        for draft in self.store.in_state(*LIVE, "queued"):
            if draft["thread"] in keys and self.store.move(draft["id"], (*LIVE, "queued"), "stale", now):
                await self._retire(draft, STALE)

    # --- sending --------------------------------------------------------------------------------------------------

    async def _release(self, draft_id: str) -> None:
        """The hold is over: unless «Отменить» won, the neighbour that holds the key is asked once. Whatever it
        answers is the card's last line; a failure or a doubt is also told to the owner as a message."""
        await asyncio.sleep(HOLD_S)
        now = self.clock()
        if not self.store.move(draft_id, ("queued",), "sending", now):
            return  # cancelled
        draft = self.store.get(draft_id)
        self.core.store.spend_card(draft["card"], time.time())  # «Отменить» is gone
        neighbour = self.sender if draft["channel"] == BY_MAIL else self.gateway
        try:
            answer = await neighbour.send(draft_id, draft["envelope"], draft["digest"]) if neighbour else \
                {"status": "failed", "error": "отправитель не подключён"}
        except Unavailable as exc:  # it may have got the request: never asked again
            answer = {"status": "unknown", "error": str(exc)}
        except Exception as exc:
            log.exception("draft %s: the send failed", draft_id)
            answer = {"status": "unknown", "error": type(exc).__name__}
        await self._outcome(draft, answer)

    async def _outcome(self, draft: dict, answer: dict) -> None:
        from .archive import SYSTEM

        core, now, status = self.core, self.clock(), str(answer.get("status") or "unknown")
        whom = draft["envelope"].get("name") or draft["envelope"]["to"]
        result = {k: v for k, v in answer.items() if k != "status"}
        if status == "sent":
            self.store.move(draft["id"], ("sending",), "sent", now, result=result)
            await self._retire(draft, f"Отправлено {clock.until(now, now, core.tz)}.")
            core.archive.append(SYSTEM, f"Отправлено по карточке {draft['id']}: {whom}.", channel="system",
                                ref=draft["card"] or None, meta={"draft": draft["id"], "sent": True},
                                conversation_id=core.store.conversation(core.default_agent))
        elif status == "unknown":
            self.store.move(draft["id"], ("sending",), "unknown", now, result=result)
            await self._retire(draft, "Исход неизвестен: повторно не отправляю. Если письмо или сообщение найдётся "
                                      "в отправленных — скажу.")
            await core.tell_owner(f"Не знаю, ушло ли: черновик {draft['id']} ({whom}) — {result.get('error', '')}. "
                                  "Повторно не отправляю; проверь отправленные сам.", meta={"draft": draft["id"]})
        else:
            self.store.move(draft["id"], ("sending",), "failed", now, result=result)
            why = self._why(draft, str(result.get("error") or status))
            await self._retire(draft, f"Не отправлено: {why}")
            await core.tell_owner(f"Не отправлено: черновик {draft['id']} ({whom}) — {why}", meta={"draft": draft["id"]})
        await core._each(core.channels, "protocol", f"courier: черновик {draft['id']} → {status}")

    def _why(self, draft: dict, error: str) -> str:
        if error == "invalid_grant":
            account = draft["envelope"]["account"]
            return ("Google не пускает отправку (обычно после смены пароля). Войди заново на Mac, в папке retinue:\n"
                    f"`uv run deploy/mail/login.py --account {account} --kind send`")
        if error == "window":
            return "окно Telegram закрылось (больше суток с её сообщения): ответь сам."
        return error + ("" if error.endswith(".") else ".")

    async def settle_mail(self, message_id: str, gmail: str) -> None:
        """His letter came back from Sent: if it is ours (`courier.message_id`), a doubtful send is settled."""
        if found := re.fullmatch(r"<retinue\.([0-9a-f]{8})@[^>]+>", str(message_id or "")):
            await self._settle(found.group(1), {"id": gmail})

    async def settle_chat(self, chat: str, text: str) -> None:
        """A reply this bot sent came back from Telegram: the doubtful one with the same chat and text is settled."""
        for draft in self.store.in_state("sending", "unknown"):
            if draft["channel"] == BY_TELEGRAM and draft["envelope"]["to"] == chat and draft["envelope"]["text"] == text:
                await self._settle(draft["id"], {})
                return

    async def _settle(self, draft_id: str, result: dict) -> None:
        draft = self.store.get(draft_id)
        if draft and self.store.move(draft_id, ("unknown",), "sent", self.clock(), result={**draft["result"], **result,
                                                                                         "settled": True}):
            await self._retire(draft, "Нашлось в отправленных: ушло.")

    # --- the clock and a restart ----------------------------------------------------------------------------------

    async def step(self, now: float) -> None:
        """Cards past their time say so; their buttons are dead already."""
        for draft in self.store.in_state(*LIVE):
            if draft["expires"] <= now and self.store.move(draft["id"], LIVE, "expired", now):
                await self._retire(draft, "Устарел: не отправлено.")

    async def recover(self) -> None:
        """What a restart interrupted. In the hold: not sent — the card comes again, the owner presses again if he
        still wants it. In the call to the sender: unknown, never sent again."""
        now = self.clock()
        for draft in self.store.in_state("queued"):
            if self.store.move(draft["id"], ("queued",), "shown", now):
                await self._retire(draft, "Роутер перезапускался во время отсрочки: не отправлено.")
                if draft["expires"] > now:
                    await self.show(self.store.get(draft["id"]), now,
                                    "Роутер перезапускался во время отсрочки: не отправлено. Нажми ещё раз, если нужно.")
        for draft in self.store.in_state("sending"):
            await self._outcome(draft, {"status": "unknown", "error": "Роутер перезапустился во время отправки"})

    async def _retire(self, draft: dict, status: str) -> None:
        """A card that no longer means anything: its buttons spent, its last line says why."""
        if draft["card"]:
            self.core.store.spend_card(draft["card"], self.clock())
            await self.core.update_card(draft["card"], self.view(self.store.get(draft["id"]) or draft, status))
