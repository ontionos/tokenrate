"""Load, cache, and summarize ccusage JSON for the dashboard.

ccusage briefly needs about 1 GB of memory on a long history, so it never runs
in a loop or in parallel: one refresh holds a lock directory, runs ccusage
twice in sequence (the unified daily/session report, then the Claude
per-project daily report) under nice with --offline, and atomically replaces
one cached payload. Readers use that payload until it is older than the TTL.

The cache lives in settings.cache_dir(). ccusage is the `ccusage` setting, else the pinned copy
`tokenrate --install-ccusage` put in settings.data_dir(), else `ccusage` on PATH; settings.py owns
the `ttl` and `ccusage` settings. TOKENRATE_JSON reads that payload (or raw unified ccusage JSON)
instead of the cache and never refreshes. CODEX_HOME (default ~/.codex) is read only to name Codex
session projects.
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

from . import classifiers, settings

PAYLOAD_VERSION = 1
HISTORY_DAYS = 90
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
    if os.environ.get("TOKENRATE_JSON"):
        return False
    a = age(payload, now)
    return a is None or a > ttl()


def _lock() -> str | None:
    path = os.path.join(cache_dir(), ".refresh.lock")
    os.makedirs(cache_dir(), exist_ok=True)
    try:
        os.mkdir(path)
        return path
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > LOCK_STALE_SECONDS:
                os.rmdir(path)
                os.mkdir(path)
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
    if os.environ.get("TOKENRATE_JSON"):
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
        payload = {"version": PAYLOAD_VERSION, "fetched_at": time.time(), "since": since,
                   "unified": unified, "instances": instances,
                   "codex_projects": codex_projects(unified.get("session") or [])}
        tmp = cache_path() + f".tmp{os.getpid()}"
        with open(tmp, "w") as f:
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
    """Codex session id -> working-directory name, read from each rollout file's first line only."""
    root = os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "sessions")
    out = {}
    for s in sessions:
        period = s.get("period") if isinstance(s, dict) else None
        if s.get("agent") != "codex" or not isinstance(period, str) or ".." in period:
            continue
        try:
            with open(os.path.join(root, period + ".jsonl"), "rb") as f:
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


def summarize(payload: dict | None, days: int = 30, today: dt.date | None = None, with_classifiers: bool = True) -> dict:
    """Everything the views need for the `days` local days ending today, inclusive."""
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
        "days": days, "start": start.isoformat(), "end": today.isoformat(),
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
