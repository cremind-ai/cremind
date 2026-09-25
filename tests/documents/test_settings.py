"""Documentation search settings: root validation, option hygiene, change plans.

The root check is the security boundary of the whole feature. A profile's
index is readable by that profile's agent, so the folder it covers decides
what that agent can see: Cremind's own system folder (tokens, every profile's
data) must never be a root, a symlink must not be a way out of the working
directory, and a non-admin profile must stay inside the working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config.settings import BaseConfig
from app.documents import settings as uds


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch):
    wd = tmp_path / "work"
    sysdir = tmp_path / "system"
    outside = tmp_path / "elsewhere"
    for d in (wd, sysdir, outside, wd / "Reports"):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.setattr(uds, "get_user_working_directory", lambda: str(wd))
    return {"wd": wd, "sys": sysdir, "outside": outside}


# ── validate_root ──────────────────────────────────────────────────────────


def test_inherit_resolves_to_the_working_directory(dirs):
    check = uds.validate_root(None, is_admin=False)
    assert check.ok
    assert check.path == os.path.realpath(dirs["wd"])


def test_a_member_may_pick_a_folder_inside_the_working_directory(dirs):
    check = uds.validate_root(str(dirs["wd"] / "Reports"), is_admin=False)
    assert check.ok, check.message


def test_a_member_may_not_leave_the_working_directory(dirs):
    check = uds.validate_root(str(dirs["outside"]), is_admin=False)
    assert not check.ok
    assert check.code == "outside_working_dir"


def test_the_admin_may_pick_any_ordinary_folder(dirs):
    assert uds.validate_root(str(dirs["outside"]), is_admin=True).ok


@pytest.mark.parametrize("admin", [True, False])
def test_the_system_folder_is_refused_for_everyone(dirs, admin):
    (dirs["sys"] / "admin").mkdir()
    check = uds.validate_root(str(dirs["sys"] / "admin"), is_admin=admin)
    assert not check.ok
    assert check.code == "inside_system_dir"


def test_a_working_directory_that_is_the_system_folder_is_refused(dirs, monkeypatch):
    """The built-in setup profile defaults the working dir to ~/.cremind."""
    monkeypatch.setattr(uds, "get_user_working_directory", lambda: str(dirs["sys"]))
    check = uds.validate_root(None, is_admin=True)
    assert not check.ok
    assert check.code == "inside_system_dir"
    assert "working directory" in (check.message or "")


def test_a_root_containing_the_system_folder_locks_it_out(dirs, monkeypatch):
    parent = dirs["sys"].parent
    monkeypatch.setattr(uds, "get_user_working_directory", lambda: str(parent))
    check = uds.validate_root(None, is_admin=True)
    assert check.ok
    assert os.path.realpath(dirs["sys"]) in check.locked_excludes


def test_missing_and_file_roots_are_refused(dirs):
    assert uds.validate_root(str(dirs["wd"] / "nope"), is_admin=True).code == "not_found"
    f = dirs["wd"] / "a.txt"
    f.write_text("x", encoding="utf-8")
    assert uds.validate_root(str(f), is_admin=True).code == "not_directory"


def test_a_symlink_cannot_escape_the_working_directory(dirs):
    link = dirs["wd"] / "escape"
    try:
        os.symlink(dirs["outside"], link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    check = uds.validate_root(str(link), is_admin=False)
    assert not check.ok
    assert check.code == "outside_working_dir"


def test_operating_system_locations_are_refused_even_for_admin():
    root = os.path.abspath(os.sep)
    check = uds.validate_root(root, is_admin=True)
    assert not check.ok
    assert check.code in ("forbidden_system_path", "inside_system_dir")


# ── options and excludes ───────────────────────────────────────────────────


def test_defaults_keep_channels_and_rooms_off():
    """A channel in open mode answers anyone; rooms fan answers out to others."""
    opts = uds.normalize_options(None)
    assert opts["allow_in"] == {"web_cli": True, "channels": False, "rooms": False}
    assert opts["caption_consent"] is None


def test_options_merge_onto_the_saved_value_and_drop_junk():
    base = uds.normalize_options({"caption": {"min_px": 512}})
    out = uds.normalize_options(
        {
            "caption": {"min_px": "99999", "daily_cap": "250"},
            "allow_in": {"channels": "true", "bogus": True},
            "observer": {"mode": "carrier-pigeon"},
            "unknown_key": 1,
        },
        base=base,
    )
    assert out["caption"]["min_px"] == 4096  # clamped
    assert out["caption"]["daily_cap"] == 250
    assert out["allow_in"]["channels"] is True
    assert "bogus" not in out["allow_in"]
    assert out["observer"]["mode"] == "auto"  # invalid mode kept the old one
    assert "unknown_key" not in out


def test_consent_needs_a_provider_and_a_model():
    assert uds.normalize_options({"caption_consent": {"provider": "openai"}})["caption_consent"] is None
    ok = uds.normalize_options({"caption_consent": {"provider": "openai", "model": "gpt-4o", "at": 1}})
    assert ok["caption_consent"]["model"] == "gpt-4o"


def test_excludes_are_deduplicated_and_normalized():
    rules = uds.normalize_excludes([
        "Archive/**",
        {"pattern": "Archive/**"},
        {"pattern": "*.ISO", "type": "ext", "mode": "metadata_only"},
        {"pattern": "", "type": "glob"},
        {"pattern": "x", "type": "regex"},
        42,
    ])
    assert {"pattern": "Archive/**", "type": "glob", "mode": "skip"} in rules
    assert {"pattern": "iso", "type": "ext", "mode": "metadata_only"} in rules
    assert {"pattern": "x", "type": "glob", "mode": "skip"} in rules  # unknown type → glob
    assert len(rules) == 3


# ── admin policy ───────────────────────────────────────────────────────────


class _FakeConfig:
    def __init__(self):
        self.rows: dict[str, str] = {}

    def set(self, table, key, value, **_kw):
        assert table == "server_config"
        self.rows[key] = value


def test_admin_policy_round_trips_and_rejects_bad_input(monkeypatch):
    store = _FakeConfig()
    monkeypatch.setattr(uds, "get_dynamic", lambda table, key, *a, **k: store.rows.get(key))

    policy = uds.write_admin_policy({"allowed": True, "storage_budget_mb": "2048"}, store)
    assert policy.allowed is True
    assert policy.storage_budget_mb == 2048
    assert store.rows["documentation_search.allowed"] == "true"

    with pytest.raises(uds.PolicyValidationError) as exc:
        uds.write_admin_policy({"workers": 99, "typo": 1}, store)
    assert set(exc.value.details) == {"workers", "typo"}
    # Nothing from a rejected write reaches storage.
    assert "documentation_search.workers" not in store.rows


def test_feature_effective_names_the_reason(monkeypatch):
    monkeypatch.setattr(BaseConfig, "is_embedding_enabled", classmethod(lambda cls: False))
    assert uds.feature_effective(uds.AdminPolicy(allowed=False)) == (False, "admin_gate_off")
    assert uds.feature_effective(uds.AdminPolicy(allowed=True)) == (False, "embedding_disabled")
    monkeypatch.setattr(BaseConfig, "is_embedding_enabled", classmethod(lambda cls: True))
    assert uds.feature_effective(uds.AdminPolicy(allowed=True)) == (True, None)


# ── change plans ───────────────────────────────────────────────────────────


def test_turning_drive_off_is_destructive_but_pausing_local_is_not():
    drive = uds.plan_source_change("p", "drive", {"enabled": True, "updated_at": 1}, {"enabled": False})
    assert drive.destructive
    local = uds.plan_source_change("p", "local", {"enabled": True, "updated_at": 1}, {"enabled": False})
    assert not local.destructive


def test_moving_the_root_or_deleting_the_index_needs_confirmation():
    cur = {"enabled": True, "root_path": "/a", "updated_at": 1}
    assert uds.plan_source_change("p", "local", cur, {"root_path": "/b"}).destructive
    assert uds.plan_source_change("p", "local", cur, {"root_path": "/a"}).destructive is False
    assert uds.plan_source_change("p", "local", cur, {"enabled": False, "delete_index": True}).destructive


def test_the_token_is_bound_to_the_change_and_the_row_version():
    cur = {"enabled": True, "root_path": "/a", "updated_at": 1}
    a = uds.plan_source_change("p", "local", cur, {"root_path": "/b"}).token
    assert a == uds.plan_source_change("p", "local", cur, {"root_path": "/b"}).token
    assert a != uds.plan_source_change("p", "local", cur, {"root_path": "/c"}).token
    assert a != uds.plan_source_change("p", "local", {**cur, "updated_at": 2}, {"root_path": "/b"}).token
    assert a != uds.plan_source_change("q", "local", cur, {"root_path": "/b"}).token


def test_with_real_counts_an_empty_purge_needs_no_dialog(monkeypatch):
    monkeypatch.setattr(uds, "_effect_counter", lambda p, k, what, ctx: uds.Effect(what, files=0))
    cur = {"enabled": True, "root_path": "/a", "updated_at": 1}
    assert not uds.plan_source_change("p", "local", cur, {"root_path": "/b"}).destructive
    monkeypatch.setattr(uds, "_effect_counter", lambda p, k, what, ctx: uds.Effect(what, files=12))
    assert uds.plan_source_change("p", "local", cur, {"root_path": "/b"}).destructive
