"""Keep the dashboard open as the second tab of a Herdr workspace: `tokenrate tab`.

  ensure   make tab 2 of the workspace a tab labelled "tokenrate" (or --label) holding a live, drawing dashboard
  check    verify exactly that, without changing anything

Each prints one line. Exit 0 means the tab is right (or, for ensure, that there is nothing to do here:
no Herdr, no running server, or no such workspace); exit 1 means a problem the line names, 2 a usage
error. `ensure --quiet` prints nothing unless there is a problem.
The workspace is --workspace LABEL, else the `tab_workspace` setting, else the workspace of the Herdr
pane this runs in. The tab label is --label LABEL, else the `tab_label` setting, else tokenrate.

Only Herdr's public interfaces are used: the CLI for everything it offers, and its socket API
(`tab.move`, socket path from `herdr status server`) for the one thing it does not - moving the
tokenrate tab itself to position 2. ensure never closes, moves, renames or focuses a tab or pane that
is not the dashboard's own: it opens the dashboard with --no-focus, closes only dashboard panes that are
dead or not drawing, moves only the tokenrate tab, and gives focus back if Herdr moved it anyway.
A pane is the dashboard's when its foreground process runs the tokenrate dashboard or Herdr labels it
"tokenrate" or with the tab label. A new dashboard pane gets every TOKENRATE_* setting of the caller's environment, so a
setting given to `tokenrate tab ensure` also reaches the dashboard it opens.

Environment:
  TOKENRATE_HERDR      herdr executable (default $HERDR_BIN_PATH, then herdr on PATH or ~/.local/bin)
  TOKENRATE_TAB_WAIT   seconds to wait for a new dashboard to draw (default 10)
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

LABEL = "tokenrate"  # the tab label; --label or the tab_label setting changes it
PANE_TITLE = "tokenrate"  # the title herdr-plugin.toml gives the dashboard pane
PLUGIN, ENTRYPOINT = "tokenrate", "dashboard"
HEADER = "◆"  # the dashboard's first line starts with it, followed by "tokenrate" or the 7d/30d/90d pills
FORWARD_SKIP = ("TOKENRATE_HERDR", "TOKENRATE_TAB_WAIT", "TOKENRATE_TAB_WORKSPACE", "TOKENRATE_TAB_LABEL")
POSITION = 1  # zero-based: the second tab


class Problem(Exception):
    """Something is wrong; the message is the one line to print."""


class Skip(Exception):
    """Nothing to do in this environment; the message says why."""


# ---- herdr access -------------------------------------------------------------------------------


def herdr_bin() -> str | None:
    for cand in (os.environ.get("TOKENRATE_HERDR"), os.environ.get("HERDR_BIN_PATH")):
        if cand and os.access(cand, os.X_OK):
            return cand
    path = os.environ.get("PATH", "") + os.pathsep + os.path.expanduser("~/.local/bin")
    return shutil.which("herdr", path=path)


class Herdr:
    def __init__(self, exe: str):
        self.exe = exe

    def raw(self, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise Problem(f"herdr {' '.join(args[:2])} timed out")

    def call(self, *args: str) -> dict:
        p = self.raw(*args)
        if p.returncode != 0:
            raise Problem(f"herdr {' '.join(args[:2])} failed: {error_text(p)}")
        try:
            return json.loads(p.stdout)["result"]
        except (ValueError, KeyError, TypeError):
            raise Problem(f"herdr {' '.join(args[:2])} printed no result")

    def api(self, method: str, params: dict) -> dict:
        """One request on the socket API, for the calls the CLI does not offer."""
        p = self.raw("status", "server")
        path = next((line.split(":", 1)[1].strip() for line in p.stdout.splitlines()
                     if line.strip().startswith("socket:")), "")
        if not path:
            raise Problem("herdr status server named no socket")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(10)
                s.connect(path)
                s.sendall((json.dumps({"id": "tokenrate-tab", "method": method, "params": params}) + "\n").encode())
                reply = json.loads(s.makefile("r", encoding="utf-8").readline())
        except (OSError, ValueError) as e:
            raise Problem(f"herdr {method} failed: {e}")
        if "error" in reply:
            raise Problem(f"herdr {method} failed: {reply['error'].get('message', reply['error'])}")
        return reply.get("result", {})


def error_text(p: subprocess.CompletedProcess) -> str:
    text = (p.stderr or p.stdout or "").strip()
    try:
        err = json.loads(text)["error"]
        return err.get("message") or err.get("code") or text
    except (ValueError, KeyError, TypeError, AttributeError):
        return text.splitlines()[-1] if text else f"exit {p.returncode}"


# ---- reading the workspace ----------------------------------------------------------------------


def connect() -> Herdr:
    exe = herdr_bin()
    if not exe:
        raise Skip("herdr is not installed")
    return Herdr(exe)


def find_workspace(h: Herdr, label: str) -> str:
    p = h.raw("workspace", "list")
    if p.returncode != 0:
        raise Skip(f"herdr is not running ({error_text(p)})")
    try:
        workspaces = json.loads(p.stdout)["result"]["workspaces"]
    except (ValueError, KeyError, TypeError):
        raise Problem("herdr workspace list printed no result")
    found = [w["workspace_id"] for w in workspaces if w.get("label") == label]
    if not found:
        raise Skip(f"no herdr workspace labelled {label}")
    if len(found) > 1:
        raise Problem(f"{len(found)} herdr workspaces are labelled {label}; leaving them alone")
    return found[0]


def tabs_of(h: Herdr, ws: str) -> list:
    return h.call("tab", "list", "--workspace", ws)["tabs"]


def panes_of(h: Herdr, ws: str) -> list:
    return h.call("pane", "list", "--workspace", ws)["panes"]


def runs_dashboard(h: Herdr, pane_id: str) -> bool:
    p = h.raw("pane", "process-info", "--pane", pane_id)
    if p.returncode != 0:
        return False
    try:
        procs = json.loads(p.stdout)["result"]["process_info"]["foreground_processes"]
    except (ValueError, KeyError, TypeError):
        return False
    return any(is_dashboard_argv(proc.get("argv") or []) for proc in procs)


def is_dashboard_argv(argv: list) -> bool:
    """`python -m tokenrate [options]`, the dashboard itself, but not `python -m tokenrate tab ...`."""
    for i, arg in enumerate(argv[:-1]):
        if arg == "-m" and argv[i + 1] == "tokenrate":
            return argv[i + 2:i + 3] != ["tab"]
    return False


def renders(h: Herdr, pane_id: str) -> bool:
    p = h.raw("pane", "read", pane_id, "--source", "visible")
    if p.returncode != 0:
        return False
    first = next((line.strip() for line in p.stdout.splitlines() if line.strip()), "")
    return first.startswith(HEADER) and ("tokenrate" in first or "90d" in first)


def is_ours(h: Herdr, pane: dict) -> bool:
    return pane.get("label") in (LABEL, PANE_TITLE) or runs_dashboard(h, pane["pane_id"])


def healthy(h: Herdr, pane_id: str) -> bool:
    return runs_dashboard(h, pane_id) and renders(h, pane_id)


def focused_tab(h: Herdr) -> str | None:
    p = h.raw("api", "snapshot")
    try:
        return json.loads(p.stdout)["result"]["snapshot"].get("focused_tab_id")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


# ---- check and ensure ---------------------------------------------------------------------------


def check(h: Herdr, label: str) -> str:
    ws = find_workspace(h, label)
    tabs = tabs_of(h, ws)
    ours = [t for t in tabs if t.get("label") == LABEL]
    if not ours:
        raise Problem(f"workspace {label} has no {LABEL} tab")
    if len(tabs) > POSITION and tabs[POSITION].get("label") != LABEL:
        raise Problem(f"the {LABEL} tab is not tab {POSITION + 1} of workspace {label}")
    tab = tabs[POSITION] if len(tabs) > POSITION else ours[0]
    panes = [p for p in panes_of(h, ws) if p["tab_id"] == tab["tab_id"]]
    running = [p["pane_id"] for p in panes if runs_dashboard(h, p["pane_id"])]
    if not running:
        raise Problem(f"the {LABEL} tab has no running dashboard")
    drawn = [p for p in running if renders(h, p)]
    if not drawn:
        raise Problem(f"the dashboard in the {LABEL} tab is not drawing its screen")
    return f"ok: {LABEL} is tab {tabs.index(tab) + 1} of workspace {label}, dashboard pane {drawn[0]} is drawing"


def wait_healthy(h: Herdr, pane_id: str) -> bool:
    deadline = time.time() + float(os.environ.get("TOKENRATE_TAB_WAIT", "10"))
    while True:
        if healthy(h, pane_id):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def open_dashboard(h: Herdr, ws: str, beside: str | None = None) -> str:
    args = ["plugin", "pane", "open", "--plugin", PLUGIN, "--entrypoint", ENTRYPOINT, "--no-focus"]
    for key in sorted(os.environ):
        if key.startswith("TOKENRATE_") and key not in FORWARD_SKIP:
            args += ["--env", f"{key}={os.environ[key]}"]
    if beside:
        args += ["--placement", "split", "--target-pane", beside, "--direction", "right"]
    else:
        args += ["--placement", "tab", "--workspace", ws]
    return h.call(*args)["plugin_pane"]["pane"]["pane_id"]


def drawn(h: Herdr, pane_id: str):
    if not wait_healthy(h, pane_id):
        raise Problem(f"opened the dashboard (pane {pane_id}) but it is not drawing its screen")


def ensure(h: Herdr, label: str) -> str:
    ws = find_workspace(h, label)
    plugins = h.call("plugin", "list", "--plugin", PLUGIN, "--json").get("plugins") or []
    if not any(p.get("plugin_id") == PLUGIN and p.get("enabled", True) for p in plugins):
        raise Problem(f"herdr plugin {PLUGIN} is not installed and enabled (see the tokenrate README, Install)")
    focus = focused_tab(h)
    actions, pane_id = [], None

    tabs = tabs_of(h, ws)
    panes = panes_of(h, ws)
    by_tab = {t["tab_id"]: [p for p in panes if p["tab_id"] == t["tab_id"]] for t in tabs}
    ours = [t for t in tabs if t.get("label") == LABEL]
    live = {p["pane_id"] for t in ours for p in by_tab[t["tab_id"]] if healthy(h, p["pane_id"])}
    keeper = next((t for t in ours if any(p["pane_id"] in live for p in by_tab[t["tab_id"]])),
                  ours[0] if ours else None)

    if keeper is None:
        pane_id = open_dashboard(h, ws)
        keeper = {"tab_id": h.call("pane", "get", pane_id)["pane"]["tab_id"]}
        h.call("tab", "rename", keeper["tab_id"], LABEL)
        actions.append(f"opened the {LABEL} tab")
    else:
        # Open the new dashboard before closing the broken one, so the tab never empties and closes;
        # closing it afterwards gives the new pane its room back before it is judged.
        inside = by_tab[keeper["tab_id"]]
        broken = [p["pane_id"] for p in inside if p["pane_id"] not in live and is_ours(h, p)]
        if not any(p["pane_id"] in live for p in inside):
            foreign = [p["pane_id"] for p in inside if p["pane_id"] not in broken]
            pane_id = open_dashboard(h, ws, beside=(foreign or broken)[0])
            actions.append("restarted the dashboard")
        for p in broken:
            h.call("pane", "close", p)
    # A duplicate tokenrate tab is closed only when every pane in it is the dashboard's own.
    for t in ours:
        if t["tab_id"] != keeper["tab_id"] and all(is_ours(h, p) for p in by_tab[t["tab_id"]]):
            h.call("tab", "close", t["tab_id"])
            actions.append(f"closed a duplicate {LABEL} tab")

    order = [t["tab_id"] for t in tabs_of(h, ws)]
    if len(order) > POSITION and order.index(keeper["tab_id"]) != POSITION:
        h.api("tab.move", {"tab_id": keeper["tab_id"], "insert_index": POSITION})
        actions.append(f"moved it to tab {POSITION + 1}")
    if focus and focused_tab(h) != focus:
        h.raw("tab", "focus", focus)
    if pane_id:
        drawn(h, pane_id)

    result = check(h, label)
    return result if not actions else f"{result} ({', '.join(actions)})"


USAGE = "usage: tokenrate tab ensure [--quiet] [--workspace LABEL] [--label LABEL] | check [--workspace LABEL] [--label LABEL]\n"


def workspace_of_this_pane(h: Herdr) -> str | None:
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return None
    p = h.raw("pane", "get", pane)
    try:
        ws = json.loads(p.stdout)["result"]["pane"]["workspace_id"]
    except (ValueError, KeyError, TypeError):
        return None
    for w in json.loads(h.raw("workspace", "list").stdout or "{}").get("result", {}).get("workspaces", []):
        if w.get("workspace_id") == ws:
            return w.get("label")
    return None


def main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE + __doc__.split("\n", 2)[2])
        return 0 if argv else 2
    cmd, opts = argv[0], list(argv[1:])
    global LABEL
    quiet, label, tab_label = False, None, None
    while opts:
        o = opts.pop(0)
        if o == "--quiet" and cmd == "ensure":
            quiet = True
        elif o == "--workspace" and opts and opts[0]:
            label = opts.pop(0)
        elif o == "--label" and opts and opts[0]:
            tab_label = opts.pop(0)
        else:
            cmd = None
            break
    if cmd not in ("ensure", "check"):
        sys.stderr.write(USAGE)
        return 2
    from . import settings
    label = label or settings.get("tab_workspace")
    LABEL = tab_label or settings.get("tab_label") or LABEL
    try:
        h = connect()
        label = label or workspace_of_this_pane(h)
        if not label:
            raise Problem("no workspace named: pass --workspace LABEL or set tab_workspace")
        line = ensure(h, label) if cmd == "ensure" else check(h, label)
    except Skip as s:
        if cmd == "check":
            print(f"tokenrate tab: {s}")
            return 1
        if not quiet:
            print(f"tokenrate tab: nothing to do: {s}")
        return 0
    except Problem as p:
        print(f"tokenrate tab: {p}")
        return 1
    if not quiet:
        print(f"tokenrate tab: {line}")
    return 0
