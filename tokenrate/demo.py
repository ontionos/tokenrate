"""Made-up usage for `tokenrate --demo`: screenshots and a first look without any real logs.

The payload has the shape data.py caches from ccusage: 90 days of daily totals split by model,
per-project days for Claude Code, a few Codex sessions, and quarter-hour rows of the last days for
the 24-hour view (recent.py's shape). Those last days are made from the quarter hours, so the day
totals, the hours and the projects agree. Every project, number and day is invented from a fixed
seed, so the same day always draws the same dashboard; the data was "counted" at 09:41.
"""

from __future__ import annotations

import datetime as dt
import random

from . import data

PROJECTS = ("web-app", "api-server", "mobile-client", "data-pipeline", "docs-site", "infra",
            "cli-tool", "ml-experiments", "design-system", "billing", "search", "playground")
# (model, share of a day's cost, first day it is used, counted back from today)
MODELS = (("claude-opus-5-5", 0.62, 40), ("claude-opus-5", 0.55, 90), ("claude-fable-5-1", 0.16, 75),
          ("claude-sonnet-5-5", 0.08, 60), ("gpt-6-astra", 0.07, 50), ("claude-haiku-4-5", 0.02, 90),
          ("gpt-5.6-luna", 0.015, 30))


def _tokens(cost: float, rng: random.Random) -> dict:
    total = cost * rng.uniform(1.6e6, 2.2e6)
    return {"cacheReadTokens": int(total * 0.955), "cacheCreationTokens": int(total * 0.036),
            "inputTokens": int(total * 0.0008), "outputTokens": int(total * 0.0082)}


# Demo price per unit of token weight (data.TOKEN_WEIGHTS), in dollars per million.
PRICE = {"claude-opus-5-5": 2.6, "claude-fable-5-1": 6.0, "claude-sonnet-5-5": 1.5, "gpt-6-astra": 1.2,
         "claude-haiku-4-5": 0.5, "gpt-5.6-luna": 0.4}
# How busy each local hour of the day is, 0 to 1.
ACTIVITY = (0.05, 0.02, 0, 0, 0, 0, 0, 0.05, 0.35, 0.8, 0.95, 1.0, 0.6, 0.7, 0.95, 1.0, 0.9, 0.75, 0.5,
            0.45, 0.7, 0.8, 0.5, 0.2)
FETCHED = dt.time(9, 41)


def recent(today: dt.date, home: str) -> tuple[list, list, dict, list]:
    """(quarter-hour rows, daily rows, Claude project day rows, Codex sessions) for the last
    RECENT_DAYS + 1 days up to 09:41 today."""
    rng = random.Random(11)
    first = dt.datetime.combine(today - dt.timedelta(days=data.RECENT_DAYS), dt.time()).astimezone()
    end = dt.datetime.combine(today, FETCHED).astimezone().timestamp()
    rows = []
    ts = first.timestamp()
    while ts < end:
        hour = dt.datetime.fromtimestamp(ts).hour
        if rng.random() < ACTIVITY[hour]:
            for name in ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5", "gpt-6-astra",
                         "claude-haiku-4-5"):
                if name != "claude-opus-5-5" and rng.random() < 0.75:
                    continue
                total = rng.uniform(4e6, 14e6) * (1.0 if name == "claude-opus-5-5" else 0.35)
                agent = "codex" if name.startswith("gpt") else "claude"
                project = rng.choice(PROJECTS[:7])
                rows.append([int(ts), agent, name, project if agent == "codex" else f"{home}-code-{project}",
                             int(total * 0.955), int(total * 0.036) if agent == "claude" else 0,
                             int(total * 0.0008), int(total * 0.0082)])
        ts += 900
    daily, days, projects, sessions = [], {}, {}, []
    fields = ("cacheReadTokens", "cacheCreationTokens", "inputTokens", "outputTokens")
    for slot, agent, name, project, *t in rows:
        day = dt.datetime.fromtimestamp(slot).date().isoformat()
        tokens = dict(zip(fields, t))
        cost = sum(v * w for v, w in zip(t, data.TOKEN_WEIGHTS.values())) * PRICE[name] / 1e6
        for row in (days.setdefault(day, {}).setdefault(name, {"modelName": name, "cost": 0.0}),
                    (sessions_row(sessions, day, project, name) if agent == "codex" else
                     project_row(projects, project, day, name))):
            row["cost"] = row.get("cost", 0.0) + cost
            for k, v in tokens.items():
                row[k] = row.get(k, 0) + v
    for day, models in sorted(days.items()):
        breakdowns = list(models.values())
        tok = {k: sum(b.get(k, 0) for b in breakdowns) for k in fields}
        cost = sum(b["cost"] for b in breakdowns)
        codex = sum(b["cost"] for b in breakdowns if b["modelName"].startswith("gpt"))
        daily.append({"period": day, "totalCost": cost, **tok, "totalTokens": sum(tok.values()),
                      "modelBreakdowns": breakdowns,
                      "agents": [{"agent": "claude", "totalCost": cost - codex}, {"agent": "codex", "totalCost": codex}]})
    for rows_ in projects.values():
        for r in rows_:
            r["totalCost"] = r["cost"]
            r["modelBreakdowns"] = [{"modelName": r.pop("model"), "cost": r.pop("cost"),
                                     **{k: r[k] for k in fields}}]
    for sess in sessions:
        sess["totalCost"] = sess.pop("cost")
        sess["modelBreakdowns"] = [{"modelName": sess["model"], "cost": sess["totalCost"],
                                    **{k: sess.get(k, 0) for k in fields}}]
    return rows, daily, projects, sessions


