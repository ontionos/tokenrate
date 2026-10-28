#!/usr/bin/env bash
# tests/dashboard.test.sh - tokenrate's data layer, headless rendering and interactive screen.
#
# Drives the public interface only: --json for the summary, --snapshot for
# frames, --refresh for the ccusage cache. A fake ccusage records each call so
# the cache freshness and one-run-at-a-time behavior are observable.
set -euo pipefail

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SPEND="$ROOT/bin/tokenrate"
tmproot dashboard
export HOME="$TMP/home" CODEX_HOME="$TMP/codex" XDG_CACHE_HOME="$TMP/cache" XDG_DATA_HOME="$TMP/data" \
  XDG_CONFIG_HOME="$TMP/config"
CACHE="$XDG_CACHE_HOME/tokenrate"
mkdir -p "$HOME" "$CODEX_HOME/sessions/2026/10/18"
TODAY=2026-10-19

# Fixture: ccusage output shapes, with one day outside the 30-day window and one inside it only for 90 days.
python3 - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
def day(date, cost, model, cr, cw=0, i=0, o=0):
    mb = {"modelName": model, "cost": cost, "cacheReadTokens": cr, "cacheCreationTokens": cw,
          "inputTokens": i, "outputTokens": o}
    return {"period": date, "totalCost": cost, "cacheReadTokens": cr, "cacheCreationTokens": cw,
            "inputTokens": i, "outputTokens": o, "totalTokens": cr + cw + i + o,
            "modelBreakdowns": [mb], "agents": [{"agent": "claude", "totalCost": cost}]}
daily = [day("2026-08-01", 500.0, "claude-opus-5", 1000),          # outside 30 days, inside 90
         day("2026-10-01", 10.0, "claude-sonnet-5-5", 900, 50, 25, 25),
         day("2026-10-08", 20.0, "claude-opus-5-5", 1800, 100, 50, 50),
         day("2026-10-15", 40.0, "claude-opus-5-5", 3600, 200, 100, 100),
         day("2026-10-19", 30.0, "gpt-6-astra", 2000, 0, 500, 500)]
session = [{"agent": "codex", "period": "2026/10/18/rollout-x-1", "totalCost": 30.0, "cacheReadTokens": 2000,
            "cacheCreationTokens": 0, "inputTokens": 500, "outputTokens": 500,
            "metadata": {"lastActivity": "2026-10-19T10:00:00Z"}}]
unified = {"daily": daily, "session": session, "totals": {}}
import re
home_key = re.sub(r"[^A-Za-z0-9]", "-", tmp + "/home")
instances = {"projects": {
    home_key + "--treehouse-shipyard-abc123-2-shipyard": [
        {"date": "2026-10-15", "totalCost": 40.0, "cacheReadTokens": 3600, "inputTokens": 100}],
    home_key + "-Development-harbor--claude-worktrees-x": [
        {"date": "2026-10-08", "totalCost": 20.0, "cacheReadTokens": 1800},
        {"date": "2026-10-01", "totalCost": 10.0, "cacheReadTokens": 900}],
    home_key + "-Development-old": [{"date": "2026-08-01", "totalCost": 500.0, "cacheReadTokens": 1000}]}}
json.dump(unified, open(f"{tmp}/unified.json", "w"))
json.dump(instances, open(f"{tmp}/instances.json", "w"))
json.dump({"daily": [], "session": [], "totals": {}}, open(f"{tmp}/empty.json", "w"))
PY
printf '%s\n' '{"type":"session_meta","payload":{"cwd":"/work/codex-dock"}}' > "$CODEX_HOME/sessions/2026/10/18/rollout-x-1.jsonl"

