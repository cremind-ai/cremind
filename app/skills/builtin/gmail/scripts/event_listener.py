# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-dotenv",
#   "google-api-python-client>=2.0",
#   "google-auth>=2.0",
#   "google-auth-oauthlib>=1.0",
#   "websocket-client>=1.6",
# ]
# ///
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from app.listener import run

if __name__ == "__main__":
    run()
