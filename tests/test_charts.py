"""Charts of the life hub: SVG drawn by code from numbers code counted — no script, no style, nothing to run."""

import xml.etree.ElementTree as ET

from retinue import charts

SVG = "{http://www.w3.org/2000/svg}"


def test_a_chart_is_drawn_by_code_with_nothing_that_runs_or_styles():
    svg = charts.bars("Токены за 7 дней", ["вход", "выход", "кеш: чтение", "кеш: запись"],
                      [("triage", [422, 86278, 1795872, 304355]), ("<script>alert(1)</script>", [1, 2, 3, 4]),
                       ("conversation", [272, 51121, 7785378, 1058198])])
    root = ET.fromstring(svg)
    assert root.tag == f"{SVG}svg" and root.get("role") == "img" and root.get("viewBox")
    tags = {element.tag.removeprefix(SVG) for element in root.iter()}
    assert tags <= {"svg", "title", "rect", "text", "g"}, tags
    for element in root.iter():
        assert not [name for name in element.attrib if name == "style" or name.startswith("on") or "href" in name]
    texts = [element.text for element in root.iter(f"{SVG}text")]
    assert "<script>alert(1)</script>" in texts, "a label is text, escaped, never markup"
    assert b"<script>" not in svg and "8,9 млн" in texts, "the total of a row, short"
    widths = {}
    for group in root.iter(f"{SVG}g"):
        widths[group.get("data-row")] = sum(float(r.get("width")) for r in group.iter(f"{SVG}rect"))
    assert widths["conversation"] > widths["triage"] > widths["<script>alert(1)</script>"] >= 0
    assert abs(widths["conversation"] - charts.BAR_WIDTH) < 0.01, "the largest row spans the whole bar"
    legend = [t for t in texts if t in ("вход", "выход", "кеш: чтение", "кеш: запись")]
    assert legend == ["вход", "выход", "кеш: чтение", "кеш: запись"]


def test_numbers_are_short_and_an_empty_chart_says_so():
    assert [charts.short(n) for n in (0, 983, 30_710, 304_355, 1_795_872, 2_500_000_000)] == [
        "0", "983", "31 тыс", "304 тыс", "1,8 млн", "2,5 млрд"]
    empty = ET.fromstring(charts.bars("Запуски", ["запуски"], []))
    assert "нет данных" in [element.text for element in empty.iter(f"{SVG}text")]
    zero = ET.fromstring(charts.bars("Запуски", ["запуски"], [("пн", [0])]))
    assert all(float(r.get("width")) >= 0 for r in zero.iter(f"{SVG}rect"))