# Fixture for the 24-hour window, in UTC: Claude Code and Codex log lines around the window's edges,
# timed from the real clock because a refresh reads the logs of the last days. NOW is half past the
# current hour; FIRST, the window's first hour, starts 23 hours before that hour.
export TZ=UTC
NOW=$(python3 - "$TMP" "$HOME" <<'H24'
import datetime as dt, json, os, re, sys, time
tmp, home = sys.argv[1], sys.argv[2]
hour = int(time.time() // 3600 * 3600)
now, first = hour + 1800, hour - 23 * 3600
day = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()
iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
def claude(t, model, mid, i=0, o=0, cr=0, cw=0):
    return json.dumps({"timestamp": iso(t), "requestId": "req-" + mid, "type": "assistant",
                       "message": {"id": "msg-" + mid, "model": model, "usage": {
                           "input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
                           "cache_creation_input_tokens": cw}}})
key = re.sub(r"[^A-Za-z0-9]", "-", home) + "-code-alpha"
proj = f"{home}/.claude/projects/{key}"
os.makedirs(f"{proj}/s1/subagents")
# Two neighbouring hours of the window on one day, to split one day's cost between hours.
k = next(k for k in range(6, 22) if day(first + k * 3600) == day(first + (k + 1) * 3600))
lines = [claude(first - 1, "claude-opus-5-5", "a", i=1000),           # a second before: the 24 before
         claude(first, "claude-opus-5-5", "b", i=100),                # the window's first second
         claude(now - 60, "claude-opus-5-5", "e", cr=5000),           # this hour, so far
         claude(now + 600, "claude-opus-5-5", "f", i=99999),          # after now: not yet
         claude(first + 7200, "<synthetic>", "g", i=77777),           # not a model
         claude(first + k * 3600 + 60, "claude-haiku-4-5", "h", i=1000),
         claude(first + (k + 1) * 3600 + 60, "claude-haiku-4-5", "i", i=3000),
         '{"type":"user","message":{"content":"no usage here"}}', "not json"]
open(f"{proj}/s1.jsonl", "w").write("\n".join(lines) + "\n")
open(f"{proj}/s1/subagents/agent-1.jsonl", "w").write("\n".join([
    claude(first + 5 * 3600 + 600, "claude-sonnet-5-5", "d", o=10),  # a subagent, in a nested folder
    claude(first, "claude-opus-5-5", "b", i=100)]) + "\n")          # a repeated message counts once
cdx = os.environ["CODEX_HOME"] + "/sessions/2030/01/01"
os.makedirs(cdx)
tc = lambda t, total, last=None: json.dumps({"timestamp": iso(t), "type": "event_msg", "payload": {
    "type": "token_count", "info": {"total_token_usage": total, **({"last_token_usage": last} if last else {})}}})
t1 = {"input_tokens": 300, "cached_input_tokens": 100, "output_tokens": 20}
t2 = {"input_tokens": 500, "cached_input_tokens": 150, "output_tokens": 30}
open(f"{cdx}/rollout-t.jsonl", "w").write("\n".join([
    json.dumps({"timestamp": iso(first), "type": "session_meta", "payload": {"cwd": "/work/beta"}}),
    json.dumps({"timestamp": iso(first), "type": "turn_context", "payload": {"model": "gpt-6-astra"}}),
    tc(first + 7200, t1, t1), tc(first + 7260, t1, t1),               # the same count repeated
    tc(first + 7200 + 1200, t2)]) + "\n")                             # no last: the growth of the total
# ccusage's day cost per model: $0.001 per unit of token weight (input 1, output 5, cache read 0.1),
# so every hour costs exactly that, except Haiku: $8 for its two lines (weights 1000 and 3000) of
# one day, so $2 and $6.
w = {"claude-opus-5-5": [(first - 1, 1000), (first, 100), (now - 60, 500)],
     "claude-sonnet-5-5": [(first + 5 * 3600 + 600, 50)], "gpt-6-astra": [(first + 7200, 15 + 350 + 150)]}
days = {}
for model, parts in w.items():
    for t, weight in parts:
        days.setdefault(day(t), {}).setdefault(model, 0.0)
        days[day(t)][model] += weight * 0.001
days.setdefault(day(first + k * 3600), {})["claude-haiku-4-5"] = 8.0
u = json.load(open(f"{tmp}/unified.json"))
for d, models in sorted(days.items()):
    u["daily"].append({"period": d, "totalCost": sum(models.values()), "modelBreakdowns": [
        {"modelName": m, "cost": c} for m, c in models.items()]})
json.dump(u, open(f"{tmp}/unified.json", "w"))
json.dump({"now": now, "first": first, "k": k}, open(f"{tmp}/h24.json", "w"))
print(dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat())
H24
)

# Fake ccusage: logs its arguments and prints the fixture for the requested report.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/ccusage" <<SH
#!/bin/sh
echo "\$*" >> "$TMP/ccusage.calls"
case "\$1" in
  claude) cat "$TMP/instances.json" ;;
  *) cat "$TMP/unified.json" ;;
esac
SH
chmod +x "$TMP/bin/ccusage"
export TOKENRATE_CCUSAGE="$TMP/bin/ccusage"

calls() { [ -f "$TMP/ccusage.calls" ] && wc -l < "$TMP/ccusage.calls" | tr -d ' ' || echo 0; }

# --- refresh builds the cache with both reports, offline -------------------
"$SPEND" --refresh || fail "refresh exits 0"
[ "$(calls)" = 2 ] || fail "refresh runs ccusage exactly twice (got $(calls))"
grep -q -- "--offline" "$TMP/ccusage.calls" || fail "ccusage runs with --offline"
if grep -v -- "--offline" "$TMP/ccusage.calls" | grep -q .; then fail "every ccusage call is offline"; fi
[ -s "$CACHE/ccusage.json" ] || fail "cache file written"
[ ! -e "$CACHE/.refresh.lock" ] || fail "refresh lock released"
pass "refresh runs the two offline reports once and writes the cache"

