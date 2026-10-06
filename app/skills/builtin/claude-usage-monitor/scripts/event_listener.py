# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Claude Usage Monitor — the skill's long-running app.

Follows Claude Code's own files every 2 seconds, raises the usage alerts as Cremind
events and serves the dashboard on 127.0.0.1. Cremind runs it from this path for every
profile that starts the listener; see usage_monitor/service.py.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from usage_monitor.service import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
