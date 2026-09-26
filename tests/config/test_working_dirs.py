"""Per-profile working directories: resolution, ownership, validation, lifecycle."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from app.config import working_dirs as wd

# By module path: ``app.config.settings`` the attribute is a Dynaconf object.
cfg = importlib.import_module("app.config.settings")


class _Store:
    """The three DynamicConfigStorage methods working_dirs uses."""

    def __init__(self, rows: dict[str, str | None]):
        self.rows = dict(rows)

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        if profile not in self.rows:
            return False
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    store = _Store({"admin": None, "javis": None, "__server__": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    wd.invalidate()
    yield sysdir, store
    wd.invalidate()


def test_each_profile_defaults_to_its_own_folder_under_workspaces(env) -> None:
    sysdir, _ = env
    admin = cfg.get_user_working_directory("admin")
    javis = cfg.get_user_working_directory("javis")
    assert Path(admin) == sysdir / "workspaces" / "admin"
    assert Path(javis) == sysdir / "workspaces" / "javis"
    assert os.path.isdir(admin) and os.path.isdir(javis), "resolution creates the folder"


def test_a_profile_is_required(env) -> None:
    with pytest.raises(ValueError):
        cfg.get_user_working_directory("")


def test_the_admins_choice_wins_and_tilde_expands(env, tmp_path: Path, monkeypatch) -> None:
    _, store = env
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    store.rows["admin"] = "~/Documents"
    assert Path(cfg.get_user_working_directory("admin")) == home / "Documents"


def test_the_env_var_moves_the_workspaces_root(env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(tmp_path / "mount" / "workspaces"))
    assert Path(cfg.get_user_working_directory("javis")) == tmp_path / "mount" / "workspaces" / "javis"


def test_a_profile_cannot_reach_another_profiles_folder(env) -> None:
    admin = cfg.get_user_working_directory("admin")
    javis = cfg.get_user_working_directory("javis")
    secret = os.path.join(javis, "contract.pdf")
    assert wd.is_foreign(secret, "admin"), "admin is not exempt"
    assert not wd.is_foreign(secret, "javis")
    assert wd.is_foreign(os.path.join(admin, "x.txt"), "javis")
    assert not wd.is_foreign(os.path.join(admin, "x.txt"), "admin")
    assert wd.is_foreign(secret, None), "no caller = nobody"


def test_ordinary_paths_and_the_root_itself_are_not_owned(env, tmp_path: Path) -> None:
    assert wd.owners_of(str(tmp_path / "elsewhere")) is None
    assert wd.owners_of(wd.workspaces_root()) is None


def test_an_entry_no_profile_owns_is_nobodys(env) -> None:
    stray = os.path.join(wd.workspaces_root(), ".deleted", "bob-20260101", "a.txt")
    assert wd.owners_of(stray) == frozenset()
    assert wd.is_foreign(stray, "admin") and wd.is_foreign(stray, "javis")


def test_a_legacy_admin_folder_holding_the_workspaces_keeps_its_own_files(env, tmp_path: Path, monkeypatch) -> None:
    """Containers: admin kept /root/Documents, the workspaces root sits inside it."""
    _, store = env
    docs = tmp_path / "Documents"
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(docs / "workspaces"))
    store.rows["admin"] = str(docs)
    wd.invalidate()
    javis = cfg.get_user_working_directory("javis")
    assert not wd.is_foreign(str(docs / "notes.txt"), "admin")
    assert wd.is_foreign(str(docs / "notes.txt"), "javis")
    assert wd.is_foreign(os.path.join(javis, "a.txt"), "admin"), "the deeper zone wins"
    assert not wd.is_foreign(os.path.join(javis, "a.txt"), "javis")
    assert wd.foreign_dirs_inside(str(docs), "admin") == [os.path.normcase(os.path.realpath(javis))]


def test_two_profiles_pointed_at_one_folder_share_it(env, tmp_path: Path) -> None:
    _, store = env
    shared = tmp_path / "team"
    shared.mkdir()
    store.rows["admin"] = str(shared)
    store.rows["javis"] = str(shared)
    wd.invalidate()
    assert wd.owners_of(str(shared / "plan.md")) == frozenset({"admin", "javis"})
    assert not wd.is_foreign(str(shared / "plan.md"), "javis")


def test_ownership_is_unknown_without_storage(env, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_dynamic_config_storage", None)
    wd.invalidate()
    assert wd.owners_of(os.path.join(wd.workspaces_root(), "javis", "a")) is None
    assert Path(cfg.get_user_working_directory("javis")).name == "javis"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("relative/path", "not_absolute"),
        ("{sys}/storage", "inside_system_dir"),
        ("{sys}/workspaces/javis", "inside_system_dir"),
    ],
)
def test_validation_refuses(env, raw: str, code: str) -> None:
    sysdir, _ = env
    check = wd.validate_working_dir("admin", raw.format(sys=sysdir))
    assert not check.ok and check.code == code


def test_validation_accepts_the_default_and_outside_folders(env, tmp_path: Path) -> None:
    assert wd.validate_working_dir("javis", None).is_default
    assert wd.validate_working_dir("javis", wd.default_working_dir("javis")).is_default
    outside = tmp_path / "work"
    check = wd.validate_working_dir("javis", str(outside), create=True)
    assert check.ok and not check.is_default and outside.is_dir()


def test_validation_refuses_a_folder_inside_the_env_workspaces_root(env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(tmp_path / "mount"))
    check = wd.validate_working_dir("admin", str(tmp_path / "mount" / "javis" / "x"))
    assert not check.ok and check.code == "inside_workspaces"


def test_validation_refuses_carving_into_another_profiles_folder_but_allows_sharing_it(env, tmp_path: Path) -> None:
    _, store = env
    team = tmp_path / "team"
    team.mkdir()
    store.rows["admin"] = str(team)
    wd.invalidate()
    inner = wd.validate_working_dir("javis", str(team / "sub"))
    assert not inner.ok and inner.code == "inside_other_working_dir"
    assert wd.validate_working_dir("javis", str(team)).ok


def test_set_stores_null_for_the_default_and_notifies(env, tmp_path: Path) -> None:
    _, store = env
    seen: list[str] = []
    wd.add_change_listener(seen.append)
    try:
        wd.set_working_dir("javis", str(tmp_path / "w"))
        assert store.rows["javis"] == os.path.normpath(str(tmp_path / "w"))
        wd.set_working_dir("javis", wd.default_working_dir("javis"))
        assert store.rows["javis"] is None
    finally:
        wd.remove_change_listener(seen.append)
    assert seen == ["javis", "javis"]


def test_a_new_profile_never_adopts_a_folder_with_files(env) -> None:
    root = Path(wd.workspaces_root())
    assert wd.fresh_default_for_new_profile("bob") is None
    (root / "bob").mkdir(parents=True)
    assert wd.fresh_default_for_new_profile("bob") is None, "an empty folder is free"
    (root / "bob" / "old.txt").write_text("left behind")
    assert Path(wd.fresh_default_for_new_profile("bob")) == root / "bob-2"


def test_a_suffixed_folder_under_workspaces_is_its_profiles_own(env) -> None:
    _, store = env
    root = Path(wd.workspaces_root())
    (root / "javis-2").mkdir(parents=True)
    store.rows["javis"] = str(root / "javis-2")
    wd.invalidate()
    assert not wd.is_foreign(str(root / "javis-2" / "a.txt"), "javis")
    assert wd.is_foreign(str(root / "javis-2" / "a.txt"), "admin")
    assert wd.foreign_dirs_inside(str(root), "javis") == [
        os.path.normcase(os.path.realpath(root / "admin"))
    ], "only admin's folder; javis-2 is javis's own"
    assert os.path.normcase(os.path.realpath(root / "javis-2")) in wd.foreign_dirs_inside(str(root), "admin")


def test_deleting_a_profile_archives_its_default_folder(env) -> None:
    folder = Path(cfg.get_user_working_directory("javis"))
    (folder / "keep.txt").write_text("mine")
    moved = wd.archive_default_dir("javis", stamp="20260929-120000")
    assert moved and Path(moved) == Path(wd.deleted_root()) / "javis-20260929-120000"
    assert (Path(moved) / "keep.txt").read_text() == "mine"
    assert not folder.exists()
    assert wd.archive_default_dir("javis") is None, "nothing left to move"


def test_invalid_profile_names_never_become_paths() -> None:
    for bad in ("", ".", "..", "a/b", "a\\b", "__server__", ".deleted"):
        assert not wd.valid_profile_dirname(bad)
        with pytest.raises(ValueError):
            wd.default_working_dir(bad)


# ── The system directory is never a zone's ─────────────────────────────────


def test_a_legacy_admin_folder_holding_the_system_dir_does_not_own_the_profiles_slices(env, tmp_path: Path) -> None:
    """An upgraded admin kept ``C:\\Users\\me`` (or ``~``), which holds
    ``~/.cremind``: every profile's skills, uploads and persona there stay each
    profile's own, not the admin's."""
    sysdir, store = env
    store.rows["admin"] = str(tmp_path)
    wd.invalidate()
    javis_skill = sysdir / "javis" / "skills" / "mine" / "SKILL.md"
    assert wd.owners_of(str(javis_skill)) is None, "the system-dir rules decide it"
    assert not wd.is_foreign(str(javis_skill), "javis")
    assert not wd.is_foreign(str(sysdir / "javis" / "uploads_tmp" / "a.png"), "javis")
    # The admin's own files and the workspaces are what they were.
    assert wd.owners_of(str(tmp_path / "notes.txt")) == frozenset({"admin"})
    javis = cfg.get_user_working_directory("javis")
    assert wd.is_foreign(os.path.join(javis, "a.txt"), "admin")
    (sysdir / "workspaces" / "admin").mkdir()  # its default from before, left behind
    assert wd.foreign_dirs_inside(str(tmp_path), "javis") == [
        os.path.normcase(os.path.realpath(sysdir / "workspaces" / "admin"))
    ], "the entry named after admin is locked; nothing of the system dir is"
    assert wd.validate_working_dir("javis", str(tmp_path / "javis-work")).ok is False, "inside admin's"
    assert wd.validate_working_dir("admin", str(tmp_path)).ok, "a folder holding the system dir is allowed"


def test_a_legacy_admin_folder_that_is_the_system_dir_owns_none_of_it(env) -> None:
    sysdir, store = env
    store.rows["admin"] = str(sysdir)  # the old ``~/.cremind`` default
    wd.invalidate()
    for slice_path in (sysdir / "javis" / "PERSONA.md", sysdir / "storage" / "x", sysdir):
        assert wd.owners_of(str(slice_path)) is None
    assert wd.owners_of(str(sysdir / "workspaces" / "javis" / "a")) == frozenset({"javis"})
    assert wd.owners_of(str(sysdir / "workspaces" / ".deleted" / "x")) == frozenset()


# ── Ownership when storage fails ───────────────────────────────────────────


class _Failing(_Store):
    def __init__(self, rows):
        super().__init__(rows)
        self.down = False

    def profile_working_dirs(self):
        if self.down:
            raise RuntimeError("database is locked")
        return super().profile_working_dirs()


def test_a_storage_error_with_no_earlier_reading_fails_closed(env, monkeypatch) -> None:
    sysdir, _ = env
    store = _Failing({"admin": None, "javis": None})
    store.down = True
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    monkeypatch.setattr(wd, "_last_good", None)
    wd.invalidate()
    secret = os.path.join(wd.workspaces_root(), "javis", "contract.pdf")
    assert wd.ownership_unavailable()
    assert wd.is_foreign(secret, "admin") and wd.is_foreign(secret, "javis"), "nobody, until it is known"
    assert not wd.is_foreign(str(sysdir.parent / "elsewhere" / "a.txt"), "javis"), "ordinary paths stay so"
    # Not cached: the moment storage answers again, so does ownership.
    store.down = False
    assert not wd.ownership_unavailable()
    assert not wd.is_foreign(secret, "javis") and wd.is_foreign(secret, "admin")


def test_a_storage_error_keeps_the_last_reading(env, monkeypatch) -> None:
    store = _Failing({"admin": None, "javis": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    monkeypatch.setattr(wd, "_last_good", None)
    wd.invalidate()
    secret = os.path.join(wd.workspaces_root(), "javis", "contract.pdf")
    assert wd.owners_of(secret) == frozenset({"javis"})
    store.down = True
    wd.invalidate()
    assert wd.owners_of(secret) == frozenset({"javis"}), "never 'nobody owns anything'"
    assert not wd.ownership_unavailable()


def test_no_storage_at_all_is_not_a_failure(env, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_dynamic_config_storage", None)
    monkeypatch.setattr(wd, "_last_good", None)
    wd.invalidate()
    assert not wd.ownership_unavailable()
    assert wd.owners_of(os.path.join(wd.workspaces_root(), "javis", "a")) is None


# ── A new profile's folder is nobody else's ────────────────────────────────


def test_a_new_profile_skips_a_sibling_that_is_a_live_profiles_default(env) -> None:
    """``dan``'s default holds files, and ``dan-2`` is a live profile: its
    folder — even missing or empty — is not ``dan``'s to take."""
    _, store = env
    store.rows["dan-2"] = None
    root = Path(wd.workspaces_root())
    (root / "dan").mkdir(parents=True)
    (root / "dan" / "old.txt").write_text("left behind")
    assert Path(wd.fresh_default_for_new_profile("dan")) == root / "dan-3"
    (root / "dan-2").mkdir()
    assert Path(wd.fresh_default_for_new_profile("dan")) == root / "dan-3"


