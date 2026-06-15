# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-dotenv",
# ]
# ///
"""Unit tests: ws_url() derivation and require_url() validation.

Run standalone:  python scripts/tests/test_config.py
Or via pytest:   pytest scripts/tests/test_config.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import config  # noqa: E402


def test_ws_url_http():
    assert config.ws_url("http://homeassistant.local:8123") == "ws://homeassistant.local:8123/api/websocket"


def test_ws_url_https():
    assert config.ws_url("https://abc123.ui.nabu.casa") == "wss://abc123.ui.nabu.casa/api/websocket"


def test_ws_url_trailing_slash():
    assert config.ws_url("http://h:8123/") == "ws://h:8123/api/websocket"


def test_ws_url_subpath_preserved():
    assert config.ws_url("https://example.com/ha") == "wss://example.com/ha/api/websocket"


def _expect_runtime_error(url):
    old_url = config.HA_URL
    config.HA_URL = url
    try:
        try:
            config.require_url()
        except RuntimeError:
            return True
        return False
    finally:
        config.HA_URL = old_url


def test_require_url_missing():
    assert _expect_runtime_error("") is True


def test_require_url_bad_url():
    assert _expect_runtime_error("not-a-url") is True


def test_require_url_ok():
    old_url = config.HA_URL
    config.HA_URL = "http://homeassistant.local:8123"
    try:
        assert config.require_url() == "http://homeassistant.local:8123"
    finally:
        config.HA_URL = old_url


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: ws_url derivation and URL validation.")
