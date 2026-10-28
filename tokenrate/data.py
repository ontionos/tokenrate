"""Load, cache, and summarize ccusage JSON for the dashboard.

ccusage briefly needs about 1 GB of memory on a long history, so it never runs
in a loop or in parallel: one refresh holds a lock directory, runs ccusage
twice in sequence (the unified daily/session report, then the Claude
per-project daily report) under nice with --offline, then reads the last
days' message timestamps from the same logs (recent.py) for the 24-hour view,
and atomically replaces one cached payload. Readers use that payload until it
is older than the TTL.

The cache lives in settings.cache_dir(). ccusage is the `ccusage` setting, else the pinned copy
`tokenrate --install-ccusage` put in settings.data_dir(), else `ccusage` on PATH; settings.py owns
the `ttl` and `ccusage` settings. TOKENRATE_JSON reads that payload (or raw unified ccusage JSON)
instead of the cache and never refreshes; TOKENRATE_DEMO (set by --demo) shows demo.py's made-up data. CODEX_HOME (default ~/.codex) is read to name Codex
session projects and, with CLAUDE_CONFIG_DIR, by recent.py for the last days' hourly tokens.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict

from . import classifiers, recent, settings

PAYLOAD_VERSION = 1
HISTORY_DAYS = 90
HOURS = 24
# The log scan starts at local midnight this many days back: the last 48 hours (the 24-hour window
# and the 24 before it) plus whole days, since a day's ccusage cost is what prices its hours.
RECENT_DAYS = 2
# Relative price of the token types, the same for every Claude model and close for GPT; it only
# splits one model's ccusage cost for one day between that day's hours, so the day total stays exact.
TOKEN_WEIGHTS = {"cache_read": 0.1, "cache_write": 1.25, "input": 1.0, "output": 5.0}
DEFAULT_TTL = 300
LOCK_STALE_SECONDS = 600
CCUSAGE_TIMEOUT = 240


def cache_dir() -> str:
    return settings.cache_dir()


def ccusage_dir() -> str:
    return os.path.join(settings.data_dir(), "ccusage")


def cache_path() -> str:
    return os.path.join(cache_dir(), "ccusage.json")


def ttl() -> int:
    return settings.get_int("ttl", DEFAULT_TTL)


def ccusage_bin() -> str | None:
    explicit = settings.get("ccusage")
    if explicit:
        explicit = os.path.expanduser(explicit)
        return explicit if os.access(explicit, os.X_OK) else None
    local = os.path.join(ccusage_dir(), "node_modules", ".bin", "ccusage")
    if os.access(local, os.X_OK):
        return local
    return shutil.which("ccusage")


# ---- cache --------------------------------------------------------------------------------------


def read_payload() -> tuple[dict | None, str | None]:
    """The payload to show and an error to surface, without ever running ccusage."""
    if os.environ.get("TOKENRATE_DEMO"):
        from . import demo
        when = os.environ["TOKENRATE_DEMO"]
        return demo.payload(dt.date.fromisoformat(when) if len(when) == 10 else None), None
    fixture = os.environ.get("TOKENRATE_JSON")
    path = fixture or cache_path()
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None, (f"no data file at {path}" if fixture else None)
    except (OSError, ValueError) as e:
        return None, f"unreadable data file {path}: {e}"
    if not isinstance(raw, dict):
        return None, f"unexpected data in {path}"
    if "unified" not in raw:  # raw `ccusage daily --sections ... --json` output
        raw = {"version": PAYLOAD_VERSION, "fetched_at": os.path.getmtime(path), "unified": raw}
    return raw, None


def age(payload: dict | None, now: float | None = None) -> float | None:
    if not payload or not isinstance(payload.get("fetched_at"), (int, float)):
        return None
    return max(0.0, (now or time.time()) - payload["fetched_at"])


def needs_refresh(payload: dict | None, now: float | None = None) -> bool:
    if os.environ.get("TOKENRATE_JSON") or os.environ.get("TOKENRATE_DEMO"):
        return False
    a = age(payload, now)
    return a is None or a > ttl() or "recent" not in payload  # a cache from before the 24-hour view


def private_dir(path: str) -> str:
    """Create `path` (and parents) and keep it readable by its owner only: usage history is private."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _lock() -> str | None:
    path = os.path.join(private_dir(cache_dir()), ".refresh.lock")
    try:
        os.mkdir(path, 0o700)
        return path
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > LOCK_STALE_SECONDS:
                os.rmdir(path)
                os.mkdir(path, 0o700)
                return path
        except OSError:
            pass
        return None


