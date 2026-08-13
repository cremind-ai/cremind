"""The Google skills' own ``unlink`` — the in-chat half of the feature.

These live under ``tests/`` rather than ``<skill>/scripts/tests/`` because
``pyproject.toml`` sets ``testpaths = ["tests"]``, so skill-local tests never run
in CI. :mod:`tests.skills.test_google_auth_parity` pins the five copies of
``google/auth.py`` byte-identical, so exercising gmail's copy here exercises all
five.

Loading it is the one fiddly part. ``spec_from_file_location`` alone fails —
``auth.py`` does ``from .account_key import account_key_for`` — and putting the
skill's ``scripts`` directory on ``sys.path`` would collide with the repo's own
``app`` package. So a synthetic parent package is registered whose ``__path__``
points at the skill's ``google/`` directory, and ``auth.py`` is loaded as its
submodule.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.skills.sync import BUILTIN_SKILLS_DIR

GOOGLE_DIR = BUILTIN_SKILLS_DIR / "gmail" / "scripts" / "app" / "google"
_PKG = "gskill_google"


def _load_auth():
    package = types.ModuleType(_PKG)
    package.__path__ = [str(GOOGLE_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PKG] = package
    spec = importlib.util.spec_from_file_location(f"{_PKG}.auth", GOOGLE_DIR / "auth.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


auth = _load_auth()


# ── fixtures ─────────────────────────────────────────────────────────────────

class Skills:
    """A fake profile skills root, plus a recorder for the one network seam."""

    def __init__(self, root: Path):
        self.root = root
        self.events: List[str] = []
        self.posts: List[Tuple[str, Dict[str, str]]] = []
        self.response: Tuple[int, str] = (200, "")

    def scripts(self, skill: str) -> Path:
        path = self.root / skill / "scripts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def link(
        self,
        skill: str,
        *,
        email: str = "u@example.com",
        client_id: str = "shared-cid",
        refresh: str = "rt-1",
        account_key: Optional[str] = "ak-1",
    ) -> Path:
        scripts = self.scripts(skill)
        (scripts / ".env").write_text("GOOGLE_SCOPES=\n", encoding="utf-8")
        payload: Dict[str, Any] = {
            "access_token": "at-1",
            "refresh_token": refresh,
            "client_id": client_id,
            "client_secret": "csecret",
            "scopes": ["openid", "email"],
            "email": email,
            "expiry": time.time() + 3600,
        }
        if account_key is not None:
            payload["account_key"] = account_key
        (scripts / ".google_token.json").write_text(json.dumps(payload), encoding="utf-8")
        return scripts

    def token(self, skill: str) -> Path:
        return self.root / skill / "scripts" / ".google_token.json"


@pytest.fixture
def skills(tmp_path, monkeypatch):
    harness = Skills(tmp_path)

    def fake_post(url, fields, *, timeout):
        harness.events.append("revoke")
        harness.posts.append((url, dict(fields)))
        return harness.response

    monkeypatch.setattr(auth, "_post_form", fake_post)
    return harness


# ── revoke_token ─────────────────────────────────────────────────────────────

def test_a_200_is_revoked(skills):
    skills.response = (200, "")

    assert auth.revoke_token("rt-1") == (True, "revoked")
    assert skills.posts[0] == (auth.GOOGLE_REVOKE_URI, {"token": "rt-1"})


def test_an_invalid_token_400_counts_as_already_revoked(skills):
    """Google returns this for a token it has already forgotten."""
    skills.response = (400, '{"error": "invalid_token"}')

    assert auth.revoke_token("rt-1") == (True, "already_revoked")


def test_an_invalid_token_400_is_recognised_without_json(skills):
    skills.response = (400, "error=invalid_token")

    assert auth.revoke_token("rt-1") == (True, "already_revoked")


def test_any_other_400_is_a_failure(skills):
    skills.response = (400, '{"error": "unsupported_token_type"}')

    revoked, status = auth.revoke_token("rt-1")

    assert revoked is False
    assert status.startswith("http_400")


def test_a_server_error_is_a_failure(skills):
    skills.response = (500, "boom")

    revoked, status = auth.revoke_token("rt-1")

    assert revoked is False
    assert status.startswith("http_500")


def test_a_network_error_is_reported_as_such(skills, monkeypatch):
    def explode(url, fields, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(auth, "_post_form", explode)

    revoked, status = auth.revoke_token("rt-1")

    assert revoked is False
    assert status.startswith("network:")


def test_no_token_never_touches_the_network(skills, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no HTTP call without a token")

    monkeypatch.setattr(auth, "_post_form", boom)

    assert auth.revoke_token("") == (False, "no_token")


# ── TokenStore.clear ─────────────────────────────────────────────────────────

def test_clear_removes_the_token_and_its_temp_sibling(tmp_path):
    """A crash between ``write`` and ``os.replace`` leaves a full credential set
    in the ``.tmp``, which is why every skill's .gitignore lists it."""
    token = tmp_path / ".google_token.json"
    temp = tmp_path / ".google_token.json.tmp"
    token.write_text("{}", encoding="utf-8")
    temp.write_text('{"refresh_token": "rt-1"}', encoding="utf-8")

    auth.TokenStore(token).clear()

    assert not token.exists()
    assert not temp.exists()