def test_a_new_profiles_default_that_another_profile_was_given_is_taken(env) -> None:
    """``javis`` was given ``<ws>/javis-2``; a profile later created as
    ``javis-2`` must not share it, empty or not."""
    _, store = env
    root = Path(wd.workspaces_root())
    (root / "javis-2").mkdir(parents=True)
    store.rows["javis"] = str(root / "javis-2")
    store.rows["javis-2"] = None  # the row exists by the time its folder is chosen
    wd.invalidate()
    assert Path(wd.fresh_default_for_new_profile("javis-2")) == root / "javis-2-2"
    assert wd.fresh_default_for_new_profile("bob") is None, "an unclaimed name is still free"


# ── Share and device spellings (Windows) ───────────────────────────────────

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path spellings")


def _unc(path: str | Path, host: str = "localhost") -> str:
    drive, rest = os.path.splitdrive(os.path.realpath(str(path)))
    return f"\\\\{host}\\{drive[0]}${rest}"


@windows_only
def test_real_path_drops_the_literal_prefix() -> None:
    assert wd._plain_spelling("\\\\?\\C:\\Users\\me") == "C:\\Users\\me"
    assert wd._plain_spelling("\\\\.\\D:\\x") == "D:\\x"
    assert wd._plain_spelling("\\\\?\\C:") == "C:\\"
    assert wd._plain_spelling("\\\\?\\UNC\\host\\share\\x") == "\\\\host\\share\\x"
    assert wd._plain_spelling("\\\\?\\unc\\host\\share") == "\\\\host\\share"
    vol = "\\\\?\\Volume{0000-1111}\\x"
    assert wd._plain_spelling(vol) == vol, "left for the identity rule"


