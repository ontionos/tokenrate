"""Lay a summary out as styled lines for one terminal width.

A line is a list of (text, style) segments; style names are resolved to
colours by the curses screen (tui.py) or the ANSI printer (snapshot.py), so
this module never touches the terminal. Every glyph used is one cell wide.

The line-and-segment model, eighth-block bars, and stat layout follow
herdr_agents_tracker's dashboard (MIT, Copyright (c) 2026 Hemanth; see
LICENSE.herdr_agents_tracker).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
import time

VIEWS = ("Overview", "Models", "Projects", "Days")
RANGES = (7, 30, 90)
EIGHTHS = " ▏▎▍▌▋▊▉█"
VBLOCKS = " ▁▂▃▄▅▆▇█"
# Bars never touch their neighbours: a horizontal bar is the lower half of its row (▄, with ▖ for a
# half step), so stacked rows keep a gap; a sparkline stops at ▆, so it keeps clear of the row above,
# and puts a space between periods. An empty period is a dim baseline, so a quiet day still shows.
HBAR, HBAR_HALF = "▄", "▖"
SPARK = "▂▃▄▅▆"  # the smallest non-zero bar stands above the empty baseline
SPARK_EMPTY = "▁"
SPARK_NOTE = ("Each row's small bars are scaled to that row's own busiest period and coloured by its main model; "
              "a flat line is a quiet one.")
MODEL_STYLES = tuple(f"m{i}" for i in range(8))
TOKEN_TYPES = (("cache_read", "cache read", "t0"), ("cache_write", "cache write", "t1"),
               ("input", "input", "t2"), ("output", "output", "t3"))
MIN_WIDTH, MIN_HEIGHT = 50, 12
TWO_COLUMNS = 110


class Click(tuple):
    """A (text, style) segment the mouse can click; `action` says what a click does.

    It unpacks like any other segment, so drawing code needs no change; tui.py
    finds these on the drawn frame to know where the clickable controls are.
    Actions: ("view", index), ("days", n), ("refresh",), ("help",).
    """

    def __new__(cls, text: str, style: str, action: tuple):
        seg = super().__new__(cls, (text, style))
        seg.action = action
        return seg


def with_text(seg: tuple, text: str) -> tuple:
    """The same segment with other text, keeping a Click's action."""
    return Click(text, seg[1], seg.action) if isinstance(seg, Click) else (text, seg[1])


# ---- formatting ---------------------------------------------------------------------------------


def tok(n: float) -> str:
    n = float(n or 0)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            v = n / div
            return f"{v:.2f}{suffix}" if v < 10 else f"{v:.1f}{suffix}" if v < 100 else f"{v:.0f}{suffix}"
    return f"{n:.0f}"


def usd(x: float) -> str:
    x = float(x or 0)
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 10:
        return f"${x:.0f}"
    if x >= 0.995:
        return f"${x:.2f}"
    if x > 0:
        return f"${x:.3f}" if x >= 0.01 else f"${x:.4f}"
    return "$0"


def usd_short(x: float) -> str:
    x = float(x or 0)
    if x >= 10000:
        return f"${x / 1000:.0f}k"
    if x >= 1000:
        return f"${x / 1000:.1f}k"
    return f"${x:.0f}"


def pct(x: float) -> str:
    x = 100 * float(x or 0)
    if x >= 9.95:
        return f"{x:.0f}%"
    if x >= 0.05:
        return f"{x:.1f}%"
    return "<0.1%" if x > 0 else "0%"


def clip(text: str, width: int) -> str:
    text = str(text if text is not None else "-")
    if width <= 0:
        return ""
    return (text[:width - 1] + "…" if len(text) > width else text).ljust(width)


def nice_day(iso: str, weekday: bool = True) -> str:
    d = dt.date.fromisoformat(iso)
    return d.strftime("%a %d %b" if weekday else "%d %b")


def seg_len(line: list) -> int:
    return sum(len(t) for t, _ in line)


def pad(line: list, width: int) -> list:
    n = seg_len(line)
    return line + [(" " * (width - n), "")] if n < width else clip_line(line, width)


def clip_line(line: list, width: int) -> list:
    out, x = [], 0
    for seg in line:
        if x >= width:
            break
        text = seg[0][:width - x]
        out.append(with_text(seg, text))
        x += len(text)
    return out


def hcat(left: list, right: list, lw: int, rw: int, gap: int = 3) -> list:
    """Two column blocks side by side, each padded to its width."""
    rows = max(len(left), len(right))
    out = []
    for i in range(rows):
        a = left[i] if i < len(left) else []
        b = right[i] if i < len(right) else []
        out.append(pad(a, lw) + [(" " * gap, "")] + clip_line(b, rw))
    return out