# --- a fresh cache is reused, a stale one is refreshed once ---------------
"$SPEND" --json --days 30 --today "$TODAY" > "$TMP/s30.json"
[ "$(calls)" = 2 ] || fail "fresh cache is reused without running ccusage"
TOKENRATE_TTL=0 "$SPEND" --json --days 30 --today "$TODAY" > /dev/null
[ "$(calls)" = 4 ] || fail "stale cache triggers exactly one refresh (got $(calls))"
mkdir "$CACHE/.refresh.lock"
TOKENRATE_TTL=0 "$SPEND" --json --today "$TODAY" > /dev/null
[ "$(calls)" = 4 ] || fail "a held refresh lock prevents a second concurrent ccusage run"
rmdir "$CACHE/.refresh.lock"
pass "cache freshness and single-run lock"

# --- the 30-day window -----------------------------------------------------
python3 - "$TMP/s30.json" <<'PY' || fail "30-day summary"
import json, sys
s = json.load(open(sys.argv[1]))["summary"]
assert s["start"] == "2026-09-20" and s["end"] == "2026-10-19", (s["start"], s["end"])
assert len(s["daily"]) == 30 and s["daily"][0]["date"] == "2026-09-20"
assert abs(s["cost"] - 100.0) < 1e-9, s["cost"]                  # 2026-08-01 is outside the window
assert s["active_days"] == 4
assert s["tokens"] == sum(s["token_types"].values())
assert s["token_types"]["cache_read"] == 900 + 1800 + 3600 + 2000
assert [m["name"] for m in s["models"]] == ["Opus 5.5", "GPT-6 Astra", "Sonnet 5.5"], s["models"]
assert abs(s["models"][0]["share"] - 0.6) < 1e-9
assert s["busiest"][0]["date"] == "2026-10-15" and s["busiest"][0]["weekday"] == "Thu"
assert s["delta"]["last7"]["cost"] == 70.0 and s["delta"]["prev7"]["cost"] == 20.0, s["delta"]
names = {p["name"]: p["cost"] for p in s["projects"]}
assert names == {"shipyard": 40.0, "harbor": 30.0, "codex-dock": 30.0}, names
assert s["classifiers"] is None
PY
"$SPEND" --json --days 90 --today "$TODAY" | python3 -c '
import json, sys
s = json.load(sys.stdin)["summary"]
assert len(s["daily"]) == 90 and abs(s["cost"] - 600.0) < 1e-9, s["cost"]
assert any(p["name"] == "old" for p in s["projects"])
' || fail "90-day window includes the older day"
"$SPEND" --json --days 7 --today "$TODAY" | python3 -c '
import json, sys
s = json.load(sys.stdin)["summary"]
assert s["cost"] == 70.0 and s["delta"]["prev7"]["cost"] == 20.0, s["delta"]
' || fail "7-day window compares with the 7 days before it"
pass "7, 30 and 90 day windows"

# --- the 24-hour window: edges, hour buckets, local time ------------------
"$SPEND" --json --days 24h --now "$NOW" > "$TMP/h24-utc.json"
python3 - "$TMP/h24-utc.json" "$TMP/h24.json" <<'H24' || fail "24-hour summary"
import json, sys
s = json.load(open(sys.argv[1]))["summary"]
f = json.load(open(sys.argv[2]))
first, k = f["first"], f["k"]
hours = s["daily"]
assert s["unit"] == "hour" and len(hours) == 24, (s["unit"], len(hours))
assert [h["start"] for h in hours] == [first + i * 3600 for i in range(24)], "24 clock hours ending with this one"
assert hours[0]["hour"].endswith(":00") and hours[-1]["start"] <= f["now"] < hours[-1]["start"] + 3600
by = lambda i: hours[i]
assert by(0)["tokens"] == 100 and abs(by(0)["cost"] - 0.1) < 1e-9, by(0)         # the first second counts, once
assert by(2)["tokens"] == 530 and by(2)["cache_read"] == 150 and by(2)["input"] == 350, by(2)  # Codex: cached split off
assert by(5)["tokens"] == 10 and abs(by(5)["cost"] - 0.05) < 1e-9, by(5)          # the nested subagent file
assert abs(by(k)["cost"] - 2.0) < 1e-9 and abs(by(k + 1)["cost"] - 6.0) < 1e-9, (by(k), by(k + 1))
assert by(23)["tokens"] == 5000 and abs(by(23)["cost"] - 0.5) < 1e-9, by(23)       # this hour; after now is not counted
assert s["tokens"] == 100 + 530 + 10 + 4000 + 5000, s["tokens"]
assert s["active_hours"] == 6
assert s["delta"]["prev24"]["tokens"] == 1000, s["delta"]                          # the second before the window
names = {m["name"] for m in s["models"]}
assert names == {"Opus 5.5", "Haiku 4.5", "GPT-6 Astra", "Sonnet 5.5"}, names
projects = {p["name"]: p for p in s["projects"]}
assert set(projects) == {"alpha", "beta"}, projects
assert projects["beta"]["agents"] == ["codex"] and projects["beta"]["daily"][2] > 0
assert s["busiest"][0]["hour"] == by(k + 1)["hour"]
H24
# The same logs in India (UTC+5:30): local hours start at half past in UTC, so the window starts half
# an hour later and the window's first line falls into the 24 hours before it.
TZ=Asia/Kolkata "$SPEND" --json --days 24h --now "$NOW" | python3 -c '
import json, sys
s = json.load(sys.stdin)["summary"]
assert all(h["start"] % 3600 == 1800 and h["hour"].endswith(":00") for h in s["daily"]), s["daily"][0]
assert s["tokens"] == 100 + 530 + 10 + 4000 + 5000 - 100, s["tokens"]
assert s["delta"]["prev24"]["tokens"] == 1100, s["delta"]
' || fail "local hours in a half-hour time zone"
"$SPEND" --snapshot --format plain --width 100 --height 48 --now "$NOW" > "$TMP/h24.txt"
grep -q "COST PER HOUR" "$TMP/h24.txt" || fail "the 24-hour overview charts hours"
grep -q "last 24 hours · 6 active" "$TMP/h24.txt" || fail "the header names the 24 hours"
grep -q "BUSIEST HOURS" "$TMP/h24.txt" || fail "busiest hours, not days"
grep -q "last 24 hours .* the 24 before" "$TMP/h24.txt" || fail "the change line compares 24 hours"
grep -q "24h .* 7d .* 30d .* 90d" "$TMP/h24.txt" || fail "24h is the first period pill"
"$SPEND" --snapshot --format plain --width 100 --height 60 --view days --now "$NOW" | grep -q "EVERY HOUR" ||
  fail "the Days view lists hours"