def test_clear_is_idempotent(tmp_path):
    auth.TokenStore(tmp_path / "never-existed.json").clear()


# ── unlink ───────────────────────────────────────────────────────────────────

def test_unlinking_something_never_linked_succeeds(skills):
    scripts = skills.scripts("gmail")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert out["ok"] is True
    assert out["unlinked"] is False
    assert out["reason"] == "not_linked"


def test_stray_derived_files_are_swept_with_no_token(skills):
    """The state a real install was found in: watch state, no credential."""
    scripts = skills.scripts("gcalendar")
    (scripts / ".listener_state.json").write_text('{"channel_id": "cm-x"}', encoding="utf-8")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert out["reason"] == "not_linked"
    assert ".listener_state.json" in out["removed"]
    assert not (scripts / ".listener_state.json").exists()


def test_a_clean_unlink_wipes_and_revokes(skills):
    scripts = skills.link("gmail")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert out["ok"] is True
    assert out["unlinked"] is True
    assert out["revoked"] is True
    assert out["revoke_status"] == "revoked"
    assert out["email"] == "u@example.com"
    assert ".google_token.json" in out["removed"]
    assert not skills.token("gmail").exists()


def test_the_temp_credential_file_goes_too(skills):
    scripts = skills.link("gmail")
    (scripts / ".google_token.json.tmp").write_text('{"refresh_token": "rt-1"}', encoding="utf-8")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert not (scripts / ".google_token.json.tmp").exists()
    assert out["unlinked"] is True


def test_a_failed_revoke_still_wipes_and_says_what_to_do(skills):
    skills.response = (500, "boom")
    scripts = skills.link("gmail")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert out["unlinked"] is True
    assert out["revoked"] is False
    assert out["revoke_error"].startswith("http_500")
    assert not skills.token("gmail").exists()
    # Never "try again": the token that could revoke it is gone for good.
    assert "re-running this cannot help" in out["action_required"]
    assert auth.GOOGLE_CONNECTIONS_URL in out["action_required"]


