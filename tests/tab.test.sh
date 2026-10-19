#!/usr/bin/env bash
# tests/tab.test.sh - `tokenrate tab` against a fake Herdr.
#
# The fake keeps workspaces, ordered tabs, panes and focus in one JSON file,
# answers the CLI calls the helper makes, and serves the socket API's tab.move
# on a private Unix socket, so every case below is observable as state. It
# never reaches a real Herdr: TOKENRATE_HERDR points at the fake and the
# ambient Herdr variables are cleared.
set -euo pipefail

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

tmproot tab
TAB="$TMP/tab"
printf '#!/bin/sh\nexec "%s" tab "$@"\n' "$ROOT/bin/tokenrate" > "$TAB"
chmod +x "$TAB"
STATE_JSON="$TMP/herdr.json"
CALLS="$TMP/calls"
SOCK=$(mktemp -u "${TMPDIR:-/tmp}/trt.XXXXXX")
unset HERDR_BIN_PATH HERDR_SOCKET_PATH HERDR_ENV HERDR_PANE_ID HERDR_TAB_ID HERDR_WORKSPACE_ID HERDR_SESSION
for v in $(env | sed -n 's/^\(TOKENRATE_[A-Z_]*\)=.*/\1/p'); do unset "$v"; done
export XDG_CONFIG_HOME="$TMP/config" XDG_CACHE_HOME="$TMP/cache" XDG_DATA_HOME="$TMP/data"
export TOKENRATE_HERDR="$TMP/herdr" TOKENRATE_TAB_WAIT=1 TOKENRATE_TAB_WORKSPACE=work FAKE_STATE="$STATE_JSON" FAKE_CALLS="$CALLS"

cat > "$TMP/fake_herdr.py" <<'PY'
import json, os, sys

STATE = os.environ["FAKE_STATE"]
DASH = " ◆ tokenrate  1 Overview   2 Models   3 Projects   4 Days      7d   30d   90d\n 1.0B tokens"


def load():
    return json.load(open(STATE))


def save(s):
    json.dump(s, open(STATE, "w"), indent=1)


def out(result):
    print(json.dumps({"id": "cli", "result": result}))


def fail(msg):
    sys.stderr.write(json.dumps({"error": {"code": "x", "message": msg}}) + "\n")
    sys.exit(1)


def tab_view(s, t):
    return {"tab_id": t["tab_id"], "workspace_id": t["ws"], "label": t["label"],
            "focused": t["tab_id"] == s["focused_tab"], "pane_count": sum(p["tab_id"] == t["tab_id"] for p in s["panes"])}


def pane_view(s, p):
    tab = next(t for t in s["tabs"] if t["tab_id"] == p["tab_id"])
    view = {"pane_id": p["pane_id"], "tab_id": p["tab_id"], "workspace_id": tab["ws"]}
    if p.get("label"):
        view["label"] = p["label"]
    return view


def new_id(s, kind):
    s["next"] += 1
    return f"w1:{kind}{s['next']}"


def close_pane(s, pid):
    pane = next((p for p in s["panes"] if p["pane_id"] == pid), None)
    if not pane:
        fail("pane not found")
    s["panes"].remove(pane)
    if not any(p["tab_id"] == pane["tab_id"] for p in s["panes"]):
        s["tabs"] = [t for t in s["tabs"] if t["tab_id"] != pane["tab_id"]]


def opt(args, name):
    return args[args.index(name) + 1] if name in args else None


def move(s, tab_id, index):
    tab = next(t for t in s["tabs"] if t["tab_id"] == tab_id)
    s["tabs"].remove(tab)
    s["tabs"].insert(index, tab)


