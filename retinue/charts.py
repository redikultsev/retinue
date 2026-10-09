"""Charts of the life hub, drawn by hand as SVG: a few rects and texts, no library, no script, no style.

The page shows them through <img>, and the site's policy (`default-src 'self'`, no 'unsafe-inline') would block a
`style` attribute anyway: colours are SVG presentation attributes. Every label is somebody's string — a run kind,
a day — and goes in escaped.
"""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

WIDTH = 640
LABEL_WIDTH = 170          # the labels' column
BAR_WIDTH = 360.0          # the longest row spans this much
ROW = 26
TOP = 34                   # below the title
COLOURS = ("#2f6db3", "#e08a2c", "#7fb0de", "#b9c7d6", "#5b9a5b", "#a05fa8")  # readable on light and dark pages
INK, MUTED, CARD = "#1f2933", "#52606d", "#f7f9fb"


def short(number: float) -> str:
    """983, 31 тыс, 1,8 млн — a number as a label."""
    for size, word in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс")):
        if abs(number) >= size:
            value = number / size
            return (f"{value:.1f}".replace(".", ",").removesuffix(",0") if value < 10 else f"{value:.0f}") + f" {word}"
    return f"{number:.0f}"


def _text(x: float, y: float, words: str, colour: str = INK, anchor: str = "start", size: int = 13) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{colour}" font-size="{size}" font-family="sans-serif" '
            f'text-anchor="{anchor}">{escape(words)}</text>')


def bars(title: str, series: list[str], rows: list[tuple[str, list[float]]]) -> bytes:
    """Horizontal bars, one per row, each split into `series` (stacked), the row's total after it, a legend below."""
    height = TOP + max(1, len(rows)) * ROW + 16 + (ROW if len(series) > 1 else 0)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" '
           f'height="{height}" role="img" aria-label={quoteattr(title)}>',
           f"<title>{escape(title)}</title>",
           f'<rect x="0" y="0" width="{WIDTH}" height="{height}" rx="8" fill="{CARD}"/>',
           _text(12, 22, title, size=15)]
    largest = max((sum(values) for _, values in rows), default=0)
    if not rows:
        out.append(_text(12, TOP + 18, "нет данных", MUTED))
    for number, (label, values) in enumerate(rows):
        y = TOP + number * ROW
        out.append(f"<g data-row={quoteattr(label)}>")
        x = float(LABEL_WIDTH)
        for index, value in enumerate(values):
            width = BAR_WIDTH * value / largest if largest else 0.0
            out.append(f'<rect x="{x:.2f}" y="{y + 4}" width="{width:.2f}" height="{ROW - 8}" '
                       f'fill="{COLOURS[index % len(COLOURS)]}"/>')
            x += width
        out.append("</g>")
        out.append(_text(LABEL_WIDTH - 8, y + 18, label[:28], anchor="end"))
        out.append(_text(LABEL_WIDTH + BAR_WIDTH + 8, y + 18, short(sum(values)), MUTED))
    if len(series) > 1:
        x, y = 12.0, TOP + max(1, len(rows)) * ROW + 12
        for index, name in enumerate(series):
            out.append(f'<rect x="{x:.1f}" y="{y}" width="12" height="12" fill="{COLOURS[index % len(COLOURS)]}"/>')
            out.append(_text(x + 16, y + 11, name, MUTED, size=12))
            x += 24 + 7.5 * len(name)
    out.append("</svg>")
    return "\n".join(out).encode()
