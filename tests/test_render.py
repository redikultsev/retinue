import re

from retinue.render import render, render_telegram, tg_plain


def test_table_becomes_list():
    text = "Цены:\n\n| Авиакомпания | Цена | Пересадка |\n|---|---|---|\n| **Lufthansa** | от 270 € | FRA |\n| Wizz Air | от £56 | — |\n\nДальше."
    body, html = render(text)
    assert "|" not in body
    assert "- **Lufthansa** — от 270 € · FRA" in body
    assert "<table" not in html
    assert "<li><strong>Wizz Air</strong> — от £56 · —</li>" in html
    assert html.endswith("<p>Дальше.</p>")


def test_no_raw_html_and_no_external_images():
    body, html = render('<b>x</b> ![logo](https://e.com/a.png) ~~old~~')
    assert "<b>" not in html
    assert "<img" not in html
    assert '<a href="https://e.com/a.png">logo</a>' in html
    assert "<s>old</s>" in html


def test_empty():
    assert render("  ")[0] == "(пусто)"


def clickable(html: str) -> list[str]:
    """Addresses Telegram would turn into links: anything address-like left outside <code> and <pre>."""
    outside = re.sub(r"<pre>.*?</pre>|<code>.*?</code>", " ", html, flags=re.S)
    return re.findall(r"[a-z]+://\S+|www\.\S+|\S+@\S+\.\S+|\b[\w-]+\.(?:com|example|org|ru|me|рф)\b", outside)


def test_telegram_addresses_are_code_not_links():
    text = ("Смотри https://example.com/a?b=1&c=2, и [сайт](https://evil.example/x?d=secret).\n\n"
            "## Заголовок с example.com и [ссылкой](http://a.example/c)\n\n"
            "**жирный https://b.example/x и `код`**, почта ivan@example.com, t.me/durov, www.site.ru, сайт.рф/путь\n\n"
            "Автоссылка <https://c.example/z>, картинка ![лого](https://d.example/a.png), tg://resolve?domain=x\n\n"
            "> цитата с https://q.example\n\n- *курсив e.example.org*\n- ~~старый https://s.example~~")
    html = "\n\n".join(render_telegram(text))
    assert "<a " not in html and "href" not in html, "no link is ever produced"
    assert clickable(html) == []
    assert "Смотри <code>https://example.com/a?b=1&amp;c=2</code>, и сайт (<code>https://evil.example/x?d=secret</code>)." in html
    assert "<b>Заголовок с </b><code>example.com</code><b> и ссылкой (</b><code>http://a.example/c</code><b>)</b>" in html
    assert "<b>жирный </b><code>https://b.example/x</code><b> и </b><code>код</code>," in html, "code is never inside bold"
    assert "Автоссылка <code>https://c.example/z</code>, картинка лого (<code>https://d.example/a.png</code>)" in html
    assert not re.search(r"<(b|i|s)>[^<]*<code>", html) and "<b></b>" not in html and "<i></i>" not in html


def test_telegram_ordinary_text_is_left_alone():
    html = render_telegram("Версия v2.1.286, число 3.14, т.е. и т.д., **важно** и `код`.")[0]
    assert html == "Версия v2.1.286, число 3.14, т.е. и т.д., <b>важно</b> и <code>код</code>."


def test_telegram_huge_block_and_markup_fallback_keep_addresses_wrapped():
    pieces = render_telegram("```\n" + ("curl https://leak.example/" + "a" * 60 + "\n") * 120 + "```")
    assert len(pieces) > 1 and all(len(p) <= 4000 for p in pieces)
    assert all(clickable(p) == [] for p in pieces), "a block too long for one message is still wrapped"
    plain = tg_plain('<b>Итог</b> <i>см.</i> &lt;тег&gt; https://x.example/z?a=1&amp;b=2 и почта a@b.example')
    assert plain == "Итог см. &lt;тег&gt; <code>https://x.example/z?a=1&amp;b=2</code> и почта <code>a@b.example</code>"


HOSTS = ("www.aviasales.ru", "kiwi.com", "www.google.com/travel/", "www.trivago.com", "www.booking.com")


def test_only_a_link_to_a_travel_site_is_a_link():
    """§15 and the owner's decision: a link is clickable only when it leads to a site travel-ops searches; the
    model may write any path there, but not another host, a port, a login or a lookalike."""
    text = ("[Aviasales, 119 €](https://www.aviasales.ru/search/BEG2210TGD23101), "
            "**жирно https://kiwi.com/u/abc?x=1&y=2**\n\n"
            "[Туту](https://avia.tutu.ru/f/?route[0]=1), [Google](https://www.google.com/search?q=passport), "
            "[Flights](https://www.google.com/travel/flights?tfs=x), https://www.trivago.com/ru/oar/hotel?x=1, "
            "https://www.trivago.evil.example/x, https://booking.com.evil.example/x, https://user@www.booking.com/x, "
            "https://www.booking.com:8443/x, http://www.booking.com/x и [чужой](https://evil.example/x)")
    html = "\n\n".join(render_telegram(text, HOSTS))
    assert re.findall(r'<a href="([^"]+)">([^<]*)</a>', html) == [
        ("https://www.aviasales.ru/search/BEG2210TGD23101", "Aviasales, 119 €"),
        ("https://kiwi.com/u/abc?x=1&amp;y=2", "https://kiwi.com/u/abc?x=1&amp;y=2"),
        ("https://www.google.com/travel/flights?tfs=x", "Flights"),
        ("https://www.trivago.com/ru/oar/hotel?x=1", "https://www.trivago.com/ru/oar/hotel?x=1")]
    for kept in ("https://avia.tutu.ru/f/?route%5B0%5D=1", "https://www.google.com/search?q=passport",
                 "https://www.trivago.evil.example/x", "https://booking.com.evil.example/x",
                 "https://user@www.booking.com/x", "https://www.booking.com:8443/x", "http://www.booking.com/x",
                 "https://evil.example/x"):
        assert f"<code>{kept}</code>" in html, kept
    assert "href" not in "".join(render_telegram(text)), "without the list nothing is a link, as before"


def test_a_listed_site_is_one_host_and_its_path_cannot_be_climbed_out_of():
    """Exact hosts, not «a site and every subdomain» and not «trivago in any domain»: `www.trivago.qzx.io` is
    anybody's. A path prefix holds only if no segment climbs out of it, plainly or encoded."""
    from retinue.render import linkable

    hosts = ("www.google.com/travel/", "www.trivago.com", "kiwi.com", "www.booking.com")
    for url in ("https://www.google.com/travel/../amp/s/evil.example/x",
                "https://www.google.com/travel/%2e%2e/url?q=https://evil.example",
                "https://www.google.com/travel/%2E%2E%2Furl", "https://www.google.com/travel/.%2e/x",
                "https://www.google.com/travel/flights/%2fx", "https://www.google.com/travel/a%5c..%5cb",
                "https://www.google.com/travel/./flights", "https://www.google.com/travel\\..\\x",
                "https://www.trivago.qzx.io/login", "https://www.trivago.co.uk/x", "https://evil.kiwi.com/u/x",
                "https://booking.com/x", "https://www.booking.com.qzx.io/x"):
        assert not linkable(url, hosts), url
    for url in ("https://www.google.com/travel/flights?tfs=CBwQ&hl=en", "https://www.trivago.com/en-US/oar/x",
                "https://kiwi.com/u/u6xbs4", "https://www.booking.com/hotel/me/x.html?checkin=2026-10-22"):
        assert linkable(url, hosts), url