def main(args):
    s = load()
    cmd = " ".join(args[:2])
    if cmd == "workspace list":
        return out({"workspaces": [{"workspace_id": w["id"], "label": w["label"]} for w in s["workspaces"]]})
    if cmd == "status server":
        print(f"status: running\nsocket: {s['socket']}")
        return
    if cmd == "api snapshot":
        return out({"snapshot": {"focused_tab_id": s["focused_tab"]}})
    if cmd == "plugin list":
        return out({"plugins": [{"plugin_id": "tokenrate", "enabled": True}] if s["plugin"] else []})
    if cmd == "tab list":
        ws = opt(args, "--workspace")
        return out({"tabs": [tab_view(s, t) for t in s["tabs"] if t["ws"] == ws]})
    if cmd == "tab rename":
        next(t for t in s["tabs"] if t["tab_id"] == args[2])["label"] = args[3]
        save(s)
        return out({"tab": {}})
    if cmd == "tab close":
        s["panes"] = [p for p in s["panes"] if p["tab_id"] != args[2]]
        s["tabs"] = [t for t in s["tabs"] if t["tab_id"] != args[2]]
        save(s)
        return out({})
    if cmd == "tab focus":
        s["focused_tab"] = args[2]
        save(s)
        return out({})
    if cmd == "pane list":
        ws = opt(args, "--workspace")
        tabs = {t["tab_id"] for t in s["tabs"] if t["ws"] == ws}
        return out({"panes": [pane_view(s, p) for p in s["panes"] if p["tab_id"] in tabs]})
    if cmd == "pane get":
        pane = next((p for p in s["panes"] if p["pane_id"] == args[2]), None)
        if not pane:
            fail("pane not found")
        return out({"pane": pane_view(s, pane)})
    if cmd == "pane process-info":
        pane = next(p for p in s["panes"] if p["pane_id"] == opt(args, "--pane"))
        argv = ["/usr/bin/python3", "-B", "-s", "-m", "tokenrate"] if pane.get("dash") else ["-zsh"]
        return out({"process_info": {"foreground_processes": [{"argv": argv}]}})
    if cmd == "pane read":
        pane = next(p for p in s["panes"] if p["pane_id"] == args[2])
        print(pane.get("screen", "$ "))
        return
    if cmd == "pane close":
        close_pane(s, args[2])
        save(s)
        return out({})
    if args[:3] == ["plugin", "pane", "open"]:
        if "--focus" in args or "--no-focus" not in args:
            fail("test fake: ensure must open with --no-focus")
        screen = os.environ.get("FAKE_NEW_SCREEN", DASH)
        if opt(args, "--placement") == "tab":
            tab_id = new_id(s, "t")
            s["tabs"].append({"tab_id": tab_id, "ws": opt(args, "--workspace"), "label": str(s["next"])})
        else:
            tab_id = next(p["tab_id"] for p in s["panes"] if p["pane_id"] == opt(args, "--target-pane"))
        pane = {"pane_id": new_id(s, "p"), "tab_id": tab_id, "label": "tokenrate", "dash": True, "screen": screen}
        s["panes"].append(pane)
        save(s)
        return out({"plugin_pane": {"pane": pane_view(s, pane)}})
    if args[:1] == ["serve"]:
        import socket
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(s["socket"])
        srv.listen(4)
        while True:
            conn, _ = srv.accept()
            req = json.loads(conn.makefile().readline())
            st = load()
            if req["method"] == "tab.move":
                move(st, req["params"]["tab_id"], req["params"]["insert_index"])
                save(st)
                with open(os.environ["FAKE_CALLS"], "a") as f:
                    f.write("api tab.move\n")
                conn.sendall((json.dumps({"id": req["id"], "result": {}}) + "\n").encode())
            else:
                conn.sendall((json.dumps({"id": req["id"], "error": {"message": "unknown"}}) + "\n").encode())
            conn.close()
    fail("test fake: unexpected call " + " ".join(args))


with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
main(sys.argv[1:])
PY
printf '#!/bin/sh\nexec python3 "%s" "$@"\n' "$TMP/fake_herdr.py" > "$TMP/herdr"
chmod +x "$TMP/herdr"