@windows_only
def test_a_literal_path_spelling_does_not_walk_around_a_zone(env) -> None:
    javis = cfg.get_user_working_directory("javis")
    for spelled in ("\\\\?\\" + os.path.join(javis, "contract.pdf"), "//?/" + javis.replace("\\", "/")):
        assert wd.is_foreign(spelled, "admin"), spelled
        assert not wd.is_foreign(spelled, "javis"), spelled
    assert wd.real_path("\\\\?\\" + javis) == os.path.realpath(javis)


@windows_only
def test_a_share_spelling_of_a_local_folder_is_judged_as_that_folder(env, tmp_path: Path, monkeypatch) -> None:
    """``\\\\localhost\\C$\\…`` (and ``\\\\127.0.0.1\\…``, ``\\\\?\\UNC\\…``) name
    the same folder as ``C:\\…`` — the same file identity — so they are
    placed where it is, whatever realpath leaves of them."""
    if not os.path.isdir(_unc(tmp_path)):
        pytest.skip("the drive's administrative share is not reachable here")
    _, store = env
    docs = tmp_path / "Documents"
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(docs / "workspaces"))
    store.rows["admin"] = str(docs)
    wd.invalidate()
    javis = cfg.get_user_working_directory("javis")
    Path(javis, "contract.pdf").write_text("javis's")
    stray = docs / "workspaces" / ".deleted" / "bob-1"
    stray.mkdir(parents=True)

    for spelled in (
        _unc(javis),
        _unc(javis, "127.0.0.1"),
        "\\\\?\\UNC\\" + _unc(javis)[2:],
    ):
        secret = spelled + "\\contract.pdf"
        assert wd.is_foreign(secret, "admin"), secret
        assert not wd.is_foreign(secret, "javis"), secret
        assert wd.is_foreign(spelled + "\\new-file.txt", "admin"), "a file not created yet too"
    assert wd.owners_of(_unc(stray) + "\\x") == frozenset(), "an unowned entry stays nobody's"
    assert wd.owners_of(_unc(docs / "notes.txt")) == frozenset({"admin"})
    # A share spelling of a folder no zone knows is an ordinary path.
    assert wd.owners_of(_unc(tmp_path / "elsewhere")) is None
    # A walker rooted at a share spelling prunes in that same spelling.
    assert wd.foreign_dirs_inside(_unc(docs), "admin") == sorted(
        os.path.normcase(_unc(p)) for p in (javis, docs / "workspaces" / ".deleted")
    )
    # And the system directory's own rules can ask for the local spelling.
    export = Path(cfg.BaseConfig.CREMIND_SYSTEM_DIR) / "javis" / "exports" / "config.json"
    assert os.path.normcase(wd.local_path(_unc(export))) == os.path.normcase(os.path.realpath(export))
    assert os.path.normcase(wd.local_path("\\\\?\\" + str(export))) == os.path.normcase(os.path.realpath(export))
    assert wd.local_path(_unc(tmp_path / "elsewhere")) == _unc(tmp_path / "elsewhere")


