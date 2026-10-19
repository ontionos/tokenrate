"""Optional usage of two cheap classifiers, Jev (text) and Clef (screenshots).

Some setups log each call of these classifiers as one JSON object per line in
jev-usage.jsonl and clef-usage.jsonl. When the `classifier_dir` setting names
the folder that holds them, the dashboard adds one line with their calls and
estimated cost; with the setting unset or neither file present it shows nothing.

A line carries "ts" (UTC, ISO 8601) and, for Jev, "input_tokens" and
"verdict"; for Clef, "input_tokens" and "unsure" (count of unsure checks).
Jev costs its input tokens at jev_input_usd_per_mtok, Clef a flat
clef_usd_per_call; settings.py owns both settings and their defaults.
"""

from __future__ import annotations

import datetime as dt
import json
import os

from . import settings

KINDS = ("jev", "clef")


def _num(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def read(folder: str, kind: str) -> list | None:
    rows = []
    try:
        with open(os.path.join(folder, f"{kind}-usage.jsonl"), encoding="utf-8") as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("ts"), str):
                    rows.append(r)
    except OSError:
        return None
    return rows


def _day(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def usage(start: dt.date, end: dt.date) -> dict | None:
    """{kind: calls, input_tokens, cost, unsure} for the days start..end, or None when there is nothing to show."""
    folder = settings.get("classifier_dir")
    if not folder:
        return None
    folder = os.path.expanduser(folder)
    jev_price = settings.get_float("jev_input_usd_per_mtok", 0.042)
    clef_price = settings.get_float("clef_usd_per_call", 0.0004)
    out = {}
    for kind in KINDS:
        rows = read(folder, kind)
        if rows is None:
            continue
        # The logs carry UTC timestamps; the window is local days, which is close enough here.
        inside = [r for r in rows if start <= (_day(r.get("ts")) or dt.date.min) <= end]
        tokens = sum(_num(r.get("input_tokens")) for r in inside)
        if kind == "jev":
            cost = tokens * jev_price / 1e6
            unsure = sum(1 for r in inside if r.get("verdict") == "unsure")
        else:
            cost = len(inside) * clef_price
            unsure = sum(_num(r.get("unsure")) for r in inside)
        out[kind] = {"calls": len(inside), "input_tokens": tokens, "cost": cost, "unsure": unsure}
    return out or None
