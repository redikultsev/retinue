"""Charts of the life hub: geometry laid out by code from numbers code counted; the page draws it with its own
classes, so a chart follows the theme and its labels are the page's text."""

from retinue import charts


def test_a_chart_is_geometry_in_per_cent_and_labels_are_plain_strings():
    chart = charts.bars("Токены за 7 дней", ["вход", "выход", "кеш: чтение", "кеш: запись"],
                        [("triage", [422, 86278, 1795872, 304355]), ("<script>alert(1)</script>", [1, 2, 3, 4]),
                         ("conversation", [272, 51121, 7785378, 1058198])])
    assert chart["title"] == "Токены за 7 дней" and not chart["empty"]
    assert [s["name"] for s in chart["series"]] == ["вход", "выход", "кеш: чтение", "кеш: запись"]
    rows = {row["label"]: row for row in chart["rows"]}
    assert "<script>alert(1)</script>" in rows, "a label is a string; Hugo escapes it like any value"
    widths = {label: sum(s["w"] for s in row["segments"]) for label, row in rows.items()}
    assert abs(widths["conversation"] - 100) < 0.05, "the largest row spans the whole strip"
    assert widths["conversation"] > widths["triage"] > widths["<script>alert(1)</script>"] >= 0
    triage = rows["triage"]["segments"]
    assert triage[0]["x"] == 0 and all(abs(b["x"] - (a["x"] + a["w"])) < 0.02 for a, b in zip(triage, triage[1:])), \
        "segments are stacked: each starts where the one before ends"
    assert [s["n"] for s in triage] == [1, 2, 3], "by its series' colour; a sliver too thin to see is not drawn"
    assert rows["conversation"]["total"] == "8,9 млн"


def test_numbers_are_short_and_an_empty_chart_says_so():
    assert [charts.short(n) for n in (0, 983, 30_710, 304_355, 1_795_872, 2_500_000_000)] == [
        "0", "983", "31 тыс", "304 тыс", "1,8 млн", "2,5 млрд"]
    assert charts.bars("Запуски", ["запуски"], [])["empty"] is True
    zero = charts.bars("Запуски", ["запуски"], [("пн", [0])])
    assert zero["empty"] is True and zero["rows"][0]["segments"] == [], "nothing to draw, nothing divided by zero"
