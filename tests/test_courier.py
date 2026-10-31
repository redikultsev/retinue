"""The courier's code: the text it refuses, the envelope the owner's button is bound to, the letter it builds, what it
names on the card. No network: what leaves is checked here before anyone sends it."""

import dataclasses
import email
import email.policy

import pytest

from retinue import courier
from retinue.courier import Envelope, Refused

LETTER = Envelope(channel="mail", account="owner@example.org", to="anna@acme.example", text="Да, четверг подходит.",
                  name="Анна из Acme", subject="Re: Встреча", thread="18c2f0a1b2c3d4e5",
                  in_reply_to="<m1@acme.example>", references=("<m0@acme.example>", "<m1@acme.example>"))
CHAT = Envelope(channel="telegram", account="42", to="777", text="Буду в 7.", name="Иван", reply_to_message=15)


def refused(call, *args):
    with pytest.raises(Refused) as caught:
        call(*args)
    return str(caught.value)


def test_a_text_with_an_invisible_character_is_refused_never_cleaned():
    """What the owner sees is what is hashed and sent: an override, a zero-width space, a tab is a refusal that names
    the character and its place — stripping it would send what he did not see."""
    assert courier.clean("  Привет,\r\nдо встречи.  ") == "Привет,\nдо встречи."
    assert courier.clean("é") == "é", "NFC: one way of writing each letter"
    assert refused(courier.clean, "оплати ‮текст") == "text: an invisible or control character U+202E at position 8 — remove it"
    assert "U+200B" in refused(courier.clean, "код​1234") and "U+0009" in refused(courier.clean, "a\tb")
    assert "U+2028" in refused(courier.clean, "a b") and "U+E000" in refused(courier.clean, "")
    assert refused(courier.clean, " \n ") == "text: empty"
    assert refused(courier.clean, "я" * 2001) == "text: 2001 characters, at most 2000 allowed"
    assert courier.line("Анна‮ \n из​ Acme") == "Анна из Acme", "somebody's name on a card: one clean line"


def test_the_digest_binds_the_whole_envelope_and_a_sender_checks_its_shape():
    first = courier.digest(LETTER)
    assert first == courier.digest(Envelope.of(LETTER.as_dict())) and len(first) == 64
    for name, value in (("to", "evil@acme.example"), ("account", "work@example.org"), ("text", "Да, пятница."),
                        ("subject", "Re: Счёт"), ("thread", "1"), ("in_reply_to", "<m9@x>"), ("name", "Аня")):
        assert courier.digest(dataclasses.replace(LETTER, **{name: value})) != first, name
    assert refused(Envelope.of, {**LETTER.as_dict(), "cc": "x@y.org"}) == "envelope: wrong fields"
    assert refused(Envelope.of, {**LETTER.as_dict(), "reply_to_message": "1"}) == "envelope: wrong fields"
    for bad in ("anna@acme.example, evil@x.org", "ANNA@acme.example", "anna@acme.example\r\nBcc: x@y.org", "anna@пример.рф"):
        assert refused(courier.check, dataclasses.replace(LETTER, to=bad)).startswith("address"), bad
    assert refused(courier.check, dataclasses.replace(LETTER, subject="Re: x\r\nBcc: e@x.org")).startswith("subject")
    assert refused(courier.check, dataclasses.replace(LETTER, references=("not-an-id",))).startswith("thread")
    assert refused(courier.check, dataclasses.replace(LETTER, text="Да ‮")).startswith("text")
    assert courier.check(CHAT) is CHAT
    assert refused(courier.check, dataclasses.replace(CHAT, subject="x")) == "a Telegram message has no subject or thread"
    assert refused(courier.check, dataclasses.replace(CHAT, channel="sms")) == "channel: 'sms'"


