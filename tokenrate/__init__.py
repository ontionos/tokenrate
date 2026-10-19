"""tokenrate - a terminal token and cost dashboard over ccusage.

settings.py says where tokenrate keeps its files and reads the optional config file,
data.py loads and caches ccusage JSON and turns it into a summary model,
classifiers.py adds the optional cheap-classifier usage logs,
view.py lays the model out as styled lines for a given width,
tui.py draws those lines with curses, snapshot.py prints them as ANSI, plain text or SVG,
and tab.py keeps the dashboard open as a fixed tab of a Herdr workspace.
"""

__version__ = "0.1.0"
