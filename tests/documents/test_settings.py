"""Documentation search settings: root validation, option hygiene, change plans.

The root check is the security boundary of the whole feature. A profile's
index is readable by that profile's agent, so the folder it covers decides
what that agent can see. The folder is always the profile's own working
directory; the check makes sure that is safe to index: Cremind's own system
folder (tokens, every profile's data) is never a root — except for a
workspace that is the profile's own under ``<SYS>/workspaces`` (its default,
or the ``<profile>-2`` sibling it was given) — a symlink is no way into it,
and another profile's working directory inside the folder is locked out of
the walk. Without the profile list (storage failing) no root is approved.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import working_dirs
from app.config.settings import BaseConfig
from app.documents import settings as uds
from app.documents.discovery.ignore import INDEX, SKIP, IgnoreMatcher

from tests.documents._workspaces import install


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch):
    sysdir = tmp_path / "system"
    outside = tmp_path / "elsewhere"
    for d in (sysdir, outside):
        d.mkdir(parents=True, exist_ok=True)
    store = install(monkeypatch, sysdir, {"admin": None, "dog": None})
    return {"sys": sysdir, "outside": outside, "store": store, "tmp": tmp_path}


def _n(path) -> str:
    return os.path.normcase(os.path.realpath(path))


# ── validate_root ──────────────────────────────────────────────────────────


def test_the_root_is_the_profiles_own_working_directory(dirs):
    check = uds.validate_root("dog")
    assert check.ok, check.message
    assert check.path == os.path.realpath(dirs["sys"] / "workspaces" / "dog")
    assert uds.validate_root("admin").path == os.path.realpath(dirs["sys"] / "workspaces" / "admin")


def test_the_own_default_workspace_is_the_one_folder_in_the_system_dir_that_indexes(dirs):
    """It sits inside the system folder, yet it is the profile's own space:
    accepted, and the walker's backstop is told so — every file in it indexes,
    nothing outside it does."""
    check = uds.validate_root("dog")
    assert check.ok and check.locked_excludes == []
    assert uds.system_dir_exempt(check.path)
    (Path(check.path) / "notes.md").write_text("x", encoding="utf-8")
    m = IgnoreMatcher(check.path, locked_excludes=check.locked_excludes, system_dir=uds.system_dir(),
                      root_sanctioned=uds.system_dir_exempt(check.path))
    assert m.classify("notes.md") == INDEX
    # Without the exemption the backstop indexes nothing under the system dir.
    locked = IgnoreMatcher(check.path, system_dir=uds.system_dir())
    assert locked.classify("notes.md") == SKIP


def test_an_explicit_folder_outside_the_system_dir_is_accepted(dirs):
    dirs["store"].rows["dog"] = str(dirs["outside"])
    check = uds.validate_root("dog")
    assert check.ok, check.message
    assert check.path == os.path.realpath(dirs["outside"])
    assert not uds.system_dir_exempt(check.path)


@pytest.mark.parametrize("where", ["storage", "workspaces/admin", ".", "workspaces"])
def test_any_other_folder_in_the_system_dir_is_refused(dirs, where):
    """Only the profile's OWN default location: not another profile's
    workspace, not the system dir itself, not its storage."""
    target = dirs["sys"] / where
    target.mkdir(parents=True, exist_ok=True)
    dirs["store"].rows["dog"] = str(target)
    check = uds.validate_root("dog")
    assert not check.ok
    assert check.code == "inside_system_dir"
    assert "working directory" in (check.message or "")


def test_the_admin_is_not_exempt(dirs):
    (dirs["sys"] / "workspaces" / "dog").mkdir(parents=True)
    dirs["store"].rows["admin"] = str(dirs["sys"] / "workspaces" / "dog")
    check = uds.validate_root("admin")
    assert not check.ok and check.code == "inside_system_dir"


def test_a_deleted_profiles_default_folder_is_nobodys(dirs):
    """A default location whose profile no longer exists belongs to nobody."""
    check = uds.validate_root("ghost")
    assert not check.ok
    assert check.code == "inside_system_dir"


def test_a_workspace_that_would_be_the_index_store_is_refused(dirs, monkeypatch):
    """An odd CREMIND_WORKSPACES_DIR must not turn the index store (every
    profile's indexed text) into some profile's "default workspace"."""
    monkeypatch.setenv(working_dirs.WORKSPACES_ENV, str(dirs["sys"] / "storage"))
    dirs["store"].rows["documents"] = None
    working_dirs.invalidate()
    check = uds.validate_root("documents")
    assert not check.ok
    assert check.code == "inside_system_dir"


def test_a_symlink_cannot_lead_into_the_system_folder(dirs):
    (dirs["sys"] / "storage").mkdir()
    link = dirs["outside"] / "shortcut"
    try:
        os.symlink(dirs["sys"] / "storage", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    dirs["store"].rows["dog"] = str(link)
    check = uds.validate_root("dog")
    assert not check.ok
    assert check.code == "inside_system_dir"


def test_a_root_containing_the_system_folder_locks_it_out(dirs):
    dirs["store"].rows["admin"] = str(dirs["tmp"])
    check = uds.validate_root("admin")
    assert check.ok, check.message
    assert _n(dirs["sys"]) in {_n(p) for p in check.locked_excludes}


def test_other_profiles_folders_inside_the_root_are_locked_out(dirs, monkeypatch):
    """An upgraded admin keeps its legacy folder, which may hold the
    workspaces root (the containers put it in the documents mount). The
    admin's index never reaches another profile's workspace there, while its
    own files still index."""
    legacy = dirs["tmp"] / "Documents"
    monkeypatch.setenv(working_dirs.WORKSPACES_ENV, str(legacy / "workspaces"))
    dirs["store"].rows["admin"] = str(legacy)
    working_dirs.invalidate()
    dog = Path(uds.validate_root("dog").path)
    (dog / "secret.txt").write_text("dog's", encoding="utf-8")
    (legacy / "notes.txt").write_text("admin's", encoding="utf-8")
    (legacy / "workspaces" / "stray").mkdir(parents=True)

    check = uds.validate_root("admin")
    assert check.ok, check.message
    locked = {_n(p) for p in check.locked_excludes}
    assert _n(dog) in locked
    assert _n(legacy / "workspaces" / "stray") in locked, "an unowned entry is nobody's"
    m = IgnoreMatcher(check.path, locked_excludes=check.locked_excludes, system_dir=uds.system_dir())
    assert m.classify("workspaces/dog/secret.txt") == SKIP
    assert m.classify("notes.txt") == INDEX
    # And dog's own index covers its workspace, with nothing locked in it.
    own = uds.validate_root("dog")
    assert own.ok and own.path == os.path.realpath(dog)
    assert own.locked_excludes == []


def test_a_sibling_workspace_the_profile_was_given_is_its_own_to_index(dirs):
    """``dog``'s default held someone's files when it was created, so it got
    ``<SYS>/workspaces/dog-2`` — inside the system folder, and its own: it
    indexes, exactly like a default. Another profile's is still refused."""
    sibling = dirs["sys"] / "workspaces" / "dog-2"
    sibling.mkdir(parents=True)
    dirs["store"].rows["dog"] = str(sibling)
    working_dirs.invalidate()
    check = uds.validate_root("dog")
    assert check.ok, check.message
    assert check.path == os.path.realpath(sibling)
    assert uds.system_dir_exempt(check.path)
    dirs["store"].rows["admin"] = str(sibling)  # shared with admin: no longer dog's alone
    working_dirs.invalidate()
    assert uds.validate_root("dog").code == "inside_system_dir"
    assert uds.validate_root("admin").code == "inside_system_dir"


class _FailingStore:
    """Storage is configured, and listing the profiles raises."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def profile_working_dirs(self):
        raise RuntimeError("database is locked")


def test_a_root_is_refused_while_ownership_cannot_be_read(dirs, monkeypatch):
    """Without the profile list the locked excludes cannot be known — an
    admin's folder holding the workspaces root would index them all."""
    import importlib

    cfg = importlib.import_module("app.config.settings")
    monkeypatch.setattr(cfg, "_dynamic_config_storage", _FailingStore(dirs["store"]))
    monkeypatch.setattr(working_dirs, "_last_good", None)
    working_dirs.invalidate()
    for who in ("admin", "dog"):
        check = uds.validate_root(who)
        assert not check.ok and check.code == "ownership_unavailable", who
    monkeypatch.setattr(cfg, "_dynamic_config_storage", dirs["store"])
    assert uds.validate_root("dog").ok, "the next configure succeeds once storage answers"


def test_a_root_without_storage_at_all_still_validates(dirs, monkeypatch):
    """Setup mode / the offline CLI: no storage is not a failure; the own
    default is still the one folder in the system dir that indexes."""
    import importlib

    cfg = importlib.import_module("app.config.settings")
    monkeypatch.setattr(cfg, "_dynamic_config_storage", None)
    working_dirs.invalidate()
    assert uds.validate_root("dog").ok


def test_missing_and_file_roots_are_refused(dirs, monkeypatch):
    f = dirs["outside"] / "a.txt"
    f.write_text("x", encoding="utf-8")
    dirs["store"].rows["dog"] = str(f)
    assert uds.validate_root("dog").code == "not_directory"
    missing = dirs["outside"] / "nope"
    monkeypatch.setattr(uds, "get_user_working_directory", lambda profile: str(missing))
    assert uds.validate_root("dog").code == "not_found"


def test_no_profile_is_no_root(dirs):
    assert uds.validate_root("").code == "invalid_profile"


def test_operating_system_locations_are_refused_even_for_admin(dirs):
    dirs["store"].rows["admin"] = os.path.abspath(os.sep)
    check = uds.validate_root("admin")
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


def test_the_folder_never_moves_through_a_settings_plan():
    """The folder is the working directory; a moved one is confirmed through
    the engine's pending_root_change hold, never a settings save."""
    cur = {"enabled": True, "root_path": "/a", "updated_at": 1}
    assert not uds.plan_source_change("p", "local", cur, {"root_path": "/b"}).destructive
    assert uds.plan_source_change("p", "local", cur, {"enabled": False, "delete_index": True}).destructive


def test_new_excludes_need_confirmation():
    cur = {"enabled": True, "root_path": "/a", "updated_at": 1}
    assert uds.plan_source_change("p", "local", cur, {"excludes": ["Archive/**"]}).destructive
    old = {**cur, "excludes": [{"pattern": "Archive/**"}]}
    assert not uds.plan_source_change("p", "local", old, {"excludes": ["Archive/**"]}).destructive


def test_the_token_is_bound_to_the_change_and_the_row_version():
    cur = {"enabled": True, "root_path": "/a", "updated_at": 1}
    a = uds.plan_source_change("p", "local", cur, {"excludes": ["b/**"]}).token
    assert a == uds.plan_source_change("p", "local", cur, {"excludes": ["b/**"]}).token
    assert a != uds.plan_source_change("p", "local", cur, {"excludes": ["c/**"]}).token
    assert a != uds.plan_source_change("p", "local", {**cur, "updated_at": 2}, {"excludes": ["b/**"]}).token
    assert a != uds.plan_source_change("q", "local", cur, {"excludes": ["b/**"]}).token


def test_with_real_counts_an_empty_purge_needs_no_dialog(monkeypatch):
    monkeypatch.setattr(uds, "_effect_counter", lambda p, k, what, ctx: uds.Effect(what, files=0))
    cur = {"enabled": True, "root_path": "/a", "updated_at": 1}
    assert not uds.plan_source_change("p", "local", cur, {"excludes": ["b/**"]}).destructive
    monkeypatch.setattr(uds, "_effect_counter", lambda p, k, what, ctx: uds.Effect(what, files=12))
    assert uds.plan_source_change("p", "local", cur, {"excludes": ["b/**"]}).destructive
