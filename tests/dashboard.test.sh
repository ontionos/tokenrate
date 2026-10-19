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

# --- cheap classifier logs appear only when present ------------------------
mkdir -p "$TMP/logs"
TOKENRATE_CLASSIFIER_DIR="$TMP/logs" "$SPEND" --json --today "$TODAY" | python3 -c '
import json, sys
assert json.load(sys.stdin)["summary"]["classifiers"] is None
' || fail "a classifier folder without logs shows nothing"
printf '%s\n' '{"ts":"2026-10-18T09:00:00Z","mode":"yesno","model":"jev","input_tokens":1000,"output_tokens":3,"verdict":"unsure"}' \
  '{"ts":"2026-07-01T09:00:00Z","mode":"yesno","model":"jev","input_tokens":5,"output_tokens":1,"verdict":"yes"}' \
  > "$TMP/logs/jev-usage.jsonl"
TOKENRATE_CLASSIFIER_DIR="$TMP/logs" "$SPEND" --json --today "$TODAY" | python3 -c '
import json, sys
c = json.load(sys.stdin)["summary"]["classifiers"]
assert c["jev"]["calls"] == 1 and c["jev"]["unsure"] == 1 and "clef" not in c, c
' || fail "Jev usage counted inside the window only"
printf 'classifier_dir = %s\n' "$TMP/logs" > "$TMP/tokenrate.conf"
TOKENRATE_CONFIG="$TMP/tokenrate.conf" "$SPEND" --snapshot --format plain --width 120 --height 60 --today "$TODAY" |
  grep -q "CHEAP CLASSIFIERS" ||
  fail "classifier line shown when a log exists"
"$SPEND" --snapshot --format plain --width 120 --height 60 --today "$TODAY" | grep -q "CHEAP CLASSIFIERS" &&
  fail "no classifier line without the setting"
rm "$TMP/logs/jev-usage.jsonl"
pass "classifier usage, from the setting or the config file, silent otherwise"

# --- frames fit 80, 120 and 200 columns in every view ----------------------
for w in 80 120 200; do
  for v in overview models projects days; do
    out=$("$SPEND" --snapshot --format plain --width "$w" --height 40 --view "$v" --today "$TODAY")
    [ "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" = 40 ] || fail "$v at $w has exactly 40 rows"
    long=$(printf '%s\n' "$out" | python3 -c 'import sys; w = int(sys.argv[1]); print(sum(len(l.rstrip("\n")) > w for l in sys.stdin))' "$w")
    [ "$long" = 0 ] || fail "$v at $w columns has $long lines wider than the terminal"
  done
done
# The overview fits itself to the window: a split-pane width (one column) and a short window still
# show it whole, down to the last line, and spare rows make the chart taller instead of staying empty.
for size in 100x48 120x30 80x45; do
  "$SPEND" --snapshot --format plain --width "${size%x*}" --height "${size#*x}" --today "$TODAY" | grep -q "WHAT CHANGED" ||
    fail "the whole overview fits $size without scrolling"
done
bars() { "$SPEND" --snapshot --format plain --width 120 --height "$1" --today "$TODAY" |
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
"$SPEND" --snapshot --format plain --width 160 --height 40 --view projects --today "$TODAY" > "$TMP/proj.txt"
grep -q "per day, each row to its own peak" "$TMP/proj.txt" || fail "the sparkline column is titled"
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
"$SPEND" --snapshot --format plain --width 120 --height 40 --today "$TODAY" | grep -q "API-equivalent estimate" ||
  fail "header labels the dollars as an estimate"
"$SPEND" --snapshot --format plain --width 40 --height 10 | grep -q "too small" || fail "tiny terminal message"
"$SPEND" --snapshot --format svg --width 120 --height 40 --today "$TODAY" | grep -q "^<svg" || fail "svg frame"
pass "frames fit every width and view"

# --- empty and broken data -------------------------------------------------
TOKENRATE_JSON="$TMP/empty.json" "$SPEND" --json --today "$TODAY" | python3 -c '
import json, sys
s = json.load(sys.stdin)["summary"]
assert s["has_data"] is False and s["cost"] == 0 and s["models"] == [] and len(s["daily"]) == 30
' || fail "empty data summarizes to zeros"
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
  --today "$TODAY" > "$TMP/missing.txt"
grep -q "ccusage not found" "$TMP/missing.txt" || fail "missing ccusage is reported"
grep -qF "\$100" "$TMP/missing.txt" || fail "cached numbers still shown"
[ "$(calls)" = "$before" ] || fail "no ccusage run when it is missing"
pass "missing ccusage"

# --- the curses screen: keys, real SGR mouse clicks and wheel, modes released on exit
python3 - "$SPEND" <<'PY' || fail "interactive screen"
import os, pty, re, select, struct, sys, termios, fcntl, time
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
assert b"COST PER DAY" in buf

# Header row 1 at 120 columns: " ◆ tokenrate  1 Overview   2 Models   3 Projects   4 Days ... 7d   30d   90d".
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
