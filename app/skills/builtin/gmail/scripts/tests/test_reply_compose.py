"""Tests for send-only reply composition.

Under ``gmail.send`` there is no way to read the original message, so a reply's
subject and threading headers must be built from what the caller supplies.

Run standalone (no pytest needed):  python scripts/tests/test_reply_compose.py
Or via pytest:                      pytest scripts/tests/test_reply_compose.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app.gmail_api import compose_reply_subject


def test_prefixes_re_when_missing():
    assert compose_reply_subject("Lunch?") == "Re: Lunch?"


def test_does_not_double_prefix():
    assert compose_reply_subject("Re: Lunch?") == "Re: Lunch?"


def test_existing_prefix_is_matched_case_insensitively_and_kept_verbatim():
    # Mail clients treat these as equivalent; rewriting the case would needlessly
    # differ from what the recipient's client already shows.
    assert compose_reply_subject("RE: Lunch?") == "RE: Lunch?"
    assert compose_reply_subject("re: Lunch?") == "re: Lunch?"


def test_trims_surrounding_whitespace():
    assert compose_reply_subject("  Lunch?  ") == "Re: Lunch?"


def test_empty_subject_still_yields_a_reply_subject():
    assert compose_reply_subject("") == "Re:"
    assert compose_reply_subject("   ") == "Re:"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