mkdir -p "$TMP/logs24"
python3 - "$TMP/logs24/jev-usage.jsonl" "$TMP/h24.json" <<'H24'
import datetime as dt, json, sys
f = json.load(open(sys.argv[2]))
iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
open(sys.argv[1], "w").write("\n".join(json.dumps({"ts": iso(t), "mode": "yesno", "input_tokens": 10})
                                       for t in (f["first"] - 60, f["first"] + 60, f["now"] - 60)) + "\n")
H24
TOKENRATE_CLASSIFIER_DIR="$TMP/logs24" "$SPEND" --json --now "$NOW" | python3 -c '
import json, sys
assert json.load(sys.stdin)["summary"]["classifiers"]["jev"]["calls"] == 2
' || fail "classifier calls counted by timestamp in the 24 hours"
pass "24-hour window: edges, hour buckets, Codex deltas, the day cost split by hour, local time"

# --- cheap classifier logs appear only when present ------------------------
mkdir -p "$TMP/logs"
TOKENRATE_CLASSIFIER_DIR="$TMP/logs" "$SPEND" --json --days 30 --today "$TODAY" | python3 -c '
import json, sys
assert json.load(sys.stdin)["summary"]["classifiers"] is None
' || fail "a classifier folder without logs shows nothing"
printf '%s\n' '{"ts":"2026-10-18T09:00:00Z","mode":"yesno","model":"jev","input_tokens":1000,"output_tokens":3,"verdict":"unsure"}' \
  '{"ts":"2026-07-01T09:00:00Z","mode":"yesno","model":"jev","input_tokens":5,"output_tokens":1,"verdict":"yes"}' \
  > "$TMP/logs/jev-usage.jsonl"
TOKENRATE_CLASSIFIER_DIR="$TMP/logs" "$SPEND" --json --days 30 --today "$TODAY" | python3 -c '
import json, sys
c = json.load(sys.stdin)["summary"]["classifiers"]
assert c["jev"]["calls"] == 1 and c["jev"]["unsure"] == 1 and "clef" not in c, c
' || fail "Jev usage counted inside the window only"
printf 'classifier_dir = %s\n' "$TMP/logs" > "$TMP/tokenrate.conf"
TOKENRATE_CONFIG="$TMP/tokenrate.conf" "$SPEND" --snapshot --format plain --width 120 --height 60 --days 30 --today "$TODAY" |
  grep -q "CHEAP CLASSIFIERS" ||
  fail "classifier line shown when a log exists"
"$SPEND" --snapshot --format plain --width 120 --height 60 --days 30 --today "$TODAY" | grep -q "CHEAP CLASSIFIERS" &&
  fail "no classifier line without the setting"
rm "$TMP/logs/jev-usage.jsonl"
pass "classifier usage, from the setting or the config file, silent otherwise"

# --- frames fit 80, 120 and 200 columns in every view ----------------------
for w in 50 72 80 120 200; do
  for v in overview models projects days; do
    for d in 24h 30; do
      out=$("$SPEND" --snapshot --format plain --width "$w" --height 40 --view "$v" --days "$d" --today "$TODAY" \
        --now "$NOW")
      [ "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" = 40 ] || fail "$v $d at $w has exactly 40 rows"
      long=$(printf '%s\n' "$out" | python3 -c 'import sys; w = int(sys.argv[1]); print(sum(len(l.rstrip("\n")) > w for l in sys.stdin))' "$w")
      [ "$long" = 0 ] || fail "$v $d at $w columns has $long lines wider than the terminal"
    done
  done
