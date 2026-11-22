"""Lay out a portrait cover over a representative frame from the source."""

from .captions import ASS_HEADER, escape_ass, width


def headline_lines(title):
    title = " ".join(title.split())
    lines = []
    while title and len(lines) < 3:
        cut = 0
        while cut < len(title) and width(title[:cut + 1]) <= 18:
            cut += 1
        if cut < len(title) and " " in title[:cut + 1]:
            cut = title.rfind(" ", 0, cut + 1)
        lines.append(title[:cut].strip())
        title = title[cut:].lstrip()
    if title:
        lines[-1] = lines[-1].rstrip(".,:;!?…") + "…"
    return lines or ["Highlight"]


def write_cover_script(title, path):
    lines = headline_lines(title)
    events = [ASS_HEADER.replace("WrapStyle: 0", "WrapStyle: 2")]

    def add(layer, body):
        events.append(f"Dialogue: {layer},0:00:00.00,9:59:59.00,Caption,,0,0,0,,{body}\n")

    def rect(x, y, w, h, color, alpha):
        add(0, rf"{{\an7\pos({x},{y})\p1\bord0\shad0\1c&H{color}&\1a&H{alpha:02X}&}}"
               f"m 0 0 l {w} 0 {w} {h} 0 {h}")

    # A smooth darkening veil keeps the subject visible above the headline.
    for y in range(620, 1920, 10):
        fraction = min(1.0, (y - 620) / 1040)
        opacity = round(231 * fraction * fraction * (3 - 2 * fraction))
        rect(0, y, 1080, min(10, 1920 - y), "160E0C", 255 - opacity)
    top = 1530 - (len(lines) - 1) * 116
    rect(86, top - 38, 102, 9, "60EEFF", 0)
    for i, line in enumerate(lines):
        color = "60EEFF" if i == len(lines) - 1 else "FFFFFF"
        # Keep unusually wide uppercase strings inside the phone's safe area.
        units = sum(1.6 if c.upper() in "WM@" else 1.25 if c.isupper() else width(c) for c in line)
        size = min(120, int(1400 / max(1, units)))
        body = rf"{{\an7\pos(82,{top + 116*i})\fs{size}\b1\bord1.7\shad1\1c&H{color}&}}"
        add(1, body + escape_ass(line))
    path.write_text("".join(events), encoding="utf-8")
