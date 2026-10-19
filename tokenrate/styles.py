"""Style names -> colours, shared by the curses screen and the snapshot printer.

Foregrounds are mid-luminance xterm-256 colours chosen to stay readable on both
dark and light backgrounds; the background is always the terminal's own, so a
light or dark theme needs no setting. Each entry also names a basic 8-colour
fallback for terminals without 256 colours. Text never depends on colour alone.
"""

# name: (xterm-256 foreground or None, 8-colour fallback or None, attributes)
# attributes: b bold, d dim, r reverse
STYLES = {
    "": (None, None, ""),
    "bold": (None, None, "b"),
    "dim": (None, None, "d"),
    "value": (None, None, "b"),
    "big": (None, None, "b"),
    "title": (33, "blue", "b"),
    "head": (33, "blue", "b"),
    "tab": (None, None, ""),
    "tab_on": (None, None, "rb"),
    "tab_hot": (33, "blue", "rb"),
    "key": (None, None, "r"),
    "rule": (None, None, "d"),
    "status": (None, None, "d"),
    "axis": (244, None, ""),
    "track": (None, None, "d"),
    "money": (35, "green", "b"),
    "peak": (166, "yellow", "b"),
    "up": (160, "red", "b"),
    "down": (35, "green", "b"),
    "weekend": (98, "magenta", ""),
    "bar": (33, "blue", ""),
    "proj": (37, "cyan", ""),
    "proj_codex": (136, "yellow", ""),
    "m0": (33, "blue", ""),
    "m1": (170, "magenta", ""),
    "m2": (208, "yellow", ""),
    "m3": (35, "green", ""),
    "m4": (98, "magenta", ""),
    "m5": (37, "cyan", ""),
    "m6": (167, "red", ""),
    "m7": (136, "yellow", ""),
    "mo": (244, "white", ""),  # every model after the eighth: one grey "other" colour
    "t0": (37, "cyan", ""),
    "t1": (98, "magenta", ""),
    "t2": (208, "yellow", ""),
    "t3": (170, "magenta", "b"),
}

BASIC = {"black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7}

# Approximate RGB for the xterm-256 colours above, for SVG screenshots only.
RGB = {33: "#0087ff", 35: "#00af5f", 37: "#00afaf", 98: "#875fd7", 136: "#af8700", 160: "#d70000",
       166: "#d75f00", 167: "#d75f5f", 170: "#d75fd7", 208: "#ff8700", 244: "#808080"}
THEMES = {"dark": {"bg": "#1c1c1e", "fg": "#e6e6e6", "dim": "#8a8a8f"},
          "light": {"bg": "#fbfbf8", "fg": "#1f1f1f", "dim": "#7a7a7a"}}


def get(name: str):
    """(foreground, fallback, attributes) of a style. A two-colour style "a/b" is a cell whose glyph
    is drawn in a's colour over b's colour as background: the stacked bars split one cell that way."""
    return STYLES.get(name.split("/", 1)[0], STYLES[""])


def background(name: str):
    """(xterm-256 colour, 8-colour fallback) of a two-colour style's background, or (None, None)."""
    if "/" not in name:
        return None, None
    fg, fallback, _attrs = STYLES.get(name.split("/", 1)[1], STYLES[""])
    return fg, fallback