done
# The overview fits itself to the window: a split-pane width (one column) and a short window still
# show it whole, down to the last line, and spare rows make the chart taller instead of staying empty.
for size in 100x48 120x30 80x45 72x40; do
  for d in 24h 30; do
    "$SPEND" --snapshot --format plain --width "${size%x*}" --height "${size#*x}" --days "$d" --today "$TODAY" \
      --now "$NOW" | grep -q "WHAT CHANGED" || fail "the whole $d overview fits $size without scrolling"
  done
done
bars() { "$SPEND" --snapshot --format plain --width 120 --height "$1" --days 30 --today "$TODAY" |
  sed -n '/COST PER DAY/,/^ *$/p' | grep -c '[▁▂▃▄▅▆▇█]'; }
[ "$(bars 70)" -gt "$(bars 40)" ] || fail "a taller window draws a taller chart ($(bars 70) vs $(bars 40) rows)"
# Bars never touch: list bars are half-height (no full or left-eighth blocks outside the daily
# chart), the daily chart keeps a gap between bars even when 90 days must share 2-cell columns,
# and a sparkline is one bar per period with a space between, an empty period a dim baseline.
for v in projects models; do
  "$SPEND" --snapshot --format plain --width 120 --height 40 --view "$v" --days 90 --today "$TODAY" |
    grep -q '[█▏▎▍▌▋▊▉]' && fail "$v view draws full-height list bars that touch the next row"
done
"$SPEND" --snapshot --format plain --width 120 --height 48 --days 90 --today "$TODAY" > "$TMP/ov90.txt"
sed -n '/COST PER DAY/,/└/p' "$TMP/ov90.txt" | grep -q '[▁▂▃▄▅▆▇█][▁▂▃▄▅▆▇█]' &&
  fail "90-day chart bars touch each other"
grep -q "each bar = 2 days" "$TMP/ov90.txt" || fail "grouped chart says how many days a bar holds"
"$SPEND" --snapshot --format plain --width 160 --height 40 --view projects --days 30 --today "$TODAY" > "$TMP/proj.txt"
grep -q "tokens  per day" "$TMP/proj.txt" || fail "the sparkline column is titled"
grep -q "each row to its own busiest period" "$TMP/proj.txt" || fail "the sparkline scale is explained"
grep -q "20 Sep .* 19 Oct" "$TMP/proj.txt" || fail "the sparkline column shows its first and last day"
python3 - "$TMP/proj.txt" <<'PYC' || fail "sparklines: a space between bars, a baseline on empty days"
import re, sys
rows = [l.rstrip() for l in open(sys.argv[1]) if re.search(r"%\s+\$\d", l)]
assert rows, "no project rows"
for l in rows:
    spark = l.split("  ")[-1].strip()
    assert re.fullmatch(r"([▁▂▃▄▅▆] )*[▁▂▃▄▅▆]", spark), spark
    assert len(spark.split()) == 30, (len(spark.split()), spark)
assert any("▁" in l.split("  ")[-1] for l in rows), "empty days show the baseline"
PYC
# Every bar is split by model: a day used by two models shows both colours in its chart column and
# its Days row, with a legend; the ninth model and later share grey instead of reusing a colour.
python3 - "$TMP/mixed.json" <<'PYC'
import json, sys
names = ["claude-opus-5-5", "claude-fable-5-1"] + [f"model-{i}" for i in range(7)]
def mb(name, cost):
    return {"modelName": name, "cost": cost, "cacheReadTokens": 100, "cacheCreationTokens": 0,
            "inputTokens": 0, "outputTokens": 0}
day = lambda date, parts: {"period": date, "totalCost": sum(c for _n, c in parts), "cacheReadTokens": 100,
                           "cacheCreationTokens": 0, "inputTokens": 0, "outputTokens": 0,
                           "modelBreakdowns": [mb(n, c) for n, c in parts]}
daily = [day("2026-10-18", [("claude-opus-5-5", 60.0), ("claude-fable-5-1", 40.0)]),
         day("2026-10-19", [(n, 10.0 - i) for i, n in enumerate(names[2:])])]
json.dump({"daily": daily, "session": [], "totals": {}}, open(sys.argv[1], "w"))
PYC
TOKENRATE_JSON="$TMP/mixed.json" "$SPEND" --snapshot --format ansi --width 120 --height 60 --view days --days 30 \
  --today "$TODAY" > "$TMP/mixed.ansi"
