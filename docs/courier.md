# Replies to people: Telegram Business and the mail sender

The assistant may propose a reply to a person — a letter in a thread, a new letter to someone you have already
written to or heard from, a message in one of the Telegram chats you chose. She never sends it. Code builds the
envelope from the archive (the address, the subject and the thread come from the letter or the message she
answers, never from her), and you get a **card**: the channel and the account, to whom, the subject, what it
answers, flags, what code found in the text, and the text itself, verbatim, in one block. It leaves only when you
press **«Отправить»** under that very card; then you have 10 seconds for **«Отменить»**. The word «да» in the chat
is never a confirmation.

```
assistant ── draft_reply (bus) ──▶ router: envelope from the archive, digest, the card ──▶ you
                                     │  «Отправить» (from your user id, under the card, while it lives)
                                     ▼  10 s hold · «Отменить»
             mail-send (gmail.send, uid 10004) ◀── send-router        tg-router ──▶ tg-business (uid 10003)
                 └── mail-egress (Google's hosts)                                     └── api.telegram.org
```

Each sender checks again that the envelope has the digest your button was bound to, and sends each key once: a
double press, a restart, a second look never send twice. A result it cannot be sure of (a timeout after the request
left, a 5xx) is never retried; the card says «исход неизвестен», and the router settles it when the letter comes
back from Sent or the reply comes back from Telegram. There is no daily ceiling: every send is your own press.

## The card

- **Flags**, only when they fire: «новый получатель» (you have never written to this address or chat), «по просьбе
  собеседника» (the draft came from a run that someone else's message started, not from your words), «ответ уйдёт
  на Reply-To …» (Reply-To is not From), «окно Telegram до …», «вместо черновика …». With a flag, the button names
  the recipient: «Отправить: Анна».
- **«В тексте найдено»**: every link whole (and whether it carries parameters or a domain not in Latin letters),
  an address not in the thread, a phone, a number of four digits or more next to «код», «пароль», «паспорт»,
  «карта», an IBAN, a card number. What the text says is what you approve; this line is what you might not notice.
- The text is refused, never repaired, if it has a control, bidi or zero-width character; at most 2,000 characters.
- **«Поправить»** kills the card; tell her in words what to change, a new card comes. «Поправь: …» in words does the
  same: her new draft replaces the old card at once. **«Не отвечать»** — nothing is sent.
- A card lives 3 hours (in Telegram, never past the 24-hour window). Then it says «устарел».
- A new message from the other person in the same chat or thread kills the open card at once — rewritten in place
  as «Устарела: пришло новое сообщение», buttons gone, even during the 10 s hold. She reads the new message with the
  exchange above it and drafts afresh if a reply is needed.
- About the job search the card waits behind «Показать», like the letter it answers.
- Outside Telegram's window, or with no gateway, the card gives a `t.me/<username>?text=…` link: the chat opens
  with the text in your field and you send it yourself. With no working mail sender the card says so, and you send
  from Gmail yourself.

## Turn on Telegram Business

1. **@BotFather → `/newbot`** — a new bot of its own, not the router's. Then the bot's settings → **Secretary Mode**
   (Business Mode) on.
2. On the server: `sudo TELEGRAM_OWNER_ID=<your id> TGBUSINESS=1 bash deploy/setup.sh`, put the new bot's token in
   `TELEGRAM_BUSINESS_TOKEN` in `/srv/retinue/stack.env`, then `docker compose … up -d`. The gateway runs before you
   connect the bot: Telegram keeps updates for 24 hours only.
3. In Telegram on your phone: **Settings → Chat Automation** (older clients: Telegram Business → Chatbots) → the
   bot's username. **Chats: only the people you choose. Rights: only «reply to messages» — nothing else.** The router
   tells you if the bot has more rights than that.
4. Only your own account's connection counts; groups, secret chats and history before the connection never reach
   a business bot.
5. Photos, documents, voice notes, audio and video the other person sends are downloaded by the gateway (up to
   20 MB, the Bot API's limit — a larger file is named, not read) and read by the router like your own files:
   pictures for her eyes, documents as text, voice and video sound through speech to text. They are kept with the
   message, marked as someone else's. Your own files in those chats are named, not downloaded.

Emergency: turn the bot off in Chat Automation **and** issue a new token in @BotFather (`/token`). A new token alone
does not disconnect the bot.

## Turn on mail sending

1. Google Cloud console → your project → **Data access**: add `…/auth/gmail.send` (Sensitive); `openid` and
   `…/auth/userinfo.email` are basic.
2. On the server: `sudo TELEGRAM_OWNER_ID=<your id> SEND=1 bash deploy/setup.sh` (needs `MAIL=1`).
3. On your Mac, once per mailbox: `uv run deploy/mail/login.py --account you@gmail.com --kind send`. `gmail.send`
   cannot read the profile, so the account is checked by the e-mail Google puts in the ID token. The token goes to
   `/srv/retinue/send/keys` (uid 10004), apart from the collector's, and never into the backup.
4. A password change kills this token too: the failed send says so, with the command to log in again.

## What it never does

Write first to a new address or a new Telegram chat; send an attachment or HTML; read your mailbox with the send
token; edit or delete a sent message; send anything without your press under the exact card.