def test_a_letter_has_one_recipient_plain_text_and_the_threads_headers():
    raw = courier.mail_bytes(LETTER, "ab12cd34")
    back = email.message_from_bytes(raw, policy=email.policy.default)
    assert back["From"] == "owner@example.org" and back["To"] == "anna@acme.example", "bare addresses, no names"
    assert back["Subject"] == "Re: Встреча" and back["Message-ID"] == "<retinue.ab12cd34@example.org>"
    assert back["In-Reply-To"] == "<m1@acme.example>" and back["References"] == "<m0@acme.example> <m1@acme.example>"
    assert back.get_content_type() == "text/plain" and back.get_content().strip() == "Да, четверг подходит."
    assert b"\r\n" in raw and back["Cc"] is None and back["Bcc"] is None
    new = dataclasses.replace(LETTER, subject="Вопрос", thread="", in_reply_to="", references=())
    assert email.message_from_bytes(courier.mail_bytes(new, "k"), policy=email.policy.default)["In-Reply-To"] is None
    assert refused(courier.mail_bytes, CHAT, "k").startswith("not a letter")
    assert courier.reply_subject("Встреча") == "Re: Встреча" and courier.reply_subject("RE: Встреча") == "RE: Встреча"


def test_what_the_owner_might_not_notice_is_named_on_the_card():
    text = ("Ссылка: https://acme.example/page?ref=anna и www.пример.рф/x. Пишите anna@acme.example или evil@other.example, "
            "тел. +44 20 7946 0000. Код из СМС: 482913. Паспорт 0000 000000. Карта 4111 1111 1111 1111, "
            "IBAN GB82WEST12345698765432. Встреча в 2026 году.")
    found = courier.findings(text, {"anna@acme.example"})
    assert found[:3] == ["address evil@other.example", "link https://acme.example/page?ref=anna — with parameters",
                         "link www.пример.рф/x (domain not in Latin letters: www.xn--e1afmkfd.xn--p1ai)"]
    assert "phone +44 20 7946 0000" in found and "number 482913 next to «код»" in found
    assert "looks like a card number: 4111 1111 1111 1111" in found and "looks like an IBAN: GB82WEST12345698765432" in found
    assert not any("2026" in f for f in found), "a year is not a secret"
    assert courier.findings("Буду в 7, спасибо!") == []


def test_a_number_next_to_a_secret_word_is_named_in_either_language():
    """The text is anybody's: «code», «password», «card» are words to look near in English as in Russian."""
    found = courier.findings("Your code is 482913. Card 5500 0000 0000 0005 expires. Password 98761234.")
    assert "number 482913 next to «code»" in found and "number 98761234 next to «password»" in found
    assert courier.findings("Пароль: 77421093")[0] == "number 77421093 next to «парол»"
    assert courier.findings("See you in 2026, room 4412.") == [], "a number with no such word nearby is no finding"


def test_the_end_of_a_path_is_not_a_link_but_every_bare_name_still_is():
    assert courier.findings("Записала в /work/x/hello.md и deploy/setup.sh, C:\\x\\main.py") == []
    assert courier.findings("hello.md pay-mup.rs mup.gov.rs x.attacker.sh evil.sh/x example.com") == [
        "link hello.md", "link pay-mup.rs", "link mup.gov.rs", "link x.attacker.sh", "link evil.sh/x",
        "link example.com"], ".md, .rs, .sh are country domains: a name alone is never let through"


def test_the_card_header_and_the_deep_link_are_built_by_code():
    lines = courier.head("ab12cd34", LETTER, "Reply to the letter of 10.10 14:02", ["new recipient"],
                         ["link https://x.example"], "mail 1")
    assert lines == ["Draft ab12cd34 · Gmail · owner@example.org", "To: Анна из Acme <anna@acme.example>",
                     "Subject: Re: Встреча", "Reply to the letter of 10.10 14:02", "Warning: new recipient",
                     "Read while drafting: mail 1", "Found in the text: link https://x.example", "Text, 21 characters:"]
    assert courier.head("ab12cd34", CHAT, "", [], [])[:2] == ["Draft ab12cd34 · Telegram · in your name", "To: Иван"]
    view = courier.CardView(["Draft x"], "Привет *всем*", "Expired.", ("open the chat", "https://t.me/ivan"))
    assert view.plain() == "Draft x\n\nПривет *всем*\n\nExpired.\n\nopen the chat: https://t.me/ivan"
    assert courier.deep_link("ivan_p", "Буду в 7 & ок?") == "https://t.me/ivan_p?text=%D0%91%D1%83%D0%B4%D1%83%20%D0%B2%207%20%26%20%D0%BE%D0%BA%3F"
    assert courier.deep_link("", "x") == "" and courier.deep_link("a/../b", "x") == ""
    assert courier.deep_link("ivan_p", "я" * 400) == "", "too long for a link: the text is copied from the card"
