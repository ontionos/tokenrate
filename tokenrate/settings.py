"""Where tokenRate keeps its files, and its optional settings.

Files (nothing else is ever written):
  $XDG_CACHE_HOME/tokenrate/   (default ~/.cache/tokenrate)        ccusage.json, the cached counts, and a
                                                                    short-lived .refresh.lock directory
  $XDG_DATA_HOME/tokenrate/    (default ~/.local/share/tokenrate)  ccusage/, the pinned ccusage that
                                                                    `tokenrate --install-ccusage` installs
  $XDG_CONFIG_HOME/tokenrate/config (default ~/.config/tokenrate/config)
                                                                    optional settings, written only by you

The config file holds `key = value` lines; `#` starts a comment. Every key can also be set in the
environment as TOKENRATE_<KEY in capitals>, which wins over the file:
  ttl                      seconds the cached counts stay fresh (default 300)
  ccusage                  ccusage executable to run instead of the installed or PATH one
  classifier_dir           folder holding jev-usage.jsonl and clef-usage.jsonl, the optional
                           cheap-classifier usage logs; unset or absent files mean no such section
  jev_input_usd_per_mtok   Jev price per million input tokens (default 0.042; output is free)
  clef_usd_per_call        Clef price per call (default 0.0004)
  tab_workspace            the Herdr workspace label `tokenrate tab` manages
TOKENRATE_CONFIG names another config file; TOKENRATE_JSON reads a fixed payload (tests, demos).
"""

from __future__ import annotations

import os


def _xdg(var: str, fallback: str) -> str:
    value = os.environ.get(var)
    return value if value and os.path.isabs(value) else os.path.expanduser(fallback)


def cache_dir() -> str:
    return os.path.join(_xdg("XDG_CACHE_HOME", "~/.cache"), "tokenrate")


def data_dir() -> str:
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "tokenrate")


def config_path() -> str:
    return os.environ.get("TOKENRATE_CONFIG") or os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"),
                                                                "tokenrate", "config")


def config_file() -> dict:
    out = {}
    try:
        with open(config_path(), encoding="utf-8") as f:
            for raw in f:
                key, sep, value = raw.split("#", 1)[0].partition("=")
                if sep and key.strip():
                    out[key.strip().lower()] = value.strip()
    except OSError:
        pass
    return out


def get(key: str, default: str | None = None) -> str | None:
    env = os.environ.get("TOKENRATE_" + key.upper())
    if env:
        return env
    return config_file().get(key) or default


def get_float(key: str, default: float) -> float:
    try:
        return float(get(key) or default)
    except ValueError:
        return default


def get_int(key: str, default: int) -> int:
    try:
        return max(0, int(get(key) or default))
    except ValueError:
        return default
