"""The courier's code: the text it refuses, the envelope the owner's button is bound to, the letter it builds, what it
names on the card. No network: what leaves is checked here before anyone sends it."""

import dataclasses
import email
import email.policy

import pytest

from retinue import courier
from retinue.courier import Envelope, Refused

LETTER = Envelope(channel="mail", account="owner@example.org", to="hr@acme.example", text="Да, четверг подходит.",
                  name="Анна из Acme", subject="Re: Интервью", thread="18c2f0a1b2c3d4e5",
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
    assert refused(courier.clean, "оплати ‮текст") == "текст: невидимый или управляющий символ U+202E на 8-м месте — убери его"
    assert "U+200B" in refused(courier.clean, "код​1234") and "U+0009" in refused(courier.clean, "a\tb")
    assert "U+2028" in refused(courier.clean, "a b") and "U+E000" in refused(courier.clean, "")
    assert refused(courier.clean, " \n ") == "текст: пусто"
    assert refused(courier.clean, "я" * 2001) == "текст: 2001 знаков, можно не больше 2000"
    assert courier.line("Анна‮ \n из​ Acme") == "Анна из Acme", "somebody's name on a card: one clean line"


def test_the_digest_binds_the_whole_envelope_and_a_sender_checks_its_shape():
    first = courier.digest(LETTER)
    assert first == courier.digest(Envelope.of(LETTER.as_dict())) and len(first) == 64
    for name, value in (("to", "evil@acme.example"), ("account", "work@example.org"), ("text", "Да, пятница."),
                        ("subject", "Re: Оффер"), ("thread", "1"), ("in_reply_to", "<m9@x>"), ("name", "Аня")):
        assert courier.digest(dataclasses.replace(LETTER, **{name: value})) != first, name
    assert refused(Envelope.of, {**LETTER.as_dict(), "cc": "x@y.org"}) == "конверт: не те поля"
    assert refused(Envelope.of, {**LETTER.as_dict(), "reply_to_message": "1"}) == "конверт: не те поля"
    for bad in ("hr@acme.example, evil@x.org", "HR@acme.example", "hr@acme.example\r\nBcc: x@y.org", "hr@пример.рф"):
        assert refused(courier.check, dataclasses.replace(LETTER, to=bad)).startswith("адрес"), bad
    assert refused(courier.check, dataclasses.replace(LETTER, subject="Re: x\r\nBcc: e@x.org")).startswith("тема")
    assert refused(courier.check, dataclasses.replace(LETTER, references=("not-an-id",))).startswith("тред")
    assert refused(courier.check, dataclasses.replace(LETTER, text="Да ‮")).startswith("текст")
    assert courier.check(CHAT) is CHAT
    assert refused(courier.check, dataclasses.replace(CHAT, subject="x")) == "у сообщения Telegram нет темы и треда"
    assert refused(courier.check, dataclasses.replace(CHAT, channel="sms")) == "канал: 'sms'"


def test_a_letter_has_one_recipient_plain_text_and_the_threads_headers():
    raw = courier.mail_bytes(LETTER, "ab12cd34")
    back = email.message_from_bytes(raw, policy=email.policy.default)
    assert back["From"] == "owner@example.org" and back["To"] == "hr@acme.example", "bare addresses, no names"
    assert back["Subject"] == "Re: Интервью" and back["Message-ID"] == "<retinue.ab12cd34@example.org>"
    assert back["In-Reply-To"] == "<m1@acme.example>" and back["References"] == "<m0@acme.example> <m1@acme.example>"
    assert back.get_content_type() == "text/plain" and back.get_content().strip() == "Да, четверг подходит."
    assert b"\r\n" in raw and back["Cc"] is None and back["Bcc"] is None
    new = dataclasses.replace(LETTER, subject="Вопрос", thread="", in_reply_to="", references=())
    assert email.message_from_bytes(courier.mail_bytes(new, "k"), policy=email.policy.default)["In-Reply-To"] is None
    assert refused(courier.mail_bytes, CHAT, "k").startswith("не письмо")
    assert courier.reply_subject("Интервью") == "Re: Интервью" and courier.reply_subject("RE: Интервью") == "RE: Интервью"


def test_what_the_owner_might_not_notice_is_named_on_the_card():
    text = ("Ссылка: https://acme.example/cv?ref=anna и www.пример.рф/x. Пишите hr@acme.example или evil@other.example, "
            "тел. +381 60 123 4567. Код из СМС: 482913. Паспорт 4510 123456. Карта 4111 1111 1111 1111, "
            "IBAN RS35260005601001611379. Встреча в 2026 году.")
    found = courier.findings(text, {"hr@acme.example"})
    assert found[:3] == ["адрес evil@other.example", "ссылка https://acme.example/cv?ref=anna — с параметрами",
                         "ссылка www.пример.рф/x (домен не латиницей: www.xn--e1afmkfd.xn--p1ai)"]
    assert "телефон +381 60 123 4567" in found and "число 482913 рядом с «код»" in found
    assert "похоже на номер карты: 4111 1111 1111 1111" in found and "похоже на IBAN: RS35260005601001611379" in found
    assert not any("2026" in f for f in found), "a year is not a secret"
    assert courier.findings("Буду в 7, спасибо!") == []


def test_the_card_header_and_the_deep_link_are_built_by_code():
    lines = courier.head("ab12cd34", LETTER, "Ответ на письмо от 10.10 14:02", ["новый получатель"],
                         ["ссылка https://x.example"])
    assert lines == ["Черновик ab12cd34 · Gmail · owner@example.org", "Кому: Анна из Acme <hr@acme.example>",
                     "Тема: Re: Интервью", "Ответ на письмо от 10.10 14:02", "Внимание: новый получатель",
                     "В тексте найдено: ссылка https://x.example", "Текст, 21 знаков:"]
    assert courier.head("ab12cd34", CHAT, "", [], [])[:2] == ["Черновик ab12cd34 · Telegram · от твоего имени",
                                                              "Кому: Иван"]
    view = courier.CardView(["Черновик x"], "Привет *всем*", "Устарел.", ("открыть чат", "https://t.me/ivan"))
    assert view.plain() == "Черновик x\n\nПривет *всем*\n\nУстарел.\n\nоткрыть чат: https://t.me/ivan"
    assert courier.deep_link("ivan_p", "Буду в 7 & ок?") == "https://t.me/ivan_p?text=%D0%91%D1%83%D0%B4%D1%83%20%D0%B2%207%20%26%20%D0%BE%D0%BA%3F"
    assert courier.deep_link("", "x") == "" and courier.deep_link("a/../b", "x") == ""
    assert courier.deep_link("ivan_p", "я" * 400) == "", "too long for a link: the text is copied from the card"
