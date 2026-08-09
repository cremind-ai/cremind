"""`status` must describe what the token can reach, not what a future link asks for.

The two disagree in both directions — a user who set GOOGLE_SCOPES but has not
re-linked yet, and a legacy token still holding whole-Drive after the shared
client narrowed — and reporting the wrong one sends the agent down the wrong path
(skipping a needed grant, or running a pointless one).

Run standalone (no pytest needed):  python scripts/tests/test_status_access_model.py
Or via pytest:                      pytest scripts/tests/test_status_access_model.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import cli, errors
from app.google.discovery import DiscoveryError

PER_FILE = errors.DRIVE_FILE_SCOPE
LEGACY = errors.LEGACY_DRIVE_SCOPE
BASE = ["openid", "email"]


def _status(*, granted, expected, own_client=False, client_id="shared-cid", resolved=True):
    """Run cmd_status with the account, discovery and BYO check all stubbed."""
    saved = (cli.auth.load_account, cli._expected_scopes, cli._uses_own_client)
    cli.auth.load_account = lambda _p: {
        "email": "a@b.c", "account_key": "k", "scopes": list(granted), "client_id": client_id,
    }
    cli._expected_scopes = lambda: (list(expected), resolved)
    cli._uses_own_client = lambda _cid: own_client
    try:
        return cli.cmd_status(None)
    finally:
        cli.auth.load_account, cli._expected_scopes, cli._uses_own_client = saved


def test_byo_scopes_configured_but_not_relinked_still_reports_per_file():
    # The failure this whole fix exists for: GOOGLE_SCOPES asks for whole-Drive,
    # the token is still per-file. Claiming whole-Drive here makes the agent skip
    # `grant` and every by-id call 404s.
    out = _status(granted=[*BASE, PER_FILE], expected=[*BASE, LEGACY])
    assert out["access_model"].startswith("per-file (drive.file)")
    assert out.get("scopes_stale") is not True
    assert "Re-run `link`" in out["hint"]


def test_a_legacy_token_under_a_narrowed_broker_still_reports_whole_drive():
    # The converse: the token really does reach everything today, even though the
    # shared client would no longer ask for it. Both facts must show at once.
    out = _status(granted=[*BASE, LEGACY], expected=[*BASE, PER_FILE], own_client=False)
    assert out["access_model"] == "whole-Drive (the shared Cremind client still requests it)"
    assert out["scopes_stale"] is True


def test_whole_drive_is_not_attributed_to_credentials_the_user_never_supplied():
    out = _status(granted=[*BASE, LEGACY], expected=[*BASE, LEGACY], own_client=False)
    assert "shared Cremind client" in out["access_model"]
    assert "your own" not in out["access_model"]
    assert "bring-your-own" not in out["access_model"]
    assert out.get("scopes_stale") is not True


def test_a_genuinely_byo_account_is_labelled_as_such():
    out = _status(granted=[*BASE, LEGACY], expected=[*BASE, LEGACY], own_client=True)
    assert out["access_model"] == "whole-Drive (bring-your-own credentials)"
    assert out.get("scopes_stale") is not True
    assert "hint" not in out


def test_plain_per_file_account_has_no_hint():
    out = _status(granted=[*BASE, PER_FILE], expected=[*BASE, PER_FILE])
    assert out["access_model"].startswith("per-file (drive.file)")
    assert "hint" not in out
    assert out.get("scopes_stale") is not True


def test_not_linked():
    saved = cli.auth.load_account

    def _raise(_p):
        raise cli.auth.AuthError("nope")

    cli.auth.load_account = _raise
    try:
        assert cli.cmd_status(None) == {"linked": False}
    finally:
        cli.auth.load_account = saved


def test_an_unreachable_broker_reports_unresolved_instead_of_stale():
    # The fallback is a guess, and its remedy (re-link) permanently narrows the
    # account — so an outage must never produce the warning.
    out = _status(granted=[*BASE, LEGACY], expected=[*BASE, PER_FILE], resolved=False)
    assert out["expected_unresolved"] is True
    assert "scopes_stale" not in out
    assert "hint" not in out
    # It still reports what the token can actually reach.
    assert out["access_model"].startswith("whole-Drive")


def test_an_unreachable_broker_does_not_suppress_the_access_model():
    out = _status(granted=[*BASE, PER_FILE], expected=[*BASE, PER_FILE], resolved=False)
    assert out["access_model"].startswith("per-file (drive.file)")
    assert out["expected_unresolved"] is True


# --- _expected_scopes: distinguishing an answer from a guess ---


def _expected(*, env_scopes="", advertised=None, raises=False):
    saved_env = cli.config.GOOGLE_SCOPES
    saved_disc = cli.Discovery
    cli.config.GOOGLE_SCOPES = env_scopes

    class _Disc:
        def __init__(self, _url):
            pass

        def scopes(self, _resource):
            if raises:
                raise DiscoveryError("broker down")
            return list(advertised or [])

    cli.Discovery = _Disc
    try:
        return cli._expected_scopes()
    finally:
        cli.config.GOOGLE_SCOPES = saved_env
        cli.Discovery = saved_disc


def test_an_env_override_is_a_resolved_answer():
    assert _expected(env_scopes=f"openid email {LEGACY}") == ([*BASE, LEGACY], True)


def test_an_advertised_set_is_a_resolved_answer():
    assert _expected(advertised=[*BASE, PER_FILE]) == ([*BASE, PER_FILE], True)


def test_a_broker_outage_is_an_unresolved_guess():
    scopes, resolved = _expected(raises=True)
    assert resolved is False
    assert PER_FILE in scopes


def test_an_empty_advertisement_is_an_unresolved_guess():
    assert _expected(advertised=[])[1] is False


# --- _uses_own_client: never claim bring-your-own we cannot demonstrate ---


def _uses_own(*, env_client_id="", token_client_id="", shared="shared-cid", raises=False):
    saved_env = cli.config.GOOGLE_CLIENT_ID
    saved_disc = cli.Discovery
    cli.config.GOOGLE_CLIENT_ID = env_client_id

    class _Disc:
        def __init__(self, _url):
            pass

        def credentials(self):
            if raises:
                raise DiscoveryError("broker down")
            return {"clientId": shared}

        def client_id(self):
            return shared

    cli.Discovery = _Disc
    try:
        return cli._uses_own_client(token_client_id)
    finally:
        cli.config.GOOGLE_CLIENT_ID = saved_env
        cli.Discovery = saved_disc


def test_an_env_supplied_client_counts_as_byo():
    assert _uses_own(env_client_id="mine") is True


def test_the_shared_client_is_not_byo():
    assert _uses_own(token_client_id="shared-cid", shared="shared-cid") is False


def test_a_different_client_is_byo():
    assert _uses_own(token_client_id="mine-cid", shared="shared-cid") is True


def test_an_unreachable_broker_never_claims_byo():
    assert _uses_own(token_client_id="mine-cid", raises=True) is False


def test_a_token_without_a_client_id_is_not_byo():
    assert _uses_own(token_client_id="") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