def project_row(projects: dict, key: str, day: str, model: str) -> dict:
    for r in projects.setdefault(key, []):
        if r["date"] == day and r["model"] == model:
            return r
    r = {"date": day, "model": model}
    projects[key].append(r)
    return r


def sessions_row(sessions: list, day: str, project: str, model: str) -> dict:
    period = f"demo/{day}/{project}/{model}"
    for s in sessions:
        if s["period"] == period:
            return s
    s = {"agent": "codex", "period": period, "model": model, "project": project,
         "metadata": {"lastActivity": f"{day}T12:00:00Z"}}
    sessions.append(s)
    return s


def payload(today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    rng = random.Random(7)
    weights = [1 / (i + 1.3) for i in range(len(PROJECTS))]
    daily, projects, sessions = [], {}, []
    home = data.encoded_home()
    for back in range(data.HISTORY_DAYS - 1, data.RECENT_DAYS, -1):
        day = today - dt.timedelta(days=back)
        quiet = back > 60 and rng.random() < 0.55 or rng.random() < 0.12
        if quiet:
            continue
        busy = 1.0 if day.weekday() < 5 else 0.45
        ramp = 0.35 + 0.65 * (1 - back / data.HISTORY_DAYS)
        cost = rng.uniform(80, 900) * busy * ramp
        mix = {}
        for name, share, since in MODELS:
            if back >= since:
                continue
            if name == "claude-opus-5" and back < 38:
                continue  # replaced by Opus 5.5
            if name != "claude-opus-5-5" and rng.random() < 0.35:
                continue
            mix[name] = share * rng.uniform(0.5, 1.5)
        if not mix:
            mix["claude-opus-5"] = 1.0
        norm = sum(mix.values())
        breakdowns = []
        for name, w in mix.items():
            c = cost * w / norm
            breakdowns.append({"modelName": name, "cost": c, **_tokens(c, rng)})
        tok = {k: sum(b[k] for b in breakdowns) for k in ("cacheReadTokens", "cacheCreationTokens",
                                                          "inputTokens", "outputTokens")}
        daily.append({"period": day.isoformat(), "totalCost": cost, **tok, "totalTokens": sum(tok.values()),
                      "modelBreakdowns": breakdowns,
                      "agents": [{"agent": "claude", "totalCost": cost}]})
        # Spread the day's Claude models over two to four projects.
        picks = rng.choices(range(len(PROJECTS)), weights=weights, k=rng.randint(2, 4))
        for b in breakdowns:
            if b["modelName"].startswith("gpt"):
                sessions.append({"agent": "codex", "period": f"demo/{day.isoformat()}/{b['modelName']}",
                                 "totalCost": b["cost"], **{k: b[k] for k in tok},
                                 "modelBreakdowns": [b], "metadata": {"lastActivity": f"{day.isoformat()}T12:00:00Z"}})
                continue
            for i, p in enumerate(set(picks)):
                part = b["cost"] * (0.6 if i == 0 else 0.4 / max(1, len(set(picks)) - 1))
                key = f"{home}-code-{PROJECTS[p]}"
                row = {"date": day.isoformat(), "totalCost": part, **_tokens(part, rng),
                       "modelBreakdowns": [{"modelName": b["modelName"], "cost": part, **_tokens(part, rng)}]}
                projects.setdefault(key, []).append(row)
    codex = {s["period"]: rng.choice(PROJECTS[:5]) for s in sessions}
    rows, last_daily, last_projects, last_sessions = recent(today, home)
    daily += last_daily
    for key, r in last_projects.items():
        projects.setdefault(key, []).extend(r)
    for s in last_sessions:
        codex[s["period"]] = s.pop("project")
        s.pop("model")
    sessions += last_sessions
    first = dt.datetime.combine(today - dt.timedelta(days=data.RECENT_DAYS), dt.time()).astimezone()
    return {"version": data.PAYLOAD_VERSION, "fetched_at": dt.datetime.combine(today, FETCHED).timestamp(),
            "since": (today - dt.timedelta(days=data.HISTORY_DAYS - 1)).strftime("%Y%m%d"),
            "unified": {"daily": daily, "session": sessions, "totals": {}},
            "instances": {"projects": projects}, "codex_projects": codex,
            "recent": {"since": first.timestamp(), "slot": 900, "rows": rows}, "demo": True}
