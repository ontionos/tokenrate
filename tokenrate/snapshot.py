"""Headless rendering of one dashboard frame: ANSI, plain text, or SVG.

It lays the frame out exactly as the curses screen does (same view.screen call,
same margins and height clipping), so a snapshot at a given size shows what a
terminal of that size would show, without opening one.
"""

from __future__ import annotations

from html import escape

from . import styles, view


def frame(summary: dict, view_index: int, width: int, height: int, status: str, error: str | None = None,
          show_help: bool = False, scroll: int = 0) -> list:
    """The full width x height grid as lines of segments, including the 1-cell side margins."""
    if width < view.MIN_WIDTH or height < view.MIN_HEIGHT:
        return [[(" Terminal too small for the dashboard (needs "
                  f"{view.MIN_WIDTH}x{view.MIN_HEIGHT}). Press q to close.", "bold")]]
    inner = width - 2
    head, body, foot = view.screen(summary, view_index, inner, height, status, error, show_help)
    room = max(0, height - len(head) - len(foot))
    scroll = max(0, min(scroll, max(0, len(body) - room)))
    shown = body[scroll:scroll + room]
    lines = head + shown + [[]] * (room - len(shown)) + foot
    return [[(" ", "")] + view.clip_line(line, inner) for line in lines[:height]]


def sgr(name: str, basic: bool = False) -> str:
    fg, fallback, attrs = styles.get(name)
    codes = []
    if "b" in attrs:
        codes.append("1")
    if "d" in attrs:
        codes.append("2")
    if "r" in attrs:
        codes.append("7")
    if basic and fallback:
        codes.append(str(30 + styles.BASIC[fallback]))
    elif fg is not None:
        codes.append(f"38;5;{fg}")
    bg, bg_fallback = styles.background(name)
    if basic and bg_fallback:
        codes.append(str(40 + styles.BASIC[bg_fallback]))
    elif bg is not None:
        codes.append(f"48;5;{bg}")
    return f"\x1b[{';'.join(codes)}m" if codes else ""


def ansi(lines: list, basic: bool = False) -> str:
    out = []
    for line in lines:
        parts = []
        for text, style in line:
            code = sgr(style, basic)
            parts.append(f"{code}{text}\x1b[0m" if code else text)
        out.append("".join(parts).rstrip())
    return "\n".join(out) + "\n"


def plain(lines: list) -> str:
    return "\n".join("".join(t for t, _ in line).rstrip() for line in lines) + "\n"


def svg(lines: list, width: int, theme: str = "dark", font_size: int = 14) -> str:
    """A terminal-like SVG of the frame: one fixed cell per character, block glyphs drawn as cell-filling
    rectangles the way a terminal draws them, so the picture matches what the screen shows."""
    t = styles.THEMES.get(theme, styles.THEMES["dark"])
    cw, lh = round(font_size * 0.6, 2), round(font_size * 1.3, 2)
    pad = 18
    w, h = int(width * cw + 2 * pad), int(len(lines) * lh + 2 * pad)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
           f'<rect width="100%" height="100%" rx="10" fill="{t["bg"]}"/>',
           f'<g font-family="Menlo, SF Mono, DejaVu Sans Mono, monospace" font-size="{font_size}">']
    for y, line in enumerate(lines):
        x = 0
        top = pad + y * lh
        for text, style in line:
            fg, _fallback, attrs = styles.get(style)
            color = styles.RGB.get(fg, t["fg"]) if fg is not None else t["fg"]
            if "d" in attrs:
                color = t["dim"]
            if "r" in attrs:
                out.append(f'<rect x="{pad + x * cw:.2f}" y="{top:.2f}" width="{len(text) * cw:.2f}" '
                           f'height="{lh:.2f}" fill="{color}"/>')
                color = t["bg"]
            bg, _bg_fallback = styles.background(style)
            if bg is not None:
                out.append(f'<rect x="{pad + x * cw:.2f}" y="{top:.2f}" width="{len(text) * cw + 0.3:.2f}" '
                           f'height="{lh + 0.3:.2f}" fill="{styles.RGB.get(bg, t["fg"])}"/>')
            weight = ' font-weight="bold"' if "b" in attrs else ""
            xs, chars = [], []
            for i, ch in enumerate(text):
                cx = pad + (x + i) * cw
                if ch in view.VBLOCKS[1:]:
                    k = view.VBLOCKS.index(ch)
                    out.append(f'<rect x="{cx:.2f}" y="{top + lh * (1 - k / 8):.2f}" width="{cw + 0.3:.2f}" '
                               f'height="{lh * k / 8 + 0.3:.2f}" fill="{color}"/>')
                elif ch == view.HBAR_HALF:
                    out.append(f'<rect x="{cx:.2f}" y="{top + lh / 2:.2f}" width="{cw / 2 + 0.3:.2f}" '
                               f'height="{lh / 2 + 0.3:.2f}" fill="{color}"/>')
                elif ch in view.EIGHTHS[1:]:
                    k = view.EIGHTHS.index(ch)
                    out.append(f'<rect x="{cx:.2f}" y="{top:.2f}" width="{cw * k / 8 + 0.3:.2f}" '
                               f'height="{lh + 0.3:.2f}" fill="{color}"/>')
                elif ch != " ":
                    xs.append(f"{cx:.2f}")
                    chars.append(ch)
            if chars:
                out.append(f'<text x="{" ".join(xs)}" y="{top + lh * 0.76:.2f}" fill="{color}"{weight}>'
                           f'{escape("".join(chars))}</text>')
            x += len(text)
    out.append("</g></svg>")
    return "\n".join(out) + "\n"