TOKENRATE_JSON="$TMP/mixed.json" "$SPEND" --json --days 30 --today "$TODAY" > "$TMP/mixed-summary.json"
python3 - "$TMP/mixed.ansi" "$TMP/mixed-summary.json" <<'PYC' || fail "bars are split by model, with a legend and grey for the rest"
import json, re, sys
text = open(sys.argv[1]).read()
models = [m["name"] for m in json.load(open(sys.argv[2]))["summary"]["models"]]
lines = text.splitlines()
chart = "\n".join(lines[lines.index(next(l for l in lines if "COST PER DAY" in l)):][:14])
opus, fable = "38;5;33", "38;5;170"   # m0 and m1: the two biggest models of the window
assert models[:2] == ["Opus 5.5", "Fable 5.1"], models
assert opus in chart and fable in chart, "the two-model day shows both colours in the chart"
row = next(l for l in lines if "Sun 18 Oct" in l and "▄" in l)
assert opus in row and fable in row, "the Days row of that day is split too"
legend = next(l for l in lines if "● " in l and "Opus 5.5" in l)
assert "Fable 5.1" in legend and "other" in legend, legend
assert "38;5;244" in next(l for l in lines if "Mon 19 Oct" in l and "▄" in l), "the ninth model is grey"
assert "38;5;33" not in next(l for l in lines if "Mon 19 Oct" in l and "▄" in l), "no model after the eighth reuses blue"
PYC
"$SPEND" --snapshot --format plain --width 120 --height 40 --today "$TODAY" | grep -q "API-equivalent estimate" ||
  fail "header labels the dollars as an estimate"
"$SPEND" --snapshot --format plain --width 40 --height 10 | grep -q "too small" || fail "tiny terminal message"
"$SPEND" --snapshot --format svg --width 120 --height 40 --today "$TODAY" | grep -q "^<svg" || fail "svg frame"
pass "frames fit every width and view"

# --- empty and broken data -------------------------------------------------
TOKENRATE_JSON="$TMP/empty.json" "$SPEND" --json --days 30 --today "$TODAY" | python3 -c '
import json, sys
s = json.load(sys.stdin)["summary"]
assert s["has_data"] is False and s["cost"] == 0 and s["models"] == [] and len(s["daily"]) == 30
' || fail "empty data summarizes to zeros"
TOKENRATE_JSON="$TMP/empty.json" "$SPEND" --json | python3 -c '
import json, sys
s = json.load(sys.stdin)["summary"]
assert s["unit"] == "hour" and s["has_data"] is False and s["cost"] == 0 and len(s["daily"]) == 24
' || fail "empty data summarizes to 24 empty hours"
TOKENRATE_JSON="$TMP/empty.json" "$SPEND" --snapshot --format plain --width 100 --height 20 | grep -q "No token usage" ||
  fail "empty data shows a clear message"
printf 'not json' > "$TMP/broken.json"
TOKENRATE_JSON="$TMP/broken.json" "$SPEND" --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["summary"]["has_data"] is False and "unreadable" in d["error"], d["error"]
' || fail "broken data reports an error instead of crashing"
pass "empty and broken data"

# --- a missing ccusage keeps the old numbers and says why ------------------
before=$(calls)
TOKENRATE_TTL=0 TOKENRATE_CCUSAGE="$TMP/bin/absent" "$SPEND" --snapshot --format plain --width 120 --height 40 \
  --days 30 --today "$TODAY" > "$TMP/missing.txt"
grep -q "ccusage not found" "$TMP/missing.txt" || fail "missing ccusage is reported"
grep -qF "\$100" "$TMP/missing.txt" || fail "cached numbers still shown"
[ "$(calls)" = "$before" ] || fail "no ccusage run when it is missing"
pass "missing ccusage"

# --- the curses screen: keys, real SGR mouse clicks and wheel, modes released on exit
python3 - "$SPEND" <<'PY' || fail "interactive screen"
import os, pty, re, select, signal, struct, sys, termios, fcntl, time
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execv(sys.argv[1], [sys.argv[1]])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
buf = b""
def pump(t):
    global buf
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                buf += os.read(fd, 65536)
            except OSError:
                return
def since(mark):
    return buf[mark:]
def click(x, y):  # 1-based cell, as a terminal reports it: press then release
    os.write(fd, f"\x1b[<0;{x};{y}M".encode())
    pump(0.15)
    os.write(fd, f"\x1b[<0;{x};{y}m".encode())
    pump(0.3)
pump(1.0)
on = buf.find(b"\x1b[?1000h")
assert on >= 0 and b"\x1b[?1006h" in buf, "button presses requested in SGR encoding"
assert b"COST PER HOUR" in buf and b"last 24 hours" in buf, "it opens on the last 24 hours"

# Header row 1 at 120 columns: " ◆ tokenrate  1 Overview   2 Models   3 Projects   4 Days ... 24h  7d  30d  90d".
mark = len(buf); click(30, 1)                       # "2 Models"
assert b"PER MODEL" in since(mark), "clicking the Models tab opens it"
mark = len(buf); click(42, 1)                       # "3 Projects"
assert b"estimate per working folder" in since(mark), "clicking the Projects tab opens it"
import datetime as dt
start = (dt.date.today() - dt.timedelta(days=89)).strftime("%b").encode()
mark = len(buf); click(116, 1)                      # "90d"
assert start in since(mark), "clicking 90d switches the period"
mark = len(buf); click(3, 1)                        # the title is not a control
assert not since(mark), "a click off any control changes nothing on screen"
mark = len(buf); click(56, 1)                       # "4 Days"
assert b"EVERY DAY" in since(mark), "clicking the Days tab opens it"
mark = len(buf)
for _ in range(10):
    os.write(fd, b"\x1b[<65;60;20M")               # wheel down
    pump(0.05)