# Start state: workspace work with the user's tabs main and notes, focus on notes,
# plus an unrelated workspace that also has tabs.
reset_state() {
  python3 - "$STATE_JSON" "$SOCK" <<'PY'
import json, sys
dash = " ◆ tokenrate  1 Overview   2 Models   3 Projects   4 Days      7d   30d   90d"
json.dump({"socket": sys.argv[2], "plugin": True, "next": 10, "focused_tab": "w1:t2",
           "workspaces": [{"id": "w1", "label": "work"}, {"id": "w2", "label": "other"}],
           "tabs": [{"tab_id": "w1:t1", "ws": "w1", "label": "main"}, {"tab_id": "w1:t2", "ws": "w1", "label": "notes"},
                    {"tab_id": "w2:t1", "ws": "w2", "label": "1"}],
           "panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1"}, {"pane_id": "w1:p2", "tab_id": "w1:t2"},
                     {"pane_id": "w2:p1", "tab_id": "w2:t1"}]}, open(sys.argv[1], "w"))
PY
  : > "$CALLS"
}
query() { python3 -c "import json,sys; s=json.load(open('$STATE_JSON')); $1"; }
order() { query 'print(" ".join(t["label"] for t in s["tabs"] if t["ws"] == "w1"))'; }
focus() { query 'print(s["focused_tab"])'; }
dash_panes() { query 'print(" ".join(p["pane_id"] + ("+" if p.get("dash") else "-") for p in s["panes"] if p["tab_id"] in [t["tab_id"] for t in s["tabs"] if t["label"] == "tokenrate"]))'; }

reset_state
python3 "$TMP/fake_herdr.py" serve >/dev/null 2>&1 &
SERVER=$!
trap 'kill "$SERVER" 2>/dev/null || true; rm -f "$SOCK"' EXIT
for _ in $(seq 50); do [ -S "$SOCK" ] && break; sleep 0.1; done
[ -S "$SOCK" ] || fail "fake socket API started"

# --- check on a workspace without the tab, then ensure opens it as tab 2 ---
out=$("$TAB" check) && fail "check fails while the tab is missing"
[ "$out" = "tokenrate tab: workspace work has no tokenrate tab" ] || fail "check names the missing tab (got: $out)"
out=$("$TAB" ensure) || fail "ensure succeeds (got: $out)"
case "$out" in *"ok: tokenrate is tab 2 of workspace work"*"opened the tokenrate tab"*) ;; *) fail "ensure reports what it did (got: $out)" ;; esac
[ "$(order)" = "main tokenrate notes" ] || fail "tokenrate is tab 2, the user's tabs keep their order (got: $(order))"
[ "$(focus)" = "w1:t2" ] || fail "focus stays on the user's tab"
grep -q '^api tab.move$' "$CALLS" || fail "the new tab is moved through the socket API"
query 'assert [t["label"] for t in s["tabs"] if t["ws"] == "w2"] == ["1"]' || fail "another workspace is untouched"
"$TAB" check | grep -q '^tokenrate tab: ok: tokenrate is tab 2' || fail "check passes after ensure"
pass "ensure opens the dashboard as tab 2 without moving focus or the user's tabs"

# --- ensure on a correct tab changes nothing -------------------------------
cp "$STATE_JSON" "$TMP/before.json"
: > "$CALLS"
out=$("$TAB" ensure --quiet) || fail "quiet ensure on a correct tab exits 0"
[ -z "$out" ] || fail "quiet ensure prints nothing when all is well (got: $out)"
cmp -s "$STATE_JSON" "$TMP/before.json" || fail "ensure is idempotent"
if grep -Eq '^(tab (close|rename|focus)|pane close|plugin pane open|api tab.move)' "$CALLS"; then
  fail "a correct tab draws no mutating call: $(cat "$CALLS")"
fi
pass "ensure is idempotent and quiet"

# --- the tab drifted to the end: only the tokenrate tab moves back -------------
query 's["tabs"].append(s["tabs"].pop(1)); json.dump(s, open("'"$STATE_JSON"'", "w"))'
[ "$(order)" = "main notes tokenrate" ] || fail "setup: tokenrate moved last"
"$TAB" check >/dev/null && fail "check fails when tokenrate is not tab 2"
"$TAB" ensure >/dev/null || fail "ensure repairs the position"
[ "$(order)" = "main tokenrate notes" ] || fail "tokenrate is back at tab 2 (got: $(order))"
pass "a misplaced tokenrate tab is moved back"

# --- a quit dashboard beside the user's own pane is reopened, their pane kept
query '
sp = next(t["tab_id"] for t in s["tabs"] if t["label"] == "tokenrate")
for p in s["panes"]:
    if p["tab_id"] == sp: p.update(dash=False, screen="$ ")