def hbar(fraction: float, width: int, style: str) -> list:
    """Half-height horizontal bar, in half-cell steps, on a dotted track; any non-zero value shows."""
    fraction = max(0.0, min(1.0, float(fraction or 0)))
    halves = round(fraction * width * 2)
    if fraction > 0 and halves == 0:
        halves = 1
    full, half = divmod(halves, 2)
    bar = HBAR * full + (HBAR_HALF if half else "")
    return [(bar, style), ("·" * (width - len(bar)), "track")]


def spark_groups(values: list, width: int) -> tuple[list, int]:
    """(period sums, days per period) so that one bar plus one space per period fits in `width`.
    Periods are counted back from the last day, so the newest bar is always complete."""
    vals = list(values or [])
    slots = (width + 1) // 2
    if not vals or slots <= 0:
        return [], 1
    per = max(1, -(-len(vals) // slots))
    groups, end = [], len(vals)
    while end > 0:
        groups.insert(0, sum(vals[max(0, end - per):end]))
        end -= per
    return groups, per


def spark(values: list, width: int, style: str, day_models: list | None = None,
          styles: dict | None = None) -> list:
    """Sparkline segments: one bar per period with a space between, a dim baseline where it is empty.
    With per-day model costs, each bar takes the colour of the model that cost most in its period."""
    groups, per = spark_groups(values, width)
    top = max(groups, default=0)
    colours = [style] * len(groups)
    if day_models and styles is not None:
        n = len(values)
        for g in range(len(groups)):
            end = n - (len(groups) - 1 - g) * per
            merged = defaultdict(float)
            for day in day_models[max(0, end - per):end]:
                for name, c in (day or {}).items():
                    merged[name] += c
            if merged:
                colours[g] = styles.get(max(merged, key=merged.get), "mo")
    out = []
    for i, v in enumerate(groups):
        if i:
            out.append((" ", ""))
        if v > 0 and top > 0:
            out.append((SPARK[min(len(SPARK) - 1, int(v / top * (len(SPARK) - 0.001)))], colours[i]))
        else:
            out.append((SPARK_EMPTY, "track"))
    return out


def spark_width(n_days: int, width: int) -> int:
    """Cells a sparkline of n_days actually takes inside `width`."""
    return max(0, 2 * len(spark_groups([0] * n_days, width)[0]) - 1)


def spark_title(n_days: int, width: int) -> str:
    per = spark_groups([0] * n_days, width)[1]
    return "per day" if per == 1 else f"per {per} days"


def spark_axis(summary: dict, width: int) -> str:
    """The first and last day under a sparkline column of `width` cells."""
    days = summary["daily"]
    groups, per = spark_groups([0] * len(days), width)
    used = 2 * len(groups) - 1
    if not groups:
        return ""
    first = days[max(0, len(days) - len(groups) * per)]["date"]
    a, b = nice_day(first, False), nice_day(days[-1]["date"], False)
    if used < len(a) + len(b) + 1:
        return b.rjust(used)
    return a + " " * (used - len(a) - len(b)) + b


def bucket(values: list, n: int) -> list:
    """Sum `values` into n nearly equal consecutive buckets."""
    if n <= 0:
        return []
    size = len(values) / n
    return [sum(values[int(i * size):int((i + 1) * size)]) for i in range(n)]


def section(title: str, note: str = "") -> list:
    return [(title.upper(), "head")] + ([("  " + note, "dim")] if note else [])


def model_styles(summary: dict) -> dict:
    """Model name -> colour: the eight biggest models of the window get their own, the rest share grey."""
    return {m["name"]: MODEL_STYLES[i] if i < len(MODEL_STYLES) else "mo"
            for i, m in enumerate(summary.get("models") or [])}


def stack_parts(models: dict, styles: dict) -> list:
    """(style, cost) parts of one bar, biggest model of the window first, models sharing grey merged."""
    order = {name: i for i, name in enumerate(styles)}
    parts = []
    for name, cost in sorted(models.items(), key=lambda kv: order.get(kv[0], len(order))):
        style = styles.get(name, "mo")
        if cost <= 0:
            continue
        if parts and parts[-1][0] == style:
            parts[-1] = (style, parts[-1][1] + cost)
        else:
            parts.append((style, cost))
    return parts


def allot(parts: list, units: int, keep_small: bool = True) -> list:
    """Split `units` between parts in proportion to their size (largest remainder). With keep_small,
    every part keeps at least one unit while there are units enough, so a small model stays visible;
    that suits eighths of a cell, while whole cells of a short bar would overstate a small model."""
    total = sum(c for _s, c in parts)
    if total <= 0 or units <= 0:
        return [0] * len(parts)
    exact = [c / total * units for _s, c in parts]
    got = [int(e) for e in exact]
    if keep_small and units >= len(parts):
        for i in range(len(parts)):
            if got[i] == 0:
                got[i] = 1
    while sum(got) > units:
        i = max((k for k in range(len(got)) if got[k] > 1), key=lambda k: got[k] - exact[k])
        got[i] -= 1
    for i in sorted(range(len(parts)), key=lambda k: -(exact[k] - got[k]))[:units - sum(got)]:
        got[i] += 1
    return got


def stacked_cells(parts: list, eighths: int, height: int) -> list:
    """Glyph and style per row, top row first, of one vertical bar `eighths` tall split into parts.
    A cell holding two parts draws the lower one as the glyph and the upper one as its background
    ("lower/upper"), so the split is exact to an eighth of a cell; only the bar's top cell, which
    has no room for a background, shows just its larger part."""
    units = []
    for (style, _c), n in zip(parts, allot(parts, eighths)):
        units += [style] * n
    rows = []
    for r in range(height):
        lo = (height - 1 - r) * 8
        cell = units[lo:lo + 8]
        if not cell:
            rows.append((" ", ""))
        elif len(cell) < 8 or len(set(cell)) == 1:
            main = max(set(cell), key=cell.count)
            rows.append((VBLOCKS[len(cell)], main))
        else:
            lower = cell[0]
            k = next(i for i, st in enumerate(cell) if st != lower)
            upper = max(set(cell[k:]), key=cell[k:].count)
            rows.append((VBLOCKS[k], f"{lower}/{upper}"))
    return rows


def hbar_stacked(parts: list, fraction: float, width: int) -> list:
    """hbar() split by model: whole cells shared between the parts, a half step in the last part's colour."""
    fraction = max(0.0, min(1.0, float(fraction or 0)))
    halves = round(fraction * width * 2)
    if fraction > 0 and halves == 0:
        halves = 1
    full, half = divmod(halves, 2)
    out = []
    for (style, _c), n in zip(parts, allot(parts, full, keep_small=False)):
        if n:
            out.append((HBAR * n, style))
    if half:
        out.append((HBAR_HALF, out[-1][1] if out else (parts[0][0] if parts else "bar")))
    return out + [("·" * (width - full - half), "track")]


def model_legend(summary: dict, width: int, names: list | None = None) -> list:
    """● name for each colour in use, then one grey "other" for the rest, clipped to the width."""
    styles = model_styles(summary)
    names = [m["name"] for m in summary["models"]] if names is None else names
    out, used, others = [], 0, False
    for name in names:
        style = styles.get(name, "mo")
        if style == "mo":
            others = True
            continue
        item = [("● ", style), (name + "  ", "dim")]
        if used + seg_len(item) > width:
            break
        out += item
        used += seg_len(item)
    if others and used + 9 <= width:
        out += [("● ", "mo"), ("other", "dim")]
    return out


# ---- chrome -------------------------------------------------------------------------------------


def header(summary: dict, view: int, rng: int, width: int, status: str) -> list:
    tabs = [("◆ tokenrate ", "title")]
    for i, name in enumerate(VIEWS):
        tabs += [Click(f" {i + 1} {name} ", "tab_on" if i == view else "tab", ("view", i)), (" ", "")]
    pills = []
    for r in RANGES:
        pills += [Click(f" {r}d ", "tab_on" if r == rng else "tab", ("days", r)), (" ", "")]
    if seg_len(tabs) + seg_len(pills) + 1 > width:
        tabs = [("◆ ", "title")] + [Click(f" {i + 1}{n[0]} ", "tab_on" if i == view else "tab", ("view", i))
                                    for i, n in enumerate(VIEWS)]
    gap = max(1, width - seg_len(tabs) - seg_len(pills))
    line1 = tabs + [(" " * gap, "")] + pills

    start, end = summary["start"], summary["end"]
    period = f"{nice_day(start, False)} - {nice_day(end, False)}"
    line2 = [(tok(summary["tokens"]), "big"), (" tokens   ", "dim"), (usd(summary["cost"]), "money"),
             (" API-equivalent estimate", "dim")]
    tail = [("   ", ""), (period, "bold"), (f" · last {summary['days']} days · {summary['active_days']} active", "dim")]
    if seg_len(line2) + seg_len(tail) <= width:
        line2 += tail
        lines = [line1, line2]
    else:
        lines = [line1, line2, [(period, "bold"), (f" · {summary['active_days']} active days", "dim")]]
    note = "subscription: not a bill"
    button = Click(" ↻ refresh ", "tab", ("refresh",))
    tail = [(note, "dim"), (" · ", "rule"), (status, "status"), (" ", ""), button]
    if seg_len(tail) + 2 > width:
        tail = tail[2:]
    lines.append([("─" * max(0, width - seg_len(tail) - 2), "rule"), ("  ", "")] + tail)
    return lines


def footer(width: int) -> list:
    keys = (("q", "quit", None), ("r", "refresh", ("refresh",)), ("tab 1-4", "view", None),
            ("7 3 9", "days", None), ("↑↓", "scroll", None), ("?", "help", ("help",)))
    line = []
    for k, label, action in keys:
        item = [Click(f" {k} ", "key", action) if action else (f" {k} ", "key"), (f" {label}  ", "dim")]
        if seg_len(line) + seg_len(item) > width:
            break
        line += item
    return line


HELP = [
    [("KEYS", "head")],
    [("  q / Esc      ", "key"), ("close", "")],
    [("  r            ", "key"), ("count again now (ccusage runs once, a few seconds)", "")],
    [("  tab / 1-4    ", "key"), ("Overview, Models, Projects, Days", "")],
    [("  7 / 3 / 9    ", "key"), ("last 7, 30 or 90 days   (d cycles)", "")],
    [("  ↑ ↓ j k      ", "key"), ("scroll; PgUp PgDn space b page; g G top/bottom", "")],
    [("  mouse        ", "key"), ("click a view, a period or refresh; the wheel scrolls", "")],
    [],
    [("WHAT THE NUMBERS MEAN", "head")],
    [("  Tokens and dollars come from ccusage over this machine's Claude Code and Codex logs.", "")],
    [("  Dollars are the public API price of those tokens: an estimate, not a bill.", "")],
    [("  The subscription is billed separately and does not change with these numbers.", "")],
    [("  Cache reads are context re-read on every turn: cheap per token, most of the volume.", "")],
    [("  Today is partial. Data refreshes when it is older than a few minutes or on r.", "")],
]


# ---- blocks -------------------------------------------------------------------------------------


def daily_chart(summary: dict, width: int, height: int = 8) -> list:
    """Vertical bars of cost per day, each split by model in the models' colours, peaks labelled."""
    days = summary["daily"]
    styles = model_styles(summary)
    axis_w = 7
    avail = max(10, width - axis_w)
    n = len(days)
    per = 1
    if 2 * n <= avail:
        col = max(2, min(5, avail // max(1, n)))
        cols = [(d["cost"], stack_parts(d["models"], styles), d) for d in days]
    else:
        # One cell of bar and one of gap per column, so neighbouring bars never touch.
        col = 2
        per = -(-n // max(1, avail // 2))
        cols = []
        for i in range(0, n, per):
            group = days[i:i + per]
            merged = defaultdict(float)
            for d in group:
                for name, c in d["models"].items():
                    merged[name] += c
            cols.append((sum(d["cost"] for d in group), stack_parts(merged, styles), group[0]))
    bar_w = col - 1
    peak = max((c[0] for c in cols), default=0)
    lines = []
    if peak <= 0:
        return [[(" " * axis_w, ""), ("no usage recorded in this window", "dim")]]

    top3 = {id(c) for c in sorted(cols, key=lambda c: -c[0])[:3] if c[0] > 0}
    label_row = [" "] * (len(cols) * col)
    label_style = {}
    for i, c in enumerate(cols):
        if id(c) not in top3:
            continue
        text = usd_short(c[0])
        x = max(0, min(len(label_row) - len(text), i * col + bar_w // 2 - len(text) // 2))
        if all(ch == " " for ch in label_row[max(0, x - 1):x + len(text) + 1]):
            label_row[x:x + len(text)] = list(text)
            for k in range(x, x + len(text)):
                label_style[k] = "peak"
    lines.append([(" " * axis_w, "")] + _styled_row(label_row, label_style))

    stacks = []
    for value, parts, _d in cols:
        eighths = round(value / peak * height * 8) if value > 0 else 0
        if value > 0 and eighths < 2:
            eighths = 2  # a small day stands above the baseline an empty day gets
        stacks.append(stacked_cells(parts, eighths, height) if value > 0 else None)
    for r in range(height):
        if r == 0:
            axis = usd_short(peak).rjust(axis_w - 2) + " ┤"
        elif r == height // 2:
            axis = usd_short(peak / 2).rjust(axis_w - 2) + " ┤"
        else:
            axis = " " * (axis_w - 2) + " │"
        row = [(axis, "axis")]
        for stack in stacks:
            if stack is None:
                row.append(((SPARK_EMPTY if r == height - 1 else " ") * bar_w, "track"))
            else:
                glyph, style = stack[r]
                row.append((glyph * bar_w, style))
            if col > bar_w:
                row.append((" " * (col - bar_w), ""))
        lines.append(row)
    lines.append([(" " * (axis_w - 2) + " └", "axis"), ("─" * (len(cols) * col), "axis")])

    # Weekday initials (weekends dimmed) when each day has its own column, then dates under Mondays.
    wd_row, date_row = [(" " * axis_w, "")], [" "] * (len(cols) * col)
    for i, (_v, _s, d) in enumerate(cols):
        day = dt.date.fromisoformat(d["date"])
        if per == 1:
            wd_row.append((day.strftime("%a")[0].ljust(col)[:col], "weekend" if day.weekday() >= 5 else "axis"))
        if day.weekday() == 0 or i == 0:
            text = day.strftime("%d %b") if col >= 3 or i == 0 else day.strftime("%d")
            x = i * col
            if x + len(text) > len(date_row):
                text = day.strftime("%d")
            if all(ch == " " for ch in date_row[max(0, x - 1):x + len(text) + 1]):
                date_row[x:x + len(text)] = list(text[:len(date_row) - x])
    if len(wd_row) > 1:
        lines.append(wd_row)
    dates = [(" " * axis_w, ""), ("".join(date_row).rstrip(), "axis")]
    if per > 1:
        dates.append((f"   each bar = {per} days", "dim"))
    lines.append(dates)
    in_use = [m["name"] for m in summary["models"] if m["cost"] > 0]
    lines.append([(" " * axis_w, "")] + model_legend(summary, width - axis_w, in_use))
    return lines


def _styled_row(chars: list, styles: dict) -> list:
    out, cur, buf = [], None, ""
    for i, ch in enumerate(chars):
        s = styles.get(i, "")
        if s != cur and buf:
            out.append((buf, cur))
            buf = ""
        cur = s
        buf += ch
    if buf:
        out.append((buf, cur))
    return out


def chart_caption(summary: dict) -> list:
    out = []
    busiest = summary["busiest"]
    if busiest:
        b = busiest[0]
        out += [("peak ", "dim"), (nice_day(b["date"]), "bold"), (" " + usd(b["cost"]), "peak")]
    out += [(" · average active day ", "dim"), (usd(summary["avg_active_day_cost"]), "bold")]
    today = summary["daily"][-1] if summary["daily"] else None
    if today:
        out += [(" · today so far ", "dim"), (usd(today["cost"]), "bold")]
    return out


def models_block(summary: dict, width: int, limit: int = 6) -> list:
    styles = model_styles(summary)
    models = summary["models"]
    lines = [section("Models", "share of the estimate")]
    if not models:
        return lines + [[("  none in this window", "dim")]]
    shown = models[:limit]
    rest = models[limit:]
    name_w = max(8, min(16, max(len(m["name"]) for m in shown) + 1))
    bar_w = max(6, min(48, width - name_w - 26))
    for m in shown:
        lines.append([("● ", styles[m["name"]]), (clip(m["name"], name_w - 1) + " ", "bold")] +
                     hbar(m["share"], bar_w, styles[m["name"]]) +
                     [(f" {pct(m['share']):>5}", "bold"), (f" {usd(m['cost']):>8}", "money"),
                      (f" {tok(m['tokens']):>6}", "dim")])
    if rest:
        cost = sum(m["cost"] for m in rest)
        share = cost / summary["cost"] if summary["cost"] else 0
        lines.append([("● ", "dim"), (clip(f"{len(rest)} more", name_w - 1) + " ", "dim")] + hbar(share, bar_w, "dim") +
                     [(f" {pct(share):>5}", "dim"), (f" {usd(cost):>8}", "dim"),
                      (f" {tok(sum(m['tokens'] for m in rest)):>6}", "dim")])
    return lines


def tokens_block(summary: dict, width: int) -> list:
    total = summary["tokens"] or 0
    tt = summary["token_types"]
    lines = [section("Token types", f"{tok(total)} in total")]
    bar_w = max(10, min(72, width - 2))
    if total <= 0:
        return lines + [[("  none in this window", "dim")]]
    # One stacked bar; every non-zero type keeps at least one cell so it stays visible.
    cells = {k: (tt[k] / total * bar_w) for k, _l, _s in TOKEN_TYPES}
    widths = {k: (max(1, round(v)) if tt[k] > 0 else 0) for k, v in cells.items()}
    biggest = max(widths, key=lambda k: widths[k])
    widths[biggest] -= sum(widths.values()) - bar_w
    lines.append([("█" * widths[k], s) for k, _l, s in TOKEN_TYPES if widths[k] > 0])
    for k, label, style in TOKEN_TYPES:
        lines.append([("■ ", style), (clip(label, 12), "bold"), (f"{tok(tt[k]):>7}", "value"),
                      (f"  {pct(tt[k] / total):>5}", "dim")] +
                     ([("  re-read context, cheapest per token", "dim")] if k == "cache_read" and width >= 60 else []))
    return lines


def projects_block(summary: dict, width: int, limit: int = 8) -> list:
    projects = summary["projects"]
    lines = [section("Projects", "estimate per working folder")]
    if not projects:
        return lines + [[("  none in this window", "dim")]]
    name_w = max(10, min(22, max(len(p["name"]) for p in projects[:limit]) + 1))
    bar_w = max(6, min(48, width - name_w - 19))
    top = projects[0]["cost"] or 1
    styles = model_styles(summary)
    for p in projects[:limit]:
        lines.append([(clip(p["name"], name_w - 1) + " ", "bold")] +
                     hbar_stacked(stack_parts(p.get("models") or {}, styles), p["cost"] / top, bar_w) +
                     [(f" {pct(p['share']):>5}", "dim"), (f" {usd(p['cost']):>8}", "money")])
    if len(projects) > limit:
        lines.append([(f"… {len(projects) - limit} more (view 3)", "dim")])
    return lines


def busiest_block(summary: dict, width: int, limit: int = 6) -> list:
    styles = model_styles(summary)
    lines = [section("Busiest days")]
    busiest = summary["busiest"][:limit]
    if not busiest:
        return lines + [[("  none in this window", "dim")]]
    peak = busiest[0]["cost"] or 1
    bar_w = max(4, min(36, width - 46))
    by_date = {d["date"]: d for d in summary["daily"]}
    for b in busiest:
        weekend = dt.date.fromisoformat(b["date"]).weekday() >= 5
        models = by_date.get(b["date"], {}).get("models", {})
        lines.append([(nice_day(b["date"]), "weekend" if weekend else "bold"), ("  ", "")] +
                     hbar_stacked(stack_parts(models, styles), b["cost"] / peak, bar_w) +
                     [(f" {usd(b['cost']):>7}", "money"), (f" {tok(b['tokens']):>6}", "dim"), ("  ", ""),
                      (clip(b["top_model"] or "-", 10), styles.get(b["top_model"], "dim"))])
    return lines


def change_lines(summary: dict, width: int) -> list:
    """The 7-day comparison, wrapped onto a second line when the width is short."""
    line = change_line(summary)
    if seg_len(line) <= width or len(line) < 3:
        return [line]
    cut = next((i for i, (t, _s) in enumerate(line) if "tokens" in t), len(line))
    return [line[:cut], [(" " * 14, "")] + line[cut:]]


def change_line(summary: dict) -> list:
    d = summary["delta"]
    last, prev = d["last7"], d["prev7"]
    out = [("WHAT CHANGED", "head"), ("  last 7 days ", "dim"), (usd(last["cost"]), "money")]
    if not prev or not prev["cost"]:
        return out + [("  (no earlier week to compare)", "dim")]
    out += [(" vs ", "dim"), (usd(prev["cost"]), "bold"), (" the 7 before  ", "dim")]
    out += _delta(last["cost"], prev["cost"], "cost")
    if prev["tokens"]:
        out += [("  ", "")] + _delta(last["tokens"], prev["tokens"], "tokens")
    return out


def _delta(new: float, old: float, label: str) -> list:
    change = (new - old) / old if old else 0
    arrow, style = ("▲", "up") if change > 0.005 else ("▼", "down") if change < -0.005 else ("=", "dim")
    return [(f"{arrow} {change * 100:+.0f}% {label}", style)]


def classifiers_line(summary: dict) -> list | None:
    c = summary.get("classifiers")
    if not c:
        return None
    out = [("CHEAP CLASSIFIERS", "head")]
    names = {"jev": "Jev (text)", "clef": "Clef (screens)"}
    for kind in ("jev", "clef"):
        if kind in c:
            k = c[kind]
            out += [("  " + names[kind] + " ", "dim"), (f"{k['calls']:,} calls", "bold"),
                    (f" {usd(k['cost'])}", "money")]
            if k.get("unsure"):
                out += [(f" · {k['unsure']} unsure", "dim")]
    return out


# ---- views --------------------------------------------------------------------------------------


# Overview layouts from roomiest to most compact: (chart rows, models, projects, busiest days).
# The first that fits the window wins, and spare rows then make the chart taller, so a window that
# can hold the whole overview never needs scrolling.
OVERVIEW_FITS = ((8, 6, 8, 6), (7, 6, 8, 6), (6, 6, 8, 6), (6, 4, 5, 4), (5, 4, 5, 4), (4, 4, 5, 4), (4, 3, 3, 3))
CHART_MAX = 16


def overview(summary: dict, width: int, room: int | None = None) -> list:
    fits = OVERVIEW_FITS if width >= 70 else tuple(f for f in OVERVIEW_FITS if f[0] <= 6)
    if room is None:
        return overview_lines(summary, width, *fits[0])
    for chart, *limits in fits:
        lines = overview_lines(summary, width, chart, *limits)
        if len(lines) <= room:
            while chart < CHART_MAX and len(lines) < room:
                taller = overview_lines(summary, width, chart + 1, *limits)
                if len(taller) > room:
                    break
                chart, lines = chart + 1, taller
            return lines
    return lines


def overview_lines(summary: dict, width: int, chart: int, n_models: int, n_projects: int, n_busiest: int) -> list:
    lines = [section("Cost per day", "API-equivalent estimate, each bar split by model")]
    lines += daily_chart(summary, width, chart)
    lines += [chart_caption(summary), []]
    models = lambda w: models_block(summary, w, limit=n_models)
    projects = lambda w: projects_block(summary, w, limit=n_projects)
    busiest = lambda w: busiest_block(summary, w, limit=n_busiest)
    tokens = lambda w: tokens_block(summary, w)
    if width >= TWO_COLUMNS:
        lw = (width - 3) * 11 // 20
        rw = width - 3 - lw
        lines += hcat(models(lw), tokens(rw), lw, rw) + [[]]
        lines += hcat(projects(lw), busiest(rw), lw, rw) + [[]]
    else:
        for block in (models, tokens, projects, busiest):
            lines += block(width) + [[]]
    lines += change_lines(summary, width)
    cl = classifiers_line(summary)
    if cl:
        lines.append(cl)
    return lines


def models_view(summary: dict, width: int) -> list:
    styles = model_styles(summary)
    lines = models_block(summary, width, limit=len(summary["models"]) or 1) + [[]]
    lines.append(section("Per model", "tokens by type and the estimate per day"))
    if not summary["models"]:
        return lines
    name_w = max(10, min(18, max(len(m["name"]) for m in summary["models"]) + 1))
    n = len(summary["daily"])
    spark_w = min(max(0, width - name_w - 44), max(0, 2 * n - 1))
    if spark_width(n, spark_w) < 7:
        spark_w = 0
    head = (clip("model", name_w) + f"{'cache rd':>8}{'cache wr':>9}{'input':>8}{'output':>8}{'est.':>9}  " +
            (spark_title(n, spark_w) if spark_w else ""))
    lines.append([(head, "dim")])
    for m in summary["models"]:
        row = [(clip(m["name"], name_w), styles[m["name"]]),
               (f"{tok(m['cache_read']):>8}{tok(m['cache_write']):>9}{tok(m['input']):>8}{tok(m['output']):>8}", ""),
               (f"{usd(m['cost']):>9}", "money"), ("  ", "")]
        if spark_w:
            row += spark(m["daily"], spark_w, styles[m["name"]])
        lines.append(row)
    if spark_w:
        lines += [[(" " * (name_w + 44), ""), (spark_axis(summary, spark_w), "axis")], [], [(SPARK_NOTE, "dim")]]
    return lines


def projects_view(summary: dict, width: int) -> list:
    projects = summary["projects"]
    lines = [section("Projects", f"{len(projects)} with usage · estimate per working folder")]
    if not projects:
        return lines + [[("  none in this window", "dim")]]
    name_w = max(12, min(28, max(len(p["name"]) for p in projects) + 1))
    n = len(summary["daily"])
    room = max(0, width - name_w - 24)  # what the bar and the sparkline share
    spark_w = spark_width(n, min(2 * n - 1, room * 3 // 5))
    if spark_w < 7:
        spark_w = 0
    bar_w = max(6, room - spark_w - (2 if spark_w else 0))
    top = projects[0]["cost"] or 1
    styles = model_styles(summary)
    head = clip("project", name_w) + " " * bar_w + f"{'share':>6}{'est.':>9}{'tokens':>7}"
    lines.append([(head + ("  " + spark_title(n, spark_w) if spark_w else ""), "dim")])
    for p in projects:
        row = [(clip(p["name"], name_w - 1) + " ", "bold")] + \
              hbar_stacked(stack_parts(p.get("models") or {}, styles), p["cost"] / top, bar_w) + \
              [(f" {pct(p['share']):>5}", "dim"), (f" {usd(p['cost']):>8}", "money"), (f" {tok(p['tokens']):>6}", "dim")]
        if spark_w:
            row += [("  ", "")] + spark(p["daily"], spark_w, "proj", p.get("daily_models"), styles)
        lines.append(row)
    if spark_w:
        lines.append([(" " * (name_w + bar_w + 24), ""), (spark_axis(summary, spark_w), "axis")])
    lines += [[], model_legend(summary, width),
              [("Bars are split by model; Claude Code by working folder per day, Codex by session folder on its last day.", "dim")]]
    if spark_w:
        lines.append([(SPARK_NOTE, "dim")])
    return lines


def days_view(summary: dict, width: int, height: int) -> list:
    styles = model_styles(summary)
    lines = [section("Cost per day", "API-equivalent estimate, each bar split by model")]
    lines += daily_chart(summary, width, max(6, min(14, height // 3)))
    lines += [chart_caption(summary), []] + change_lines(summary, width) + [[]]
    lines.append(section("Every day", "newest first"))
    peak = summary["peak_cost"] or 1
    bar_w = max(6, width - 58)
    lines.append([(f"{'day':<12}{'':<{bar_w}}{'est.':>9}{'tokens':>8}{'cache':>7}  main model", "dim")])
    for d in reversed(summary["daily"]):
        weekend = dt.date.fromisoformat(d["date"]).weekday() >= 5
        cache = d["cache_read"] / d["tokens"] if d["tokens"] else 0
        if not d["cost"] and not d["tokens"]:
            lines.append([(clip(nice_day(d["date"]), 12), "weekend" if weekend else "dim"), ("·" * bar_w, "track"),
                          (f"{'-':>9}", "dim")])
            continue
        lines.append([(clip(nice_day(d["date"]), 12), "weekend" if weekend else "bold")] +
                     hbar_stacked(stack_parts(d["models"], styles), d["cost"] / peak, bar_w) +
                     [(f"{usd(d['cost']):>9}", "money"), (f"{tok(d['tokens']):>8}", ""),
                      (f"{pct(cache):>7}", "dim"), ("  ", ""),
                      (clip(d["top_model"] or "-", 14), styles.get(d["top_model"], "dim"))])
    return lines


def body(summary: dict, view: int, width: int, height: int, room: int | None = None) -> list:
    """The view's lines; `room` is how many rows the body gets, which the overview fits itself to."""
    if not summary["has_data"]:
        first = summary.get("history_first")
        return [[], [("No token usage in this window.", "bold")],
                [(f"History starts {first}." if first else "ccusage found no Claude Code or Codex logs yet.", "dim")],
                [("Press 9 for 90 days or r to count again.", "dim")]]
    if view == 1:
        return models_view(summary, width)
    if view == 2:
        return projects_view(summary, width)
    if view == 3:
        return days_view(summary, width, height)
    return overview(summary, width, room)


def status_text(summary: dict | None, refreshing: bool, error: str | None, now: float | None = None) -> str:
    if refreshing:
        return "counting…"
    if error:
        return "refresh failed"
    fetched = (summary or {}).get("fetched_at")
    if not fetched:
        return "no data yet"
    now = now or time.time()
    mins = int((now - fetched) // 60)
    when = time.strftime("%H:%M", time.localtime(fetched))
    return f"updated {when}" + (f" ({mins} min ago)" if mins >= 1 else "")


def screen(summary: dict, view: int, width: int, height: int, status: str, error: str | None = None,
           show_help: bool = False) -> tuple[list, list, list]:
    """(header, body, footer) lines for a terminal of width x height; width excludes the 1-cell margins."""
    head = header(summary, view, summary["days"], width, status)
    if error:
        head.append([("! ", "up"), (error, "up")])
    foot = [footer(width)]
    content = HELP if show_help else body(summary, view, width, height, max(1, height - len(head) - len(foot)))
    return head, content, foot


def loading_screen(width: int, message: str) -> list:
    return [[("◆ tokenrate", "title")], [], [(message, "bold")],
            [("ccusage reads the local Claude Code and Codex logs once; it takes a few seconds.", "dim")]]
