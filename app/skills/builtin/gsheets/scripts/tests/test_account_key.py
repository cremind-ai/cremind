"""Parity test: routing keys MUST match cremind-connect's golden vectors.

Run standalone (no pytest needed):  python scripts/tests/test_account_key.py
Or via pytest:                      pytest scripts/tests/test_account_key.py
"""
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app.google.account_key import account_key_for, normalize_email

_GOLDEN = _SCRIPTS / "app" / "google" / "golden_account_keys.json"


def _load_vectors():
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))["vectors"]


def test_golden_vectors():
    for v in _load_vectors():
        assert account_key_for("google", v["email"]) == v["accountKey"], v["email"]


def test_normalize_email():
    assert normalize_email("  Foo@Bar.COM ") == "foo@bar.com"


def test_dots_not_collapsed():
    assert account_key_for("google", "u.s.e.r@gmail.com") != account_key_for("google", "user@gmail.com")


if __name__ == "__main__":
    test_golden_vectors()
    test_normalize_email()
    test_dots_not_collapsed()
    print(f"OK: {len(_load_vectors())} golden vectors match; parity with cremind-connect verified.")