def _run(cmd: list[str]) -> dict:
    nice = shutil.which("nice")
    full = ([nice, "-n", "15"] if nice else []) + cmd
    env = dict(os.environ, NO_COLOR="1")
    proc = subprocess.run(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          env=env, timeout=CCUSAGE_TIMEOUT, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"ccusage exited {proc.returncode}: {tail[0]}")
    return json.loads(proc.stdout)


def refresh() -> str | None:
    """Run ccusage once (two sequential reports) and replace the cache; returns an error or None."""
    if os.environ.get("TOKENRATE_JSON") or os.environ.get("TOKENRATE_DEMO"):
        return None
    exe = ccusage_bin()
    if not exe:
        return "ccusage not found: run `tokenrate --install-ccusage` or set TOKENRATE_CCUSAGE"
    lock = _lock()
    if not lock:
        return None  # another refresh is running; its result lands in the same cache
    try:
        since = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS - 1)).strftime("%Y%m%d")
        unified = _run([exe, "daily", "--json", "--offline", "--since", since, "--sections", "daily,session",
                        "--by-agent"])
        try:
            instances = _run([exe, "claude", "daily", "--json", "--offline", "--instances", "--since", since])
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            instances = None  # projects then fall back to sessions only
        midnight = dt.datetime.combine(dt.date.today() - dt.timedelta(days=RECENT_DAYS), dt.time())
        recent_since = midnight.astimezone().timestamp()
        payload = {"version": PAYLOAD_VERSION, "fetched_at": time.time(), "since": since,
                   "unified": unified, "instances": instances,
                   "codex_projects": codex_projects(unified.get("session") or []),
                   "recent": {"since": recent_since, "slot": recent.SLOT, "rows": recent.scan(recent_since)}}
        tmp = cache_path() + f".tmp{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(payload, f, separators=(",", ":"))
        os.replace(tmp, cache_path())
        return None
    except subprocess.TimeoutExpired:
        return f"ccusage took longer than {CCUSAGE_TIMEOUT}s"
    except (OSError, RuntimeError, ValueError) as e:
        return str(e)
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def codex_projects(sessions: list) -> dict:
    """Codex session id -> working-directory name, read from each rollout file's first line only.
    Only files inside the Codex sessions folder are opened: a session id that is absolute, climbs out
    with "..", or resolves outside through a link is skipped."""
    root = os.path.realpath(os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                                         "sessions"))
    out = {}
    for s in sessions:
        period = s.get("period") if isinstance(s, dict) else None
        if s.get("agent") != "codex" or not isinstance(period, str) or not period or os.path.isabs(period):
            continue
        path = os.path.realpath(os.path.join(root, period + ".jsonl"))
        if not path.startswith(root + os.sep):
            continue
        try:
            with open(path, "rb") as f:
                first = json.loads(f.readline(4 << 20) or b"{}")
            cwd = (first.get("payload") or {}).get("cwd")
            if isinstance(cwd, str) and cwd:
                out[period] = os.path.basename(cwd.rstrip("/")) or cwd
        except (OSError, ValueError, AttributeError):
            continue
    return out


# ---- names --------------------------------------------------------------------------------------


def model_label(name: str) -> str:
    """claude-opus-5-5 -> Opus 5.5, gpt-6-astra -> GPT-6 Astra, claude-haiku-4-5-20251001 -> Haiku 4.5."""
    n = re.sub(r"-\d{8}$", "", str(name or "?"))
    n = re.sub(r"^(claude|anthropic)[-/]", "", n)
    n = re.sub(r"(?<=\d)-(?=\d)", ".", n)
    parts = []
    for p in n.split("-"):
        if p.lower() in ("gpt", "glm", "o1", "o3", "o4") or re.fullmatch(r"gpt\.?\d.*", p.lower()):
            parts.append(p.upper())
        elif p[:1].isalpha():
            parts.append(p[:1].upper() + p[1:])
        else:
            parts.append(p)
    label = " ".join(parts)
    return re.sub(r"^GPT (\d)", r"GPT-\1", label)