s["panes"].append({"pane_id": "w1:p99", "tab_id": sp})
json.dump(s, open("'"$STATE_JSON"'", "w"))'
out=$("$TAB" check) && fail "check fails when the dashboard is not running"
[ "$out" = "tokenrate tab: the tokenrate tab has no running dashboard" ] || fail "check names the dead dashboard (got: $out)"
"$TAB" ensure | grep -q 'restarted the dashboard' || fail "ensure restarts the dashboard"
panes=$(dash_panes)
case " $panes " in *" w1:p99- "*) ;; *) fail "the user's pane in the tokenrate tab is kept (got: $panes)" ;; esac
[ "$(printf '%s\n' "$panes" | tr ' ' '\n' | grep -c '+$')" = 1 ] || fail "exactly one live dashboard (got: $panes)"
[ "$(printf '%s\n' "$panes" | tr ' ' '\n' | wc -l | tr -d ' ')" = 2 ] || fail "the dead dashboard pane was closed (got: $panes)"
[ "$(order)" = "main tokenrate notes" ] || fail "tab order unchanged by the repair"
pass "a dead dashboard is replaced; the user's pane stays"

# --- the dashboard quit alone, so its tab closed: ensure recreates it --------
reset_state
"$TAB" ensure >/dev/null || fail "first ensure"
query '
sp = next(t["tab_id"] for t in s["tabs"] if t["label"] == "tokenrate")
s["tabs"] = [t for t in s["tabs"] if t["tab_id"] != sp]; s["panes"] = [p for p in s["panes"] if p["tab_id"] != sp]
json.dump(s, open("'"$STATE_JSON"'", "w"))'
"$TAB" ensure >/dev/null || fail "ensure after the tab closed"
[ "$(order)" = "main tokenrate notes" ] || fail "tokenrate recreated at tab 2 (got: $(order))"
pass "a closed tokenrate tab is recreated"

# --- duplicates: an all-dashboard duplicate closes, a mixed one is left alone
reset_state
"$TAB" ensure >/dev/null || fail "first ensure"
query '
for n, extra in ((50, False), (60, True)):
    s["tabs"].append({"tab_id": f"w1:t{n}", "ws": "w1", "label": "tokenrate"})
    s["panes"].append({"pane_id": f"w1:p{n}", "tab_id": f"w1:t{n}", "label": "tokenrate", "dash": True, "screen": "x"})
    if extra: s["panes"].append({"pane_id": f"w1:p{n + 1}", "tab_id": f"w1:t{n}"})
json.dump(s, open("'"$STATE_JSON"'", "w"))'
"$TAB" ensure | grep -q 'closed a duplicate tokenrate tab' || fail "ensure closes a duplicate made only of dashboards"
query 'assert not any(t["tab_id"] == "w1:t50" for t in s["tabs"])' || fail "all-dashboard duplicate closed"
query 'assert any(p["pane_id"] == "w1:p61" for p in s["panes"])' || fail "a duplicate holding another pane is left alone"
[ "$(order | cut -d' ' -f1-3)" = "main tokenrate notes" ] || fail "kept tokenrate tab is still tab 2 (got: $(order))"
pass "duplicate tokenrate tabs"

# --- problems print one line and exit 1 ------------------------------------
reset_state
FAKE_NEW_SCREEN='Terminal too small' "$TAB" ensure > "$TMP/out" && fail "ensure fails when the dashboard never draws"
grep -q 'is not drawing its screen' "$TMP/out" && [ "$(wc -l < "$TMP/out" | tr -d ' ')" = 1 ] ||
  fail "one line names the drawing problem (got: $(cat "$TMP/out"))"
out=$("$TAB" check) && fail "check fails while the dashboard does not draw"
[ "$out" = "tokenrate tab: the dashboard in the tokenrate tab is not drawing its screen" ] ||
  fail "check names a dashboard that does not draw (got: $out)"
