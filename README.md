# tokenrate

A terminal dashboard of the tokens your coding agents used and what they would cost at API prices, over the last 7, 30 or 90 days.
It reads the local Claude Code and Codex logs through [ccusage](https://github.com/ccusage/ccusage), so it works offline once ccusage is installed.
It runs on its own in any terminal, and as a [Herdr](https://herdr.dev) plugin in an overlay or a fixed tab.

The dollars are the public API price of the tokens, cache reads and cache writes included: an estimate, not a bill.
A subscription is billed separately and does not change with these numbers.

## What it shows

- **Overview** - cost per day coloured by each day's main model, with peaks marked; model share; token types (cache read, cache write, input, output); projects; busiest days; this week against the week before.
- **Models** - every model with its tokens by type and its cost per day.
- **Projects** - cost per working folder.
- **Days** - every day of the period.
- **Cheap classifiers** - optional: if you log Jev and Clef classifier calls as `jev-usage.jsonl` and `clef-usage.jsonl`, set `classifier_dir` (see Settings) and one line shows their calls and cost.
  Without the setting or the files nothing is shown.

## Requirements

- macOS or Linux.
- Python 3.9 or newer with its standard `curses` module (the system or Homebrew Python is fine).
- Node.js with `npm`, once, to install the pinned ccusage; or any `ccusage` already on your `PATH`.
- Herdr 0.9 or newer, only for the plugin.

## Install

### As a Herdr plugin

```sh
herdr plugin install ontionos/tokenrate
```

Herdr clones with `git`, so git must be able to read the repository; for a private copy, sign git in to an account that can, for example `GH_TOKEN=$(gh auth token -u <account>) herdr plugin install ontionos/tokenrate` when git uses the GitHub CLI as its credential helper.

Herdr clones the repository into its own plugin folder and runs the plugin's build step, which installs ccusage 20.0.36 into `~/.local/share/tokenrate/ccusage` with `npm install --prefix` (nothing global, no install scripts).
Without npm the build step only says so; tokenrate then uses a `ccusage` on your `PATH`.

Open it from the Herdr command palette with **tokenrate: open token dashboard**, or bind a key in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "tokenrate.open"
description = "token and cost dashboard"
```

### On its own

```sh
git clone https://github.com/ontionos/tokenrate.git
tokenrate/bin/tokenrate --install-ccusage
tokenrate/bin/tokenrate
```

`bin/tokenrate` can be linked onto your `PATH` (for example `ln -s "$PWD/tokenrate/bin/tokenrate" ~/.local/bin/tokenrate`); it follows the link back to its folder.

## Use

```sh
tokenrate                    # the dashboard in this terminal
tokenrate --refresh          # count again now, then exit
tokenrate --json --days 7    # the numbers the dashboard draws, as JSON
tokenrate --snapshot --format plain --width 120 --height 40   # one frame without a terminal (also ansi or svg)
tokenrate --paths            # every folder and file tokenrate uses
tokenrate --help
```

The first open runs ccusage, which takes a few seconds on a long history and briefly needs about 1 GB of memory.
After that the counts are cached and counted again only when they are older than five minutes or when you press `r`, one ccusage run at a time.

### Keys and mouse

| Key | Does |
| --- | --- |
| `q`, `Esc` | close (Esc first closes the help) |
| `r` | count again now |
| `Tab`, `1`-`4` | Overview, Models, Projects, Days |
| `7`, `3`, `9`, `d` | last 7, 30 or 90 days; `d` cycles |
| `↑` `↓` `j` `k` | scroll; `PgUp` `PgDn` `Space` `b` page; `g` `G` top and bottom |
| `?` | help |

The overview fits itself to the window: the chart grows into spare rows and the lists shorten when rows are few, so scrolling is only needed in a window too small for even the compact overview, or for long lists such as 90 days of Days.
It redraws whenever the terminal or pane is resized.

A mouse click works on the view tabs, the period pills, refresh and help, and the wheel scrolls.
tokenrate asks the terminal for button presses only, never pointer motion, so moving the mouse over it sends nothing.

### A fixed tab in a Herdr workspace

```sh
tokenrate tab ensure --workspace work   # open or repair a "tokenrate" tab as tab 2 of workspace "work"
tokenrate tab check --workspace work    # verify it is there, running and drawing; changes nothing
```

`ensure` touches only the tokenrate tab and its own dashboard panes and never moves your focus; `--label` names the tab differently and `--quiet` prints only a problem.
Without `--workspace` it uses the `tab_workspace` setting, else the workspace of the Herdr pane it runs in.
Run it from a login script or a timer to keep the tab open; `tokenrate tab --help` has the details.

## Settings

Everything works without settings.
The optional file `~/.config/tokenrate/config` (or `$XDG_CONFIG_HOME/tokenrate/config`, or the file `TOKENRATE_CONFIG` names) holds `key = value` lines, and each key can also be set as an environment variable `TOKENRATE_<KEY>` in capitals, which wins.

| Key | Default | Meaning |
| --- | --- | --- |
| `ttl` | `300` | seconds the cached counts stay fresh |
| `ccusage` | installed copy, else `PATH` | the ccusage executable to run |
| `classifier_dir` | unset | folder with `jev-usage.jsonl` and `clef-usage.jsonl` |
| `jev_input_usd_per_mtok` | `0.042` | Jev price per million input tokens |
| `clef_usd_per_call` | `0.0004` | Clef price per call |
| `tab_workspace` | unset | workspace label for `tokenrate tab` |
| `tab_label` | `tokenrate` | label of that tab |

`tokenrate tab ensure` passes every `TOKENRATE_*` variable of its environment on to the dashboard it opens.

## Files

tokenrate writes only these, and `tokenrate --paths` prints them:

- `~/.cache/tokenrate/` (`$XDG_CACHE_HOME/tokenrate`) - the cached counts, `ccusage.json`, and a short-lived lock folder.
- `~/.local/share/tokenrate/ccusage/` (`$XDG_DATA_HOME/tokenrate/ccusage`) - the pinned ccusage.
- `~/.config/tokenrate/config` - only if you create it.

As a Herdr plugin, Herdr also keeps the cloned plugin and its registration under `~/.config/herdr/plugins/`, and an empty state folder in `~/.local/state/herdr/plugins/tokenrate`.

## Uninstall

As a Herdr plugin:

```sh
herdr plugin action invoke purge --plugin tokenrate   # deletes ~/.cache/tokenrate and ~/.local/share/tokenrate
herdr plugin uninstall tokenrate
# Herdr keeps an uninstalled plugin's files; these are all of tokenrate's:
rm -rf ~/.config/herdr/plugins/github-installations/tokenrate-* \
  ~/.config/herdr/plugins/.locks/.tokenrate-*.lock \
  ~/.config/herdr/plugins/config/tokenrate ~/.local/state/herdr/plugins/tokenrate
rm -rf ~/.config/tokenrate                           # only if you created a config file
```

Close any open tokenrate pane or tab first: Herdr keeps an installation that a running pane still uses.

On its own: `tokenrate --purge`, then delete the cloned folder, any link you made to `bin/tokenrate`, and `~/.config/tokenrate` if you created it.

## Tests

```sh
tests/run.sh
```

The tests use a fake ccusage and a fake Herdr, never your real logs or Herdr.

## Licence

MIT, see [LICENSE](LICENSE).
The dashboard layout adapts [herdr_agents_tracker](https://github.com/VHemanth45/herdr_agents_tracker) (MIT); its notice is in [LICENSE.herdr_agents_tracker](LICENSE.herdr_agents_tracker).
Counting is done by [ccusage](https://github.com/ccusage/ccusage) (MIT), which tokenrate runs but does not include.