def encoded_home() -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser("~"))


def project_label(key: str, enc_home: str | None = None) -> str:
    """Readable project name from a Claude projects-directory key (the cwd with non-alphanumerics as '-')."""
    enc_home = encoded_home() if enc_home is None else enc_home
    k = str(key or "")
    if k in ("", enc_home):
        return "~ (home)"
    if k.startswith(enc_home + "-"):
        k = k[len(enc_home) + 1:]
    elif k.startswith(("-private-", "-tmp", "-var-")):
        return "temp / scratch"
    if k.startswith("-no-mistakes-"):
        return "no-mistakes reviews"
    m = re.match(r"^-treehouse-(.+?)-[0-9a-f]{6}-\d+-", k + "-")
    if m:
        return m.group(1)
    m = re.match(r"^orca-workspaces-([^-]+)-", k)
    if m:
        return m.group(1)
    k = re.sub(r"--claude-worktrees-.*$", "", k)
    k = re.sub(r"^(Development|Projects|projects|src|code|dev|work)-", "", k)
    return k.strip("-") or "~ (home)"


# ---- summary ------------------------------------------------------------------------------------

TOKEN_FIELDS = (("cache_read", "cacheReadTokens"), ("cache_write", "cacheCreationTokens"),
                ("input", "inputTokens"), ("output", "outputTokens"))


