"""Token use of the last days by quarter hour, read straight from the local logs, for the 24-hour view.

ccusage reports whole days (and Claude-only billing blocks without a model split), so a rolling
24 hours needs the message timestamps. This reads them the way ccusage does, from the same files,
during the same locked refresh, and only from files changed since `since`:

  Claude Code  <config>/projects/<project>/**/*.jsonl, one line per message; a line with
               message.usage counts once per message.id + requestId (resumed sessions repeat lines).
               <config> is each entry of CLAUDE_CONFIG_DIR (comma-separated), else ~/.config/claude
               and ~/.claude.
  Codex        $CODEX_HOME/sessions/**/*.jsonl (default ~/.codex); an event_msg "token_count" line
               carries last_token_usage (else the growth of total_token_usage), the model comes from
               the latest turn_context and the project from session_meta's cwd. Codex input counts
               its cached part, which is split off as cache read, as ccusage does.

It returns tokens only: rows of [slot start (UTC epoch, a multiple of 900 s), agent, model, project,
cache_read, cache_write, input, output]. Quarter hours fit every time zone's offset, so the view can
bucket them into local hours at drawing time. Dollars come later from ccusage's own daily per-model
cost (data.summarize_hours). Lines are filtered as bytes before any JSON parsing, so a few hundred
megabytes of logs take about a second.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections import defaultdict

SLOT = 900
_TS = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')


def _epoch(value) -> float | None:
    if not isinstance(value, (str, bytes)):
        return None
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    return when.timestamp()


def _n(v) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def claude_roots() -> list:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    roots = [p.strip() for p in env.split(",") if p.strip()] if env else ["~/.config/claude", "~/.claude"]
    out = []
    for r in roots:
        path = os.path.realpath(os.path.join(os.path.expanduser(r), "projects"))
        if os.path.isdir(path) and path not in out:
            out.append(path)
    return out


def _files(root: str, since: float):
    """Every *.jsonl under root changed since `since`, without following links out of it."""
    for folder, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(folder, name)
            if not os.path.realpath(path).startswith(root + os.sep):
                continue  # a link to a file elsewhere
            try:
                if os.path.getmtime(path) >= since:
                    yield path
            except OSError:
                continue


def scan(since: float, until: float | None = None) -> list:
    """Aggregated rows for messages with since <= timestamp < until (until: no upper bound)."""
    rows = defaultdict(lambda: [0, 0, 0, 0])

    def add(ts, agent, model, project, cr, cw, i, o):
        if ts < since or (until is not None and ts >= until) or not (cr or cw or i or o):
            return
        r = rows[(int(ts // SLOT * SLOT), agent, model, project)]
        r[0] += cr
        r[1] += cw
        r[2] += i
        r[3] += o

    seen = set()
    for root in claude_roots():
        for path in _files(root, since):
            project = os.path.relpath(path, root).split(os.sep, 1)[0]
            try:
                with open(path, "rb") as f:
                    for line in f:
                        if b'"usage"' not in line:
                            continue
                        m = _TS.search(line)
                        ts = _epoch(m.group(1)) if m else None
                        if ts is None or ts < since:
                            continue
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        msg = e.get("message") if isinstance(e, dict) else None
                        u = msg.get("usage") if isinstance(msg, dict) else None
                        model = msg.get("model") if isinstance(msg, dict) else None
                        if not isinstance(u, dict) or not isinstance(model, str) or model == "<synthetic>":
                            continue
                        key = (msg.get("id"), e.get("requestId"))
                        if key[0] and key[1]:
                            if key in seen:
                                continue
                            seen.add(key)
                        add(ts, "claude", model, project, _n(u.get("cache_read_input_tokens")),
                            _n(u.get("cache_creation_input_tokens")), _n(u.get("input_tokens")),
                            _n(u.get("output_tokens")))
            except OSError:
                continue

    codex = os.path.realpath(os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                                          "sessions"))
    if os.path.isdir(codex):
        for path in _files(codex, since):
            model, project, prev = "gpt-5", "", None
            try:
                with open(path, "rb") as f:
                    for line in f:
                        if not (b'"token_count"' in line or b'"turn_context"' in line or b'"session_meta"' in line):
                            continue
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        p = e.get("payload") if isinstance(e, dict) else None
                        if not isinstance(p, dict):
                            continue
                        if e.get("type") == "session_meta":
                            cwd = p.get("cwd")
                            if isinstance(cwd, str) and cwd:
                                project = os.path.basename(cwd.rstrip("/")) or cwd
                        elif e.get("type") == "turn_context":
                            model = p.get("model") if isinstance(p.get("model"), str) else model
                        elif p.get("type") == "token_count" and isinstance(p.get("info"), dict):
                            info = p["info"]
                            total, last = info.get("total_token_usage"), info.get("last_token_usage")
                            if isinstance(total, dict):
                                if total == prev:
                                    continue  # the same count repeated
                                if not isinstance(last, dict):
                                    last = {k: _n(total.get(k)) - _n((prev or {}).get(k)) for k in total}
                                prev = total
                            if not isinstance(last, dict):
                                continue
                            ts = _epoch(e.get("timestamp"))
                            if ts is None:
                                continue
                            cached = _n(last.get("cached_input_tokens"))
                            add(ts, "codex", info.get("model") if isinstance(info.get("model"), str) else model,
                                project, cached, 0, max(0, _n(last.get("input_tokens")) - cached),
                                _n(last.get("output_tokens")))
            except OSError:
                continue
    return [[slot, agent, model, project, *t] for (slot, agent, model, project), t in sorted(rows.items())]