@windows_only
def test_the_admin_cannot_store_a_share_spelling_of_a_local_folder(env, tmp_path: Path) -> None:
    if not os.path.isdir(_unc(tmp_path)):
        pytest.skip("the drive's administrative share is not reachable here")
    javis = cfg.get_user_working_directory("javis")
    inner = wd.validate_working_dir("admin", _unc(javis) + "\\sub")
    assert not inner.ok and inner.code == "inside_system_dir", "judged as the local folder"
    other = wd.validate_working_dir("admin", _unc(tmp_path / "work"))
    assert not other.ok and other.code == "network_path_to_local_folder"


@windows_only
def test_a_share_is_placed_by_identity_without_a_network(env, tmp_path: Path, monkeypatch) -> None:
    """``\\\\nas\\team\\j`` is javis's folder under another name (the same
    file identity) — placed there; the rest of the share matches no folder
    Cremind knows and stays ordinary; a zone that IS on the share is judged
    by its own spelling, with no identity lookups at all."""
    _, store = env
    javis = cfg.get_user_working_directory("javis")
    share = "\\\\nas\\team"
    real_resolved, real_file_id = wd._resolved, wd._file_id
    looked_up: list[str] = []

    def resolved(path):
        s = str(path)
        return os.path.normpath(s) if s.lower().startswith(share) else real_resolved(path)

    def file_id(path):
        n = os.path.normcase(str(path)).rstrip("\\")
        if n.startswith(share):
            looked_up.append(n)
            return real_file_id(javis) if n == share + "\\j" else None
        return real_file_id(path)

    monkeypatch.setattr(wd, "_resolved", resolved)
    monkeypatch.setattr(wd, "_file_id", file_id)
    assert wd.is_foreign(share + "\\j\\contract.pdf", "admin")
    assert not wd.is_foreign(share + "\\j\\contract.pdf", "javis")
    assert wd.owners_of(share + "\\other\\x.txt") is None

    store.rows["admin"] = share + "\\admin-files"
    wd.invalidate()
    looked_up.clear()
    assert wd.owners_of(share + "\\admin-files\\x.txt") == frozenset({"admin"})
    assert wd.is_foreign(share + "\\admin-files\\x.txt", "javis")
    assert looked_up == [], "a path inside a share zone is judged by its spelling"