def _num(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _tokens(row: dict) -> dict:
    return {k: _num(row.get(src)) for k, src in TOKEN_FIELDS}


def _day(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def summarize(payload: dict | None, days: int = 30, today: dt.date | None = None, with_classifiers: bool = True,
              now: float | None = None) -> dict:
    """Everything the views need for the `days` local days ending today, inclusive; days=1 is the
    rolling last 24 hours ending `now` instead (summarize_hours)."""
    if days == 1:
        return summarize_hours(payload, now, with_classifiers)
    today = today or dt.date.today()
    start = today - dt.timedelta(days=days - 1)
    unified = (payload or {}).get("unified") or {}
    daily_rows = [r for r in unified.get("daily") or [] if isinstance(r, dict)]

    per_day = {}
    for i in range(days):
        d = start + dt.timedelta(days=i)
        per_day[d] = {"date": d.isoformat(), "weekday": d.strftime("%a"), "cost": 0.0, "tokens": 0,
                      **{k: 0 for k, _ in TOKEN_FIELDS}, "models": defaultdict(float)}
    models = defaultdict(lambda: {"cost": 0.0, "tokens": 0, **{k: 0 for k, _ in TOKEN_FIELDS},
                                  "daily": defaultdict(float)})
    agents = defaultdict(float)
    history_first = None
    for r in daily_rows:
        d = _day(r.get("period") or r.get("date"))
        if d is None:
            continue
        history_first = d if history_first is None or d < history_first else history_first
        if d not in per_day:
            continue
        slot = per_day[d]
        slot["cost"] += _num(r.get("totalCost"))
        for k, v in _tokens(r).items():
            slot[k] += v
            slot["tokens"] += v
        for mb in r.get("modelBreakdowns") or []:
            name = model_label(mb.get("modelName"))
            m = models[name]
            c = _num(mb.get("cost"))
            m["cost"] += c
            m["daily"][d.isoformat()] += c
            slot["models"][name] += c
            for k, v in _tokens(mb).items():
                m[k] += v
                m["tokens"] += v
        for a in r.get("agents") or []:
            agents[str(a.get("agent") or "?")] += _num(a.get("totalCost"))

    day_list = []
    for d in sorted(per_day):
        slot = per_day[d]
        slot["models"] = dict(sorted(slot["models"].items(), key=lambda kv: -kv[1]))
        slot["top_model"] = next(iter(slot["models"]), None)
        day_list.append(slot)

    total_cost = sum(s["cost"] for s in day_list)
    totals = {k: sum(s[k] for s in day_list) for k, _ in TOKEN_FIELDS}
    total_tokens = sum(totals.values())

    model_list = []
    for name, m in models.items():
        model_list.append({"name": name, "cost": m["cost"], "tokens": m["tokens"],
                           "share": m["cost"] / total_cost if total_cost else 0.0,
                           **{k: m[k] for k, _ in TOKEN_FIELDS},
                           "daily": [m["daily"].get(s["date"], 0.0) for s in day_list]})
    model_list.sort(key=lambda m: (-m["cost"], -m["tokens"]))

    def week(offset: int) -> dict:
        part = day_list[max(0, len(day_list) - 7 * (offset + 1)):len(day_list) - 7 * offset]
        return {"cost": sum(s["cost"] for s in part), "tokens": sum(s["tokens"] for s in part), "days": len(part)}

    last7 = week(0)
    if days >= 14:
        prev7 = week(1)
    else:
        # A short window compares with the 7 days before it, which lie outside the window itself.
        prev7 = summarize(payload, days=14, today=today, with_classifiers=False)["delta"]["prev7"]

    active = [s for s in day_list if s["cost"] or s["tokens"]]
    busiest = sorted(active, key=lambda s: (-s["cost"], -s["tokens"]))[:7]
    peak = busiest[0]["cost"] if busiest else 0.0

    return {
        "days": days, "unit": "day", "start": start.isoformat(), "end": today.isoformat(),
        "fetched_at": (payload or {}).get("fetched_at"),
        "history_first": history_first.isoformat() if history_first else None,
        "has_data": bool(active),
        "cost": total_cost, "tokens": total_tokens, "token_types": totals,
        "active_days": len(active), "avg_active_day_cost": total_cost / len(active) if active else 0.0,
        "peak_cost": peak,
        "daily": [{k: v for k, v in s.items()} for s in day_list],
        "models": model_list,
        "agents": dict(sorted(agents.items(), key=lambda kv: -kv[1])),
        "projects": _projects(payload, start, today, total_cost, [s["date"] for s in day_list]),
        "busiest": [{"date": s["date"], "weekday": s["weekday"], "cost": s["cost"], "tokens": s["tokens"],
                     "top_model": s["top_model"]} for s in busiest],
        "delta": {"last7": last7, "prev7": prev7},
        "classifiers": classifiers.usage(start, today) if with_classifiers else None,
    }


def _projects(payload: dict | None, start: dt.date, end: dt.date, total_cost: float, dates: list) -> list:
    """Cost per project: Claude per project per day from the instances report, Codex and others by session."""
    payload = payload or {}
    groups = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "daily": defaultdict(float), "agents": set(),
                                  "models": defaultdict(float), "daily_models": defaultdict(lambda: defaultdict(float))})

    def add_models(g, row, day):
        """Split a row's cost by model; a row without a breakdown counts as one unnamed model."""
        parts = [(model_label(mb.get("modelName")), _num(mb.get("cost"))) for mb in row.get("modelBreakdowns") or []]
        if not parts:
            parts = [("?", _num(row.get("totalCost")))]
        for name, c in parts:
            g["models"][name] += c
            g["daily_models"][day][name] += c
    enc_home = encoded_home()
    instances = (payload.get("instances") or {}).get("projects") or {}
    have_claude = bool(instances)
    for key, rows in instances.items():
        label = project_label(key, enc_home)
        for r in rows or []:
            d = _day(r.get("date") or r.get("period"))
            if d is None or not start <= d <= end:
                continue
            g = groups[label]
            g["cost"] += _num(r.get("totalCost"))
            g["tokens"] += sum(_tokens(r).values())
            g["daily"][d.isoformat()] += _num(r.get("totalCost"))
            g["agents"].add("claude")
            add_models(g, r, d.isoformat())
    codex = payload.get("codex_projects") or {}
    for s in ((payload.get("unified") or {}).get("session") or []):
        if not isinstance(s, dict) or (have_claude and s.get("agent") == "claude"):
            continue
        d = _day((s.get("metadata") or {}).get("lastActivity"))
        if d is None or not start <= d <= end:
            continue
        agent = str(s.get("agent") or "?")
        label = codex.get(s.get("period")) if agent == "codex" else None
        label = label or (project_label(s.get("period"), enc_home) if agent == "claude" else f"{agent} sessions")
        g = groups[label]
        g["cost"] += _num(s.get("totalCost"))
        g["tokens"] += sum(_tokens(s).values())
        g["daily"][d.isoformat()] += _num(s.get("totalCost"))
        g["agents"].add(agent)
        add_models(g, s, d.isoformat())
    out = [{"name": name, "cost": g["cost"], "tokens": g["tokens"],
            "share": g["cost"] / total_cost if total_cost else 0.0, "agents": sorted(g["agents"]),
            "daily": [g["daily"].get(d, 0.0) for d in dates],
            "models": dict(sorted(g["models"].items(), key=lambda kv: -kv[1])),
            "daily_models": [dict(g["daily_models"].get(d, {})) for d in dates]}
           for name, g in groups.items() if g["cost"] or g["tokens"]]
    out.sort(key=lambda p: (-p["cost"], -p["tokens"]))
    return out