pump(0.3)
assert since(mark), "wheel down redraws the scrolled view"
mark = len(buf)
for _ in range(20):
    os.write(fd, b"\x1b[<64;60;20M")               # wheel up, back to the top
    pump(0.05)
pump(0.3)
assert b"COST PER DAY" in since(mark), "wheel up scrolls back to the top"
# Period keys 6, 7, 8 and 9 sit side by side; 3 stays the Projects view and leaves the period alone.
# curses sends only the cells that changed, so each check first forces a full repaint by resizing.
rows = [40]
def press(key):
    os.write(fd, key); pump(0.4)
    rows[0] = 41 if rows[0] == 40 else 40
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows[0], 120, 0, 0))
    os.kill(pid, signal.SIGWINCH)
    mark = len(buf); pump(0.6)
    return since(mark)
assert b"last 30 days" in press(b"8"), "8 shows the last 30 days"
assert b"last 7 days" in press(b"7"), "7 shows the last 7 days"
shown = press(b"3")
assert b"estimate per working folder" in shown and b"last 7 days" in shown, "3 is the Projects view, period kept"
assert b"last 90 days" in press(b"9"), "9 shows the last 90 days"
assert b"last 24 hours" in press(b"6"), "6 shows the last 24 hours"
assert b"last 7 days" in press(b"d"), "d cycles on from 24 hours to 7 days"
for k in (b"1", b"3", b"\t", b"j", b"?", b"x", b"?", b"\x1b"):  # a lone Esc closes help, not the screen
    os.write(fd, k)
    pump(0.2)
os.write(fd, b"q")
pump(1.0)
done, status = os.waitpid(pid, os.WNOHANG)
assert done == pid and status == 0, "q exits cleanly"
last_on, last_off = buf.rfind(b"\x1b[?1000h"), buf.rfind(b"\x1b[?1000l")
assert last_off > last_on and buf.rfind(b"\x1b[?1006l") > buf.rfind(b"\x1b[?1006h"), "mouse modes released on exit"
modes = set(re.findall(rb"\x1b\[\?(\d+)[hl]", buf))
bad = modes & {b"1002", b"1003", b"1005", b"1007", b"1015"}
assert not bad, f"motion, alternate-scroll or legacy mouse modes requested: {bad}"
PY
pass "interactive screen: keys and mouse clicks, wheel, no motion tracking, modes released on q"

# --- a killed dashboard releases the mouse too -----------------------------
python3 - "$SPEND" <<'PY' || fail "SIGTERM releases mouse modes"
import os, pty, select, signal, struct, sys, termios, fcntl, time
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execv(sys.argv[1], [sys.argv[1]])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
buf = b""
def pump(t):
    global buf
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                buf += os.read(fd, 65536)
            except OSError:
                return
pump(1.0)
assert b"\x1b[?1000h" in buf
os.kill(pid, signal.SIGTERM)
pump(1.0)
os.waitpid(pid, 0)
assert buf.rfind(b"\x1b[?1000l") > buf.rfind(b"\x1b[?1000h"), "SIGTERM releases mouse reporting"
PY
pass "SIGTERM releases mouse modes"

# --- refresh shows that it is working: a moving bright spinner, then a green tick
printf '#!/bin/sh\nsleep 1.5\nexec "%s" "$@"\n' "$TMP/bin/ccusage" > "$TMP/bin/slow-ccusage"
chmod +x "$TMP/bin/slow-ccusage"
TOKENRATE_CCUSAGE="$TMP/bin/slow-ccusage" TOKENRATE_TTL=100000 python3 - "$SPEND" <<'PY' || fail "refresh feedback"
import os, pty, select, struct, sys, termios, fcntl, time
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execv(sys.argv[1], [sys.argv[1]])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
buf = b""
def pump(t):
    global buf
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                buf += os.read(fd, 65536)
            except OSError:
                return
pump(1.0)
assert "counting".encode() not in buf, "a fresh cache does not count on open"
mark = len(buf); os.write(fd, b"r"); pump(1.0)
busy = buf[mark:].decode("utf-8", "replace")
assert "counting" in busy, "the header says it is counting"
frames = {ch for ch in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if ch in busy}
assert len(frames) >= 3, f"the spinner moves ({frames})"
assert "\x1b[38;5;208m" in busy or ";38;5;208m" in busy, "the spinner is bright, not dim"
mark = len(buf); pump(3.5)
assert "✓".encode() in buf[mark:], "a finished count shows a tick"
os.write(fd, b"q"); pump(0.5)
PY
pass "refresh shows a moving bright spinner while counting, then a tick"

