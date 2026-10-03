from retinue.render import render


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
