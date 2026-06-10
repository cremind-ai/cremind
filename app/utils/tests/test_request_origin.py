"""Unit test: loopback origin recording for the dynamic Google OAuth redirect.

record_origin() must capture only LOOPBACK Host headers (so the OAuth redirect
tracks the user's port-forward port), and ignore real hostnames (Ingress), where
a Desktop OAuth client can't redirect to the pod anyway.

Run standalone:  python app/utils/tests/test_request_origin.py
Or via pytest:   pytest app/utils/tests/test_request_origin.py
"""
import sys
from pathlib import Path

# Repo root = parents[3] of app/utils/tests/test_request_origin.py
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.utils import request_origin as ro  # noqa: E402


def test_loopback_detection():
    for host in ("localhost", "localhost:8081", "127.0.0.1", "127.0.0.1:9000", "[::1]:8080"):
        assert ro.is_loopback_host(host), f"expected loopback: {host}"
    for host in ("cremind.example.com", "cremind.example.com:443", "10.0.0.5:8080", "", "evil-localhost.com"):
        assert not ro.is_loopback_host(host), f"expected NON-loopback: {host}"


def test_records_loopback_with_port():
    ro.record_origin("localhost:8081")
    assert ro.get_loopback_origin() == "http://localhost:8081"
    ro.record_origin("127.0.0.1:9000")
    assert ro.get_loopback_origin() == "http://127.0.0.1:9000"


def test_ignores_non_loopback():
    ro.record_origin("localhost:8081")               # set a known good value
    ro.record_origin("cremind.example.com")          # must NOT overwrite
    assert ro.get_loopback_origin() == "http://localhost:8081"
    ro.record_origin("")                              # empty must NOT overwrite
    assert ro.get_loopback_origin() == "http://localhost:8081"


if __name__ == "__main__":
    test_loopback_detection()
    test_records_loopback_with_port()
    test_ignores_non_loopback()
    print("OK: request_origin records loopback origins and ignores the rest.")