# --- --demo draws made-up data and never runs ccusage or reads the cache ----
before=$(calls)
TOKENRATE_TTL=0 "$SPEND" --demo --json --days 30 --today "$TODAY" | python3 -c '
import json, sys
s = json.load(sys.stdin)["summary"]
names = {p["name"] for p in s["projects"]}
assert s["has_data"] and s["cost"] > 0 and "web-app" in names, (s["cost"], names)
assert len(s["models"]) >= 3
' || fail "demo summary"
"$SPEND" --demo --json --today "$TODAY" > "$TMP/demo24.json"
"$SPEND" --demo --json --days 7 --today "$TODAY" > "$TMP/demo7.json"
python3 - "$TMP/demo24.json" "$TMP/demo7.json" <<'H24' || fail "demo 24 hours"
import json, sys
h, w = (json.load(open(p))["summary"] for p in sys.argv[1:])
assert h["unit"] == "hour" and h["has_data"] and h["active_hours"] >= 8 and len(h["models"]) >= 3, h["active_hours"]
assert h["end"].startswith("2026-10-19T09:41"), h["end"]                 # the demo was "counted" at 09:41
assert {p["name"] for p in h["projects"]} <= {"web-app", "api-server", "mobile-client", "data-pipeline",
                                             "docs-site", "infra", "cli-tool"}, h["projects"]
today = h["daily"][-1]["date"]
hours_today = sum(x["cost"] for x in h["daily"] if x["date"] == today)
assert abs(hours_today - w["daily"][-1]["cost"]) < 1e-6, (hours_today, w["daily"][-1]["cost"])  # hours add up to the day
H24
[ "$(calls)" = "$before" ] || fail "demo never runs ccusage"
a=$("$SPEND" --demo --snapshot --format plain --today "$TODAY"); b=$("$SPEND" --demo --snapshot --format plain --today "$TODAY")
[ "$a" = "$b" ] || fail "demo is the same for the same day"
pass "--demo shows made-up data without ccusage"

# --- the cache is private, and Codex rollout lookups stay inside the sessions folder
python3 - "$CACHE" <<'PYC' || fail "cache folder 0700 and cache file 0600"
import os, stat, sys
d = sys.argv[1]
assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, oct(os.stat(d).st_mode)
assert stat.S_IMODE(os.stat(os.path.join(d, "ccusage.json")).st_mode) == 0o600
PYC
mkdir -p "$TMP/outside" "$CODEX_HOME/sessions/link"
printf '%s\n' '{"type":"session_meta","payload":{"cwd":"/private/leaked-name"}}' > "$TMP/outside/rollout.jsonl"
ln -s "$TMP/outside/rollout.jsonl" "$CODEX_HOME/sessions/link/rollout.jsonl"
python3 - "$TMP" <<'PYC'
import json, sys
tmp = sys.argv[1]
s = lambda period: {"agent": "codex", "period": period, "totalCost": 1.0, "cacheReadTokens": 1,
                    "cacheCreationTokens": 0, "inputTokens": 0, "outputTokens": 0,
                    "metadata": {"lastActivity": "2026-10-19T10:00:00Z"}}
u = json.load(open(f"{tmp}/unified.json"))
u["session"] += [s(f"{tmp}/outside/rollout"), s("../../outside/rollout"), s("link/rollout")]
json.dump(u, open(f"{tmp}/unified-escape.json", "w"))
PYC
sed "s#$TMP/unified.json#$TMP/unified-escape.json#" "$TMP/bin/ccusage" > "$TMP/bin/ccusage-escape"
chmod +x "$TMP/bin/ccusage-escape"
python3 - "$TMP/outside/rollout.jsonl" "$TMP/h24.json" <<'H24'
import datetime as dt, json, sys
t = json.load(open(sys.argv[2]))["first"] + 3600
open(sys.argv[1], "a").write(json.dumps({"timestamp": dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(),
    "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 5}}}}) + "\n")
H24
XDG_CACHE_HOME="$TMP/cache-escape" TOKENRATE_CCUSAGE="$TMP/bin/ccusage-escape" "$SPEND" --json --days 30 --today "$TODAY" |
  python3 -c '
import json, sys
names = {p["name"] for p in json.load(sys.stdin)["summary"]["projects"]}
assert "leaked-name" not in names, names
assert "codex-dock" in names, names
' || fail "absolute, climbing and linked Codex session ids are not followed"
XDG_CACHE_HOME="$TMP/cache-escape" "$SPEND" --json --no-refresh --now "$NOW" | python3 -c '
import json, sys
names = {p["name"] for p in json.load(sys.stdin)["summary"]["projects"]}
assert "leaked-name" not in names and "beta" in names, names
' || fail "the 24-hour scan does not follow a linked rollout out of the sessions folder"
pass "the cache is private and Codex lookups stay inside the sessions folder"
