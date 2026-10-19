"""Made-up usage for `tokenrate --demo`: screenshots and a first look without any real logs.

The payload has the shape data.py caches from ccusage: 90 days of daily totals split by model,
per-project days for Claude Code, and a few Codex sessions. Every project, number and day is
invented from a fixed seed, so the same day always draws the same dashboard.
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


def payload(today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    rng = random.Random(7)
    weights = [1 / (i + 1.3) for i in range(len(PROJECTS))]
    daily, projects, sessions = [], {}, []
    home = data.encoded_home()
    for back in range(data.HISTORY_DAYS - 1, -1, -1):
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
    return {"version": data.PAYLOAD_VERSION, "fetched_at": dt.datetime.combine(today, dt.time(9, 41)).timestamp(),
            "since": (today - dt.timedelta(days=data.HISTORY_DAYS - 1)).strftime("%Y%m%d"),
            "unified": {"daily": daily, "session": sessions, "totals": {}},
            "instances": {"projects": projects}, "codex_projects": codex, "demo": True}
