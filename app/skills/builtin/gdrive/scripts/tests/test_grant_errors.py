"""Tests for the ungranted-file error payload and the picker URL builder.

Run standalone (no pytest needed):  python scripts/tests/test_grant_errors.py
Or via pytest:                      pytest scripts/tests/test_grant_errors.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import errors
from app.grant import DRIVE_FILE_SCOPE, build_authorize_url, build_picker_params, parse_picked_ids

_LEGACY = "https://www.googleapis.com/auth/drive"


def test_picker_requests_drive_file_alone():
    # Google rejects a picker request that carries any other scope.
    params = build_picker_params(
        client_id="cid", redirect_uri="http://localhost:9/", state="s" * 24,
        code_challenge="chal",
    )
    assert params["scope"] == DRIVE_FILE_SCOPE
    assert " " not in params["scope"]
    assert params["trigger_onepick"] == "true"
    assert params["prompt"] == "consent"


def test_picker_optional_params():
    params = build_picker_params(
        client_id="cid", redirect_uri="http://localhost:9/", state="s" * 24,
        code_challenge="chal", file_ids=["a", "b"], mime_types=["text/csv"],
        allow_multiple=False, allow_folders=False, login_hint="u@e.com",
    )
    assert params["file_ids"] == "a,b"
    assert params["mimetypes"] == "text/csv"
    assert params["login_hint"] == "u@e.com"
    assert "allow_multiple" not in params
    assert "allow_folder_selection" not in params


def test_authorize_url_is_a_google_consent_url():
    url, _ = build_authorize_url(
        client_id="cid", redirect_uri="http://localhost:9/", state="s" * 24,
        code_challenge="chal",
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "trigger_onepick=true" in url


def test_parse_picked_ids():
    assert parse_picked_ids("code=x&picked_file_ids=a,b") == ["a", "b"]
    assert parse_picked_ids("code=x") == []
    assert parse_picked_ids("") == []


def test_not_granted_payload_covers_both_contexts():
    payload = errors.not_granted_payload(file_id="F1", status=404)
    assert payload["error"] == "drive_file_not_granted"
    assert payload["file_id"] == "F1"
    # A 404 is ambiguous under drive.file; the message must not claim otherwise.
    assert "does not exist" in payload["message"]
    # An agent picks the fix from its own context, so both must always be present.
    assert "grant --file F1" in payload["interactive_fix"]
    assert "do NOT run" in payload["unattended_fix"]
    assert "notification" in payload["unattended_fix"].lower()
    assert "scopes_stale" not in payload


def test_not_granted_payload_flags_a_stale_link():
    payload = errors.not_granted_payload(file_id="F1", status=403, stale_scopes=True)
    assert payload["scopes_stale"] is True
    assert "Re-link" in payload["message"] or "re-link" in payload["message"]


def test_stale_scope_detection():
    per_file = ["openid", "email", DRIVE_FILE_SCOPE]
    assert errors.scopes_are_stale([_LEGACY], per_file) is True
    assert errors.scopes_are_stale([_LEGACY, DRIVE_FILE_SCOPE], per_file) is False
    assert errors.scopes_are_stale([DRIVE_FILE_SCOPE], per_file) is False
    assert errors.scopes_are_stale([], per_file) is False
    assert errors.scopes_are_stale(None, per_file) is False


def test_an_unreachable_broker_never_flags_stale():
    # No ``expected`` means discovery could not answer. Acting on that guess would
    # push the user into a re-link that permanently drops whole-Drive access.
    assert errors.scopes_are_stale([_LEGACY], None) is False
    assert errors.scopes_are_stale([_LEGACY]) is False


def test_whole_drive_is_not_stale_when_it_was_requested():
    # A bring-your-own-credentials user holds the broad scope deliberately, so
    # telling them to re-link would be wrong.
    assert errors.scopes_are_stale([_LEGACY], ["openid", "email", _LEGACY]) is False
    # Asking for per-file but holding the old broad grant is the real stale case.
    assert errors.scopes_are_stale([_LEGACY], ["openid", "email", DRIVE_FILE_SCOPE]) is True


def test_http_status_extraction():
    class Resp:
        status = 404

    class Err(Exception):
        resp = Resp()

    assert errors.http_status(Err()) == 404
    assert errors.http_status(Exception()) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
