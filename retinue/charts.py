"""Charts of the life hub: numbers laid out by code, drawn by the page.

A chart used to be an SVG file shown through <img> — a picture cannot take the page's colours, so it stayed light
in the dark theme, and its 13 px labels shrank to 7 px on a phone. Now code gives the geometry only — every bar's
offset and width in per cent of the row — and the page draws each row as text and a strip of rects coloured by
CSS classes (`s0`…`s5`), so a chart follows the theme and its labels are the page's own text. Labels are
somebody's strings (a run kind, a day) and Hugo escapes them like any other value.
"""

from __future__ import annotations

SERIES = 6  # colour classes s0…s5 in the stylesheet


def short(number: float) -> str:
    """983, 31 тыс, 1,8 млн — a number as a label."""
    for size, word in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс")):
        if abs(number) >= size:
            value = number / size
            return (f"{value:.1f}".replace(".", ",").removesuffix(",0") if value < 10 else f"{value:.0f}") + f" {word}"
    return f"{number:.0f}"


def bars(title: str, series: list[str], rows: list[tuple[str, list[float]]], colours: list[int] | None = None) -> dict:
    """Horizontal bars, one per row, each split into `series` (stacked); the longest row spans 100. Each segment:
    its offset `x` and width `w` in per cent, its colour class `n` (`colours`, by series, when a colour means
    something — s1 green, s4 red) and its value as a label."""
    largest = max((sum(values) for _, values in rows), default=0)
    colours = [c % SERIES for c in colours or ()] + [i % SERIES for i in range(len(colours or ()), len(series))]
    out = {"title": title, "series": [{"name": name, "n": colours[index]} for index, name in enumerate(series)],
           "rows": [], "empty": not rows or not largest}
    for label, values in rows:
        x, segments = 0.0, []
        for index, value in enumerate(values):
            width = round(100 * value / largest, 2) if largest else 0.0
            if width > 0:
                segments.append({"x": round(x, 2), "w": width, "n": colours[index] if index < len(colours)
                                 else index % SERIES, "value": short(value),
                                 "series": series[index] if index < len(series) else ""})
            x += width
        out["rows"].append({"label": str(label)[:40], "total": short(sum(values)), "segments": segments})
    return out
