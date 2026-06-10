# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-dotenv",
#   "caldav>=1.3,<2.0",
#   "icalendar>=5.0",
#   "python-dateutil",
# ]
# ///
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