reset_state
query 's["workspaces"].append({"id": "w3", "label": "work"}); json.dump(s, open("'"$STATE_JSON"'", "w"))'
cp "$STATE_JSON" "$TMP/before.json"
out=$("$TAB" ensure) && fail "two workspaces with the label are refused"
[ "$out" = "tokenrate tab: 2 herdr workspaces are labelled work; leaving them alone" ] || fail "ambiguity named (got: $out)"
cmp -s "$STATE_JSON" "$TMP/before.json" || fail "nothing changes on ambiguity"
reset_state
query 's["plugin"] = False; json.dump(s, open("'"$STATE_JSON"'", "w"))'
out=$("$TAB" ensure) && fail "a missing plugin is a problem"
case "$out" in *"herdr plugin tokenrate is not installed"*"README"*) ;; *) fail "missing plugin names the fix (got: $out)" ;; esac
pass "problems are one line with exit 1 and change nothing they should not"

# --- nothing to do: no workspace, no herdr, a secondmate home --------------
reset_state
out=$(TOKENRATE_TAB_WORKSPACE=elsewhere "$TAB" ensure) || fail "no workspace is not a problem for ensure"
[ "$out" = "tokenrate tab: nothing to do: no herdr workspace labelled elsewhere" ] || fail "skip reason printed (got: $out)"
[ -z "$(TOKENRATE_TAB_WORKSPACE=elsewhere "$TAB" ensure --quiet)" ] || fail "quiet skip prints nothing"
TOKENRATE_TAB_WORKSPACE=elsewhere "$TAB" check >/dev/null && fail "check fails without the workspace"
out=$(TOKENRATE_HERDR=/nonexistent PATH=/usr/bin:/bin HOME="$TMP/nohome" "$TAB" ensure) || fail "no herdr is not a problem"
[ "$out" = "tokenrate tab: nothing to do: herdr is not installed" ] || fail "missing herdr skip (got: $out)"
pass "nothing to do without the workspace or Herdr"

# --- the workspace and label come from options, settings or the pane it runs in
reset_state
out=$(env -u TOKENRATE_TAB_WORKSPACE "$TAB" ensure) && fail "no workspace named is a problem"
case "$out" in *"no workspace named"*) ;; *) fail "missing workspace named (got: $out)" ;; esac
env -u TOKENRATE_TAB_WORKSPACE HERDR_PANE_ID=w1:p2 "$TAB" ensure >/dev/null || fail "the pane's own workspace is used"
[ "$(order)" = "main tokenrate notes" ] || fail "opened in the pane's workspace (got: $(order))"
"$TAB" ensure --workspace other --label Usage >/dev/null || fail "--workspace and --label"
query 'assert [t["label"] for t in s["tabs"] if t["ws"] == "w2"] == ["1", "Usage"]' || fail "label and workspace from options"
mkdir -p "$XDG_CONFIG_HOME/tokenrate"
printf 'tab_workspace = other\ntab_label = Tokens\n' > "$XDG_CONFIG_HOME/tokenrate/config"
reset_state
env -u TOKENRATE_TAB_WORKSPACE "$TAB" ensure >/dev/null || fail "workspace from the config file"
query 'assert [t["label"] for t in s["tabs"] if t["ws"] == "w2"] == ["1", "Tokens"]' || fail "config names workspace and label"
rm "$XDG_CONFIG_HOME/tokenrate/config"
pass "workspace and label from options, the config file, or this pane"

# --- settings in the caller's environment reach the dashboard it opens -----
reset_state
TOKENRATE_CLASSIFIER_DIR=/some/logs "$TAB" ensure >/dev/null || fail "ensure with a setting"
grep -q -- "--env TOKENRATE_CLASSIFIER_DIR=/some/logs" "$CALLS" || fail "setting forwarded with --env"
if grep -q -- "--env TOKENRATE_HERDR=\|--env TOKENRATE_TAB_" "$CALLS"; then fail "the helper's own variables stay out"; fi
pass "TOKENRATE_* settings are forwarded to the new dashboard pane"

# --- usage ----------------------------------------------------------------
"$TAB" bogus 2>/dev/null && fail "unknown command refused"
"$TAB" check --quiet 2>/dev/null && fail "--quiet only with ensure"
"$TAB" --help | grep -q 'ensure' || fail "help"
pass "usage"