def test_no_revoke_never_contacts_google(skills, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("revoke must not be attempted with revoke_at_google=False")

    monkeypatch.setattr(auth, "_post_form", boom)
    scripts = skills.link("gmail")

    out = auth.unlink(token_path=scripts / ".google_token.json", revoke_at_google=False)

    assert out["unlinked"] is True
    assert out["revoke_status"] == "skipped"
    assert not skills.token("gmail").exists()


def test_user_config_survives(skills):
    scripts = skills.link("gmail")
    (scripts / ".env").write_text("GOOGLE_CLIENT_ID=mine\n", encoding="utf-8")
    before = (scripts / ".env").read_bytes()

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert (scripts / ".env").read_bytes() == before
    assert ".env" not in out["removed"]


def test_the_listener_lock_is_never_deleted(skills):
    scripts = skills.link("gcalendar")
    lock = scripts / ".listener.lock"
    lock.write_text("", encoding="utf-8")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert lock.exists()
    assert ".listener.lock" not in out["removed"]


def test_event_payloads_from_the_unlinked_account_are_removed(skills):
    """They hold calendar entries / file names read out of that account, and the
    backend's unlink removes them — the two halves have to agree."""
    scripts = skills.link("gcalendar")
    folder = scripts.parent / "events" / "event_changed"
    folder.mkdir(parents=True)
    (folder / "one.md").write_text("# private calendar content\n", encoding="utf-8")
    (folder / "two.md").write_text("# more\n", encoding="utf-8")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert "events/event_changed/one.md" in out["removed"]
    assert "events/event_changed/two.md" in out["removed"]
    assert list(folder.glob("*.md")) == []
    # The folder itself stays: the listener writes into it on the next link.
    assert folder.is_dir()


def test_event_payloads_are_swept_even_with_no_token(skills):
    scripts = skills.scripts("gcalendar")
    folder = scripts.parent / "events" / "event_changed"
    folder.mkdir(parents=True)
    (folder / "orphan.md").write_text("# leftover\n", encoding="utf-8")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert out["reason"] == "not_linked"
    assert "events/event_changed/orphan.md" in out["removed"]


def test_non_payload_files_under_events_are_left_alone(skills):
    """Only the ``*.md`` drop-zone payloads are ours to remove."""
    scripts = skills.link("gcalendar")
    folder = scripts.parent / "events" / "event_changed"
    folder.mkdir(parents=True)
    keep = folder / "README.txt"
    keep.write_text("not a payload", encoding="utf-8")

    auth.unlink(token_path=scripts / ".google_token.json")

    assert keep.exists()


def test_a_skill_with_no_events_folder_is_unaffected(skills):
    scripts = skills.link("gmail")

    out = auth.unlink(token_path=scripts / ".google_token.json")

    assert out["unlinked"] is True
    assert not any(name.startswith("events/") for name in out["removed"])


def test_a_cleaned_sibling_loses_its_event_payloads_too(skills):
    skills.link("gmail")
    sibling = skills.link("gcalendar")
    folder = sibling.parent / "events" / "event_changed"
    folder.mkdir(parents=True)
    (folder / "one.md").write_text("# private\n", encoding="utf-8")

    out = auth.unlink(token_path=skills.token("gmail"))

    entry = out["siblings"][0]
    assert entry["cleaned"] is True
    assert "events/event_changed/one.md" in entry["removed"]
    assert list(folder.glob("*.md")) == []


def test_the_credential_file_list_excludes_config_and_the_lock():
    assert ".env" not in auth.CREDENTIAL_FILES
    assert ".listener.lock" not in auth.CREDENTIAL_FILES
    assert ".listener_heartbeat" not in auth.CREDENTIAL_FILES


# ── ordering ─────────────────────────────────────────────────────────────────

def test_before_revoke_runs_before_the_revoke(skills):
    """``channels.stop`` needs a live credential, so this order is load-bearing."""
    scripts = skills.link("gcalendar")

    def stop_watch(_data):
        skills.events.append("watch")
        return {"watch_stopped": True}

    out = auth.unlink(token_path=scripts / ".google_token.json", before_revoke=stop_watch)

    assert skills.events == ["watch", "revoke"]
    assert out["pre_revoke"] == {"watch_stopped": True}


def test_a_raising_before_revoke_never_blocks_the_wipe(skills):
    scripts = skills.link("gcalendar")

    def stop_watch(_data):
        raise RuntimeError("Google said 403")

    out = auth.unlink(token_path=scripts / ".google_token.json", before_revoke=stop_watch)

    assert out["pre_revoke_error"] == "Google said 403"
    assert out["revoked"] is True  # the revoke was still attempted
    assert out["unlinked"] is True
    assert not skills.token("gcalendar").exists()


def test_before_revoke_receives_the_token_data(skills):
    scripts = skills.link("gmail", email="who@example.com")
    seen: List[Any] = []

    auth.unlink(
        token_path=scripts / ".google_token.json",
        before_revoke=lambda data: seen.append(data.get("email")),
    )

    assert seen == ["who@example.com"]


# ── siblings ─────────────────────────────────────────────────────────────────

def test_a_sibling_on_the_same_grant_is_found(skills):
    skills.link("gmail")
    skills.link("gcalendar")

    found = auth.find_sibling_accounts(
        skills.token("gmail"), auth.TokenStore(skills.token("gmail")).load()
    )

    assert [entry["skill"] for entry in found] == ["gcalendar"]


def test_a_skill_never_matches_itself(skills):
    skills.link("gmail")

    found = auth.find_sibling_accounts(
        skills.token("gmail"), auth.TokenStore(skills.token("gmail")).load()
    )

    assert found == []


def test_a_different_oauth_client_is_not_a_sibling(skills):
    skills.link("gmail", client_id="shared-cid")
    skills.link("gcalendar", client_id="my-own-cid")

    found = auth.find_sibling_accounts(
        skills.token("gmail"), auth.TokenStore(skills.token("gmail")).load()
    )

    assert found == []


def test_a_different_account_is_not_a_sibling(skills):
    skills.link("gmail", email="work@example.com", account_key="ak-work")
    skills.link("gcalendar", email="home@example.com", account_key="ak-home")

    found = auth.find_sibling_accounts(
        skills.token("gmail"), auth.TokenStore(skills.token("gmail")).load()
    )

    assert found == []


def test_a_token_without_an_account_key_matches_on_the_derived_one(skills):
    """Covers a hand-edited or pre-``account_key`` file."""
    skills.link("gmail", email="same@example.com", account_key=None)
    skills.link("gcalendar", email="same@example.com", account_key=None)

    found = auth.find_sibling_accounts(
        skills.token("gmail"), auth.TokenStore(skills.token("gmail")).load()
    )

    assert [entry["skill"] for entry in found] == ["gcalendar"]


def test_no_client_id_claims_no_siblings(skills):
    skills.link("gmail", client_id="")
    skills.link("gcalendar", client_id="")

    found = auth.find_sibling_accounts(
        skills.token("gmail"), auth.TokenStore(skills.token("gmail")).load()
    )

    assert found == []


def test_siblings_are_cleaned_only_after_a_successful_revoke(skills):
    """Their tokens are provably dead then; leaving them makes `status` lie."""
    skills.link("gmail")
    sibling = skills.link("gcalendar")
    (sibling / ".listener_state.json").write_text('{"channel_id": "cm-x"}', encoding="utf-8")

    out = auth.unlink(token_path=skills.token("gmail"))

    entry = out["siblings"][0]
    assert entry["skill"] == "gcalendar"
    assert entry["cleaned"] is True
    assert entry["orphaned_watch"] is True
    assert not skills.token("gcalendar").exists()
    assert not (sibling / ".listener_state.json").exists()
    # The sibling's user config is not ours to delete either.
    assert (sibling / ".env").exists()


def test_no_revoke_reports_siblings_but_never_touches_them(skills):
    skills.link("gmail")
    skills.link("gcalendar")

    out = auth.unlink(token_path=skills.token("gmail"), revoke_at_google=False)

    assert out["siblings"][0]["cleaned"] is False
    assert skills.token("gcalendar").exists()


def test_a_failed_revoke_never_touches_siblings(skills):
    skills.response = (500, "boom")
    skills.link("gmail")
    skills.link("gcalendar")

    out = auth.unlink(token_path=skills.token("gmail"))

    assert out["siblings"][0]["cleaned"] is False
    assert skills.token("gcalendar").exists()


def test_keep_siblings_reports_without_cleaning(skills):
    skills.link("gmail")
    skills.link("gcalendar")

    out = auth.unlink(token_path=skills.token("gmail"), clean_siblings=False)

    assert out["siblings"][0]["cleaned"] is False
    assert skills.token("gcalendar").exists()


# ── the listener lock ────────────────────────────────────────────────────────

def test_no_lock_path_means_no_listener(skills):
    assert auth.listener_is_running(None) is False


def test_an_unheld_lock_means_no_listener(skills):
    scripts = skills.scripts("gcalendar")

    assert auth.listener_is_running(scripts / ".listener.lock") is False


def test_probing_the_lock_never_deletes_it(skills):
    scripts = skills.scripts("gcalendar")
    lock = scripts / ".listener.lock"

    auth.listener_is_running(lock)

    assert lock.exists()


# ── unlink_preview ───────────────────────────────────────────────────────────

def test_the_preview_describes_the_link_without_touching_it(skills):
    skills.link("gmail")
    skills.link("gcalendar")
    before = skills.token("gmail").read_bytes()

    preview = auth.unlink_preview(skills.token("gmail"))

    assert preview["linked"] is True
    assert preview["email"] == "u@example.com"
    assert [entry["skill"] for entry in preview["siblings"]] == ["gcalendar"]
    assert ".google_token.json" in preview["will_remove"]
    assert preview["listener_running"] is False
    # Nothing moved.
    assert skills.token("gmail").read_bytes() == before
    assert skills.token("gcalendar").exists()


def test_the_preview_makes_no_network_call(skills, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("unlink_preview must not reach the network")

    monkeypatch.setattr(auth, "_post_form", boom)
    skills.link("gmail")

    auth.unlink_preview(skills.token("gmail"))


def test_the_preview_of_an_unlinked_skill(skills):
    scripts = skills.scripts("gmail")

    preview = auth.unlink_preview(scripts / ".google_token.json")

    assert preview["linked"] is False
    assert preview["siblings"] == []
    assert preview["will_remove"] == []


def test_the_preview_flags_an_unknowable_blast_radius(skills):
    """Without a client id we cannot tell which siblings share the grant."""
    skills.link("gmail", client_id="")

    preview = auth.unlink_preview(skills.token("gmail"))

    assert preview["siblings_unknown"] is True
