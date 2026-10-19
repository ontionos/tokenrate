"""Command line for bin/tokenrate; `tokenrate --help` documents it."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from . import __version__, data, settings, snapshot, view

HELP = """\
tokenrate - token and cost dashboard over the last 7, 30 or 90 days, from ccusage.

  tokenrate                          open the dashboard in this terminal
                                     (q quits, r counts again, ? help; a mouse click works on
                                     views, periods, refresh and help, the wheel scrolls)
  tokenrate --refresh                count again now (one ccusage run), then exit
  tokenrate --snapshot [--width N --height N --view NAME --days N --format ansi|plain|svg --theme dark|light]
                                     print one frame without a terminal
  tokenrate --json [--days N]        print the summary the views are drawn from
  tokenrate --install-ccusage        install the pinned ccusage into the data folder (needs network once)
  tokenrate --demo [...]             any of the above with made-up data instead of your logs
  tokenrate --paths                  print every folder and file tokenrate uses
  tokenrate --purge                  delete tokenrate's cache and its ccusage install (not your config)
  tokenrate tab ensure|check ...     keep the dashboard open as tab 2 of a Herdr workspace (tab --help)

Dollars are the public API price of the tokens: an estimate, not a bill.
"""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["tab"]:
        from . import tab
        return tab.main(argv[1:])
    p = argparse.ArgumentParser(prog="tokenrate", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--snapshot", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--install-ccusage", action="store_true")
    p.add_argument("--paths", action="store_true")
    p.add_argument("--demo", action="store_true", help="made-up data instead of your logs")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--version", action="version", version=f"tokenrate {__version__}")
    p.add_argument("--no-refresh", action="store_true", help="never run ccusage; use the cache as it is")
    p.add_argument("--width", type=int, default=120)
    p.add_argument("--height", type=int, default=48)
    p.add_argument("--view", default="overview", choices=[v.lower() for v in view.VIEWS])
    p.add_argument("--days", type=int, default=30, choices=view.RANGES)
    p.add_argument("--format", default="ansi", choices=("ansi", "plain", "svg", "basic"))
    p.add_argument("--theme", default="dark", choices=("dark", "light"))
    p.add_argument("--today", help=argparse.SUPPRESS)
    p.add_argument("--help-screen", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--scroll", type=int, default=0, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    view_index = [v.lower() for v in view.VIEWS].index(a.view)
    today = dt.date.fromisoformat(a.today) if a.today else None
    if a.demo:
        os.environ["TOKENRATE_DEMO"] = a.today or "1"

    if a.install_ccusage:
        return install_ccusage()
    if a.paths:
        return print_paths()
    if a.purge:
        return purge()
    if a.refresh:
        err = data.refresh()
        if err:
            print(f"tokenrate: {err}", file=sys.stderr)
            return 1
        return 0
    if not (a.snapshot or a.json):
        if not sys.stdout.isatty():
            print("tokenrate: not a terminal; use --snapshot or --json", file=sys.stderr)
            return 2
        from . import tui
        tui.run(view_index, a.days)
        return 0

    payload, error = data.read_payload()
    if not a.no_refresh and data.needs_refresh(payload):
        err = data.refresh()
        payload, error = data.read_payload()
        error = err or error
    summary = data.summarize(payload, a.days, today=today)
    if a.json:
        json.dump({"summary": summary, "error": error}, sys.stdout, indent=1, default=list)
        sys.stdout.write("\n")
        return 0
    status = view.status_text(summary, False, None)
    lines = snapshot.frame(summary, view_index, a.width, a.height, status, error, a.help_screen, a.scroll)
    if a.format == "plain":
        sys.stdout.write(snapshot.plain(lines))
    elif a.format == "svg":
        sys.stdout.write(snapshot.svg(lines, a.width, a.theme))
    else:
        sys.stdout.write(snapshot.ansi(lines, basic=a.format == "basic"))
    return 0


CCUSAGE_VERSION = "20.0.36"


def installed_ccusage_version() -> str | None:
    try:
        with open(os.path.join(data.ccusage_dir(), "node_modules", "ccusage", "package.json")) as f:
            return json.load(f).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def install_ccusage() -> int:
    """npm install --ignore-scripts of the pinned version into tokenrate's own data folder; nothing global."""
    import shutil
    import subprocess
    target = data.ccusage_dir()
    if installed_ccusage_version() == CCUSAGE_VERSION:
        print(f"tokenrate: ccusage {CCUSAGE_VERSION} is already installed in {target}", file=sys.stderr)
        return 0
    npm = shutil.which("npm")
    if not npm:
        print("tokenrate: npm not found; install Node.js, or set TOKENRATE_CCUSAGE to a ccusage executable",
              file=sys.stderr)
        return 1
    data.private_dir(target)
    cmd = [npm, "install", "--prefix", target, "--ignore-scripts", "--no-audit", "--no-fund", "--no-save",
           "--no-package-lock", f"ccusage@{CCUSAGE_VERSION}"]
    print("tokenrate: " + " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd, stdout=sys.stderr)


def print_paths() -> int:
    exe = data.ccusage_bin()
    print(f"cache    {data.cache_dir()}")
    print(f"ccusage  {data.ccusage_dir()}  (installed: {installed_ccusage_version() or 'no'}; runs: {exe or 'none found'})")
    print(f"config   {settings.config_path()}  ({'present' if os.path.exists(settings.config_path()) else 'absent'})")
    return 0


def purge() -> int:
    """Remove the cache and the ccusage install; the config file is yours and stays."""
    import shutil
    removed = []
    for path in (data.cache_dir(), settings.data_dir()):
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed.append(path)
    print("tokenrate: removed " + (", ".join(removed) if removed else "nothing (no cache or ccusage install)"))
    if os.path.exists(settings.config_path()):
        print(f"tokenrate: kept your config file {settings.config_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