def hour_start(ts: float) -> float:
    """Start of the local clock hour holding `ts`, as an epoch (right across DST and half-hour zones)."""
    local = dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone()
    return local.replace(minute=0, second=0, microsecond=0).timestamp()


def _weight(t: dict) -> float:
    return sum(t[k] * w for k, w in TOKEN_WEIGHTS.items())


def summarize_hours(payload: dict | None, now: float | None = None, with_classifiers: bool = True) -> dict:
    """The views' summary for the 24 local clock hours ending with the current one (in progress at `now`).

    Tokens are the logs' own (recent.py). A model's cost in an hour is its ccusage cost for that day
    split by the hour's share of the model's token weight that day; a model ccusage did not price
    that day takes the average price of the priced ones. The same structure as summarize(), with
    hours in "daily", plus the 24 hours before for the comparison."""
    payload = payload or {}
    if now is None:
        # Made-up data was "counted" at its fetch time, so it always draws the same 24 hours.
        now = payload["fetched_at"] if payload.get("demo") else time.time()
    first = hour_start(now) - (HOURS - 1) * 3600
    slots = [first + k * 3600 for k in range(HOURS)]
    prev_first = first - HOURS * 3600
    rec = payload.get("recent") or {}
    rows = [r for r in rec.get("rows") or [] if isinstance(r, list) and len(r) == 8]

    def local(ts: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone()

    # ccusage's cost per (local day, raw model name), and the scanned weight of the same.
    day_cost = defaultdict(float)
    for r in ((payload.get("unified") or {}).get("daily") or []):
        d = _day(r.get("period") or r.get("date")) if isinstance(r, dict) else None
        for mb in (r.get("modelBreakdowns") or []) if d else []:
            day_cost[(d, str(mb.get("modelName")))] += _num(mb.get("cost"))
    parsed = []
    day_weight = defaultdict(float)
    for slot, agent, model, project, *t in rows:
        if not isinstance(slot, (int, float)) or slot >= now:
            continue
        tokens = {k: _num(v) for (k, _src), v in zip(TOKEN_FIELDS, t)}
        d = local(slot).date()
        day_weight[(d, str(model))] += _weight(tokens)
        parsed.append((slot, str(agent), str(model), str(project), tokens, d))
    priced = [(k, day_cost[k]) for k in day_weight if day_cost.get(k)]
    rate = (sum(c for _k, c in priced) / sum(day_weight[k] for k, _c in priced)) if priced else 0.0

    def cost(tokens, d, model):
        w = _weight(tokens)
        key = (d, model)
        return day_cost[key] * w / day_weight[key] if day_cost.get(key) and day_weight[key] else w * rate

    per_hour = {}
    for s in slots:
        when = local(s)
        per_hour[s] = {"date": when.date().isoformat(), "hour": when.strftime("%H:%M"), "start": s,
                       "weekday": when.strftime("%a"), "cost": 0.0, "tokens": 0,
                       **{k: 0 for k, _ in TOKEN_FIELDS}, "models": defaultdict(float)}
    models = defaultdict(lambda: {"cost": 0.0, "tokens": 0, **{k: 0 for k, _ in TOKEN_FIELDS},
                                  "daily": defaultdict(float)})
    agents = defaultdict(float)
    enc_home = encoded_home()
    groups = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "daily": defaultdict(float), "agents": set(),
                                  "models": defaultdict(float), "daily_models": defaultdict(lambda: defaultdict(float))})
    prev = {"cost": 0.0, "tokens": 0, "hours": HOURS}
    for slot, agent, model, project, tokens, d in parsed:
        c, n = cost(tokens, d, model), sum(tokens.values())
        if prev_first <= slot < first:
            prev["cost"] += c
            prev["tokens"] += n
            continue
        if slot < first:
            continue
        h = hour_start(slot)
        if h not in per_hour:
            continue
        name = model_label(model)
        hour = per_hour[h]
        hour["cost"] += c
        hour["tokens"] += n
        hour["models"][name] += c
        m = models[name]
        m["cost"] += c
        m["tokens"] += n
        m["daily"][h] += c
        for k, v in tokens.items():
            hour[k] += v
            m[k] += v
        agents[agent] += c
        if agent == "claude":
            label = project_label(project, enc_home)
        else:
            label = project or f"{agent} sessions"
        g = groups[label]
        g["cost"] += c
        g["tokens"] += n
        g["daily"][h] += c
        g["agents"].add(agent)
        g["models"][name] += c
        g["daily_models"][h][name] += c

    hour_list = []
    for s in slots:
        slot = per_hour[s]
        slot["models"] = dict(sorted(slot["models"].items(), key=lambda kv: -kv[1]))
        slot["top_model"] = next(iter(slot["models"]), None)
        hour_list.append(slot)
    total_cost = sum(h["cost"] for h in hour_list)
    totals = {k: sum(h[k] for h in hour_list) for k, _ in TOKEN_FIELDS}
    model_list = [{"name": name, "cost": m["cost"], "tokens": m["tokens"],
                   "share": m["cost"] / total_cost if total_cost else 0.0,
                   **{k: m[k] for k, _ in TOKEN_FIELDS}, "daily": [m["daily"].get(s, 0.0) for s in slots]}
                  for name, m in models.items()]
    model_list.sort(key=lambda m: (-m["cost"], -m["tokens"]))
    projects = [{"name": name, "cost": g["cost"], "tokens": g["tokens"],
                 "share": g["cost"] / total_cost if total_cost else 0.0, "agents": sorted(g["agents"]),
                 "daily": [g["daily"].get(s, 0.0) for s in slots],
                 "models": dict(sorted(g["models"].items(), key=lambda kv: -kv[1])),
                 "daily_models": [dict(g["daily_models"].get(s, {})) for s in slots]}
                for name, g in groups.items() if g["cost"] or g["tokens"]]
    projects.sort(key=lambda p: (-p["cost"], -p["tokens"]))
    active = [h for h in hour_list if h["cost"] or h["tokens"]]
    busiest = sorted(active, key=lambda h: (-h["cost"], -h["tokens"]))[:7]
    history = [d for d in (_day(r.get("period") or r.get("date")) for r in
                           ((payload.get("unified") or {}).get("daily") or []) if isinstance(r, dict)) if d]
    since = rec.get("since")
    return {
        "days": 1, "unit": "hour", "hours": HOURS,
        "start": local(first).isoformat(timespec="minutes"), "end": local(now).isoformat(timespec="minutes"),
        "fetched_at": payload.get("fetched_at"),
        "history_first": min(history).isoformat() if history else None,
        "has_data": bool(active),
        "cost": total_cost, "tokens": sum(totals.values()), "token_types": totals,
        "active_hours": len(active), "avg_active_hour_cost": total_cost / len(active) if active else 0.0,
        "peak_cost": busiest[0]["cost"] if busiest else 0.0,
        "daily": [dict(h) for h in hour_list],
        "models": model_list,
        "agents": dict(sorted(agents.items(), key=lambda kv: -kv[1])),
        "projects": projects,
        "busiest": [{"date": h["date"], "hour": h["hour"], "start": h["start"], "weekday": h["weekday"],
                     "cost": h["cost"], "tokens": h["tokens"], "top_model": h["top_model"]} for h in busiest],
        "delta": {"last24": {"cost": total_cost, "tokens": sum(totals.values()), "hours": HOURS},
                  # only when the scan reached back to the start of the 24 hours before
                  "prev24": prev if isinstance(since, (int, float)) and since <= prev_first else None},
        "classifiers": classifiers.usage(local(first).date(), local(now).date(), since=first, until=now)
                       if with_classifiers else None,
    }
