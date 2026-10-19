"""The interactive curses screen.

It redraws only when something changed (a key, a click, a resize, new data, or
the minute ticking over), so an idle dashboard does not repaint. Counting runs
ccusage in a background thread while the cached numbers stay on screen.

Mouse: it asks the terminal for button presses only (mode 1000) in SGR encoding
(1006), never motion tracking (1002/1003) or alternate-scroll (1007), so the
pointer moving over the pane sends nothing and a multiplexer has no stream of
events to repaint. A press on a view tab, a period pill, refresh or help acts
at once and lights the control until the button is released; the wheel
scrolls. The sequences are parsed here rather than by curses, whose mouse
support depends on the terminfo entry. Both modes are switched off again on
every exit path, including SIGTERM and SIGHUP.
"""

from __future__ import annotations

import curses
import os
import signal
import sys
import threading
import time

from . import data, snapshot, styles, view

KEY_TAB, KEY_ESC = 9, 27
MOUSE_ON, MOUSE_OFF = "\x1b[?1000h\x1b[?1006h", "\x1b[?1006l\x1b[?1000l"
WHEEL_STEP = 3
PRESS_LIGHT = 0.4  # seconds a pressed control stays lit when no release arrives


class Dashboard:
    def __init__(self, screen, view_index: int = 0, days: int = 30):
        self.screen = screen
        self.view, self.days, self.scroll, self.help = view_index, days, 0, False
        self.payload, self.error = data.read_payload()
        self.summaries = {}
        self.worker = None
        self.refresh_error = None
        self.done_at = None  # when the last count finished, for the short "✓ updated"
        self.dirty = True
        self.minute = int(time.time() // 60)
        self.targets = []
        self.pressed, self.pressed_at = None, 0.0
        self.attrs = {}
        self.setup_colors()
        if data.needs_refresh(self.payload):
            self.start_refresh()

    # ---- data -----------------------------------------------------------------------------------

    def start_refresh(self):
        if self.worker and self.worker.is_alive():
            return
        self.refresh_error = None

        def work():
            self.refresh_error = data.refresh()

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.dirty = True

    def poll_refresh(self):
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self.payload, self.error = data.read_payload()
            self.summaries.clear()
            self.done_at = time.time() if not self.refresh_error else None
            self.dirty = True

    def summary(self):
        if self.days not in self.summaries:
            self.summaries[self.days] = data.summarize(self.payload, self.days)
        return self.summaries[self.days]

    # ---- drawing --------------------------------------------------------------------------------

    def setup_colors(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        has = False
        try:
            has = curses.has_colors()
            if has:
                curses.start_color()
                curses.use_default_colors()
        except curses.error:
            has = False
        pair = 0
        for name, (fg, fallback, flags) in styles.STYLES.items():
            attr = curses.A_NORMAL
            if "b" in flags:
                attr |= curses.A_BOLD
            if "d" in flags:
                attr |= curses.A_DIM
            if "r" in flags:
                attr |= curses.A_REVERSE
            color = fg if has and curses.COLORS >= 256 else (styles.BASIC[fallback] if has and fallback else None)
            if color is not None and pair + 1 < curses.COLOR_PAIRS:
                try:
                    pair += 1
                    curses.init_pair(pair, color, -1)
                    attr |= curses.color_pair(pair)
                except curses.error:
                    pass
            self.attrs[name] = attr
        self.has_colors, self.pairs = has, pair

    def attr(self, style: str):
        """The curses attribute of a style; a two-colour "a/b" style gets its own colour pair on first use."""
        if style in self.attrs:
            return self.attrs[style]
        attr = self.attrs.get(style.split("/", 1)[0], curses.A_NORMAL)
        if "/" in style and self.has_colors:
            wide = curses.COLORS >= 256
            fg, fallback, _ = styles.get(style)
            bg, bg_fallback = styles.background(style)
            fg = fg if wide else (styles.BASIC[fallback] if fallback else None)
            bg = bg if wide else (styles.BASIC[bg_fallback] if bg_fallback else None)
            if fg is not None and bg is not None and self.pairs + 1 < curses.COLOR_PAIRS:
                try:
                    self.pairs += 1
                    curses.init_pair(self.pairs, fg, bg)
                    attr = (attr & ~curses.A_COLOR) | curses.color_pair(self.pairs)
                except curses.error:
                    pass
        self.attrs[style] = attr
        return attr

    def draw(self):
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        if self.payload is None:
            spin = view.SPINNER[int(time.time() * 10) % len(view.SPINNER)]
            msg = f"{spin} Counting tokens…" if self.worker else (self.refresh_error or self.error or "No data yet.")
            lines = view.loading_screen(width - 2, msg)
            if not self.worker:
                lines.append([("Press r to try again, q to close.", "dim")])
            lines = [[(" ", "")] + view.clip_line(line, width - 2) for line in lines]
        else:
            s = self.summary()
            status = view.status_text(s, bool(self.worker), self.refresh_error, done_at=self.done_at)
            lines = snapshot.frame(s, self.view, width, height, status, self.refresh_error or self.error,
                                   self.help, self.scroll)
            self.scroll = self.clamped_scroll(s, width, height)
        self.targets = []
        for y, line in enumerate(lines[:height]):
            x = 0
            for seg in line:
                text, style = seg
                if x >= width:
                    break
                text = text[:width - x - (1 if y == height - 1 else 0)]
                action = getattr(seg, "action", None)
                if action:
                    self.targets.append((y, x, x + len(text), action))
                    if action == self.pressed:
                        style = "tab_hot"
                try:
                    self.screen.addstr(y, x, text, self.attr(style))
                except curses.error:
                    pass
                x += len(text)
        self.screen.refresh()
        self.dirty = False

    def clamped_scroll(self, s, width, height):
        head, body, foot = view.screen(s, self.view, width - 2, height, "", self.refresh_error or self.error, self.help)
        room = max(0, height - len(head) - len(foot))
        return max(0, min(self.scroll, max(0, len(body) - room)))

    # ---- input ----------------------------------------------------------------------------------

    def loop(self):
        self.screen.keypad(True)
        while True:
            self.poll_refresh()
            minute = int(time.time() // 60)
            if minute != self.minute:
                self.minute = minute
                self.dirty = True
                if self.payload is not None and data.needs_refresh(self.payload) and not self.worker:
                    self.start_refresh()
            if self.pressed and time.time() - self.pressed_at > PRESS_LIGHT:
                self.pressed, self.dirty = None, True
            if self.worker:
                self.dirty = True  # the spinner moves
            if self.done_at and time.time() - self.done_at >= view.DONE_SHOWN:
                self.done_at, self.dirty = None, True
            if self.dirty:
                self.draw()
            self.screen.timeout(100 if self.pressed or self.worker else 500)
            key = self.screen.getch()
            if key == -1:
                continue
            if not self.handle(key):
                return
            self.dirty = True

    def handle(self, key) -> bool:
        height = self.screen.getmaxyx()[0]
        if key == KEY_ESC:
            seq = self.read_escape()
            if seq is not None:
                if seq:
                    self.mouse(seq)
                return True
        if key in (ord("q"), ord("Q"), KEY_ESC):
            if self.help and key == KEY_ESC:
                self.help = False
                return True
            return False
        if key == curses.KEY_RESIZE:
            return True
        if self.help:
            self.help = False
            return True
        if key in (ord("r"), ord("R")):
            self.start_refresh()
        elif key == KEY_TAB:
            self.view, self.scroll = (self.view + 1) % len(view.VIEWS), 0
        elif key == curses.KEY_BTAB:
            self.view, self.scroll = (self.view - 1) % len(view.VIEWS), 0
        elif key in map(ord, "1234"):
            self.view, self.scroll = int(chr(key)) - 1, 0
        elif key in map(ord, "789"):  # 7, 8, 9 side by side; 3 is the Projects view
            self.days = {"7": 7, "8": 30, "9": 90}[chr(key)]
        elif key == ord("d"):
            self.days = view.RANGES[(view.RANGES.index(self.days) + 1) % len(view.RANGES)]
        elif key in (curses.KEY_DOWN, ord("j")):
            self.scroll += 1
        elif key in (curses.KEY_UP, ord("k")):
            self.scroll = max(0, self.scroll - 1)
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.scroll += max(1, height - 6)
        elif key in (curses.KEY_PPAGE, ord("b")):
            self.scroll = max(0, self.scroll - max(1, height - 6))
        elif key in (ord("g"), curses.KEY_HOME):
            self.scroll = 0
        elif key in (ord("G"), curses.KEY_END):
            self.scroll = 10 ** 6
        elif key == ord("?"):
            self.help = True
        return True

    def read_escape(self):
        """After Esc: None for a lone Esc, "" for an ignored sequence, or an SGR mouse report's body."""
        self.screen.nodelay(True)
        chars = []
        try:
            while len(chars) < 32:
                ch = self.screen.getch()
                if ch == -1:
                    break
                chars.append(chr(ch) if 0 <= ch < 0x110000 else "")
                if len(chars) > 2 and chars[-1] in "Mm~ABCDHF":
                    break
        finally:
            self.screen.nodelay(False)
        if not chars:
            return None
        text = "".join(chars)
        return text[2:] if text.startswith("[<") and text[-1] in "Mm" else ""

    def mouse(self, report: str):
        """Act on one SGR report "b;x;yM" (press) or "b;x;ym" (release); x and y count from 1."""
        try:
            button, x, y = (int(n) for n in report[:-1].split(";"))
        except ValueError:
            return
        if report.endswith("m"):
            self.pressed = None
            return
        if button & 64:
            if not self.help:
                self.scroll = max(0, self.scroll + (WHEEL_STEP if button & 1 else -WHEEL_STEP))
            return
        if button & 3 != 0:
            return
        if self.help:
            self.help = False
            return
        hit = next((a for row, x0, x1, a in self.targets if row == y - 1 and x0 <= x - 1 < x1), None)
        if hit is None:
            return
        self.pressed, self.pressed_at = hit, time.time()
        if hit[0] == "view":
            self.view, self.scroll = hit[1], 0
        elif hit[0] == "days":
            self.days = hit[1]
        elif hit[0] == "refresh":
            self.start_refresh()
        elif hit[0] == "help":
            self.help = True


def mouse_modes(on: bool):
    try:
        sys.stdout.write(MOUSE_ON if on else MOUSE_OFF)
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def run(view_index: int = 0, days: int = 30):
    os.environ.setdefault("ESCDELAY", "25")

    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)

    def main(screen):
        mouse_modes(True)
        try:
            Dashboard(screen, view_index, days).loop()
        finally:
            mouse_modes(False)

    try:
        curses.wrapper(main)
    finally:
        mouse_modes(False)
