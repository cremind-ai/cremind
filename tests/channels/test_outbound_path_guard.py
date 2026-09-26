"""What an outbound file may be, whoever asks and whichever tool sends it.

``validate_outbound_paths`` is the one gate every deliberate send goes through:
the ``send_files_to_chat`` / ``send_channel_message`` / ``send_notification``
tools and the operator's REST sends. Being inside the profile's roots used to be
the whole rule, and those roots hold far more than documents — the profile's
slice of the system dir keeps its channel logins (a Telegram session file IS the
account), its browser profile and its skills' OAuth tokens, and an admin's
folder may contain the whole system dir. These pin what that means now:
documents go, Cremind's own data and credentials never do.

The system dir here is deliberately NOT a dot-folder, so each rule is seen on
its own rather than hidden behind the hidden-name one.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.channels.attachments import (
    REASON_CREDENTIAL,
    REASON_CREMIND_DATA,
    REASON_HIDDEN,
    validate_outbound_paths,
)
from app.config import working_dirs as wd

# By module path: ``app.config.settings`` the attribute is a Dynaconf object.
cfg = importlib.import_module("app.config.settings")

_PEM_KEY = (
    b"-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
    b"-----END PRIVATE KEY-----\n"
)
_PEM_CERT = (
    b"-----BEGIN CERTIFICATE-----\nMIIDdzCCAl+gAwIBAgIEAgAAuTANBgkq\n"
    b"-----END CERTIFICATE-----\n"
)


class _Store:
    """The DynamicConfigStorage methods working_dirs reads."""

    def __init__(self, rows):
        self.rows = dict(rows)

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    sysdir = home / "cremind-data"
    sysdir.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    store = _Store({"p": None, "q": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    wd.invalidate()
    ns = SimpleNamespace(home=home, sys=sysdir, store=store)
    ns.work = Path(cfg.get_user_working_directory("p"))
    yield ns
    wd.invalidate()


def _point_working_dir(box, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    box.store.rows["p"] = str(path)
    wd.invalidate()


def _write(path: Path, data: bytes = b"%PDF-1.7 content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sent(path: Path, **kw) -> dict:
    ok, rejected = validate_outbound_paths("p", [str(path)], **kw)
    assert rejected == [], rejected
    assert len(ok) == 1
    return ok[0]


def _refusal(path: Path, **kw) -> str:
    ok, rejected = validate_outbound_paths("p", [str(path)], **kw)
    assert ok == [], ok
    assert len(rejected) == 1
    return rejected[0]["reason"]


# ── what may go ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("where", [
    "work/report.pdf",
    "work/clients/2026/invoice.xlsx",
    "sys/p/uploads_tmp/conv1/photo.jpg",
    "sys/p/exports/research/dossier.pdf",
    "sys/p/plans/plan.md",
    "sys/p/skills/invoice/templates/invoice.docx",
])
def test_documents_in_the_profiles_own_places_can_be_sent(box, where):
    base, rest = where.split("/", 1)
    root = box.work if base == "work" else box.sys
    path = _write(root / rest)
    entry = _sent(path)
    assert entry["name"] == path.name
    assert entry["size"] == path.stat().st_size


def test_a_certificate_is_public_and_can_be_sent(box):
    _sent(_write(box.work / "ca.pem", _PEM_CERT))


def test_a_public_key_can_be_sent(box):
    _sent(_write(box.work / "id_ed25519.pub", b"ssh-ed25519 AAAAC3Nza lee@box\n"))


def test_a_keynote_deck_named_key_can_be_sent(box):
    """``.key`` is also Keynote's extension; the bytes decide, not the name."""
    _sent(_write(box.work / "pitch.key", b"PK\x03\x04 keynote archive"))


def test_a_working_directory_that_is_a_dot_folder_still_works(box):
    """The hidden-name rule reads the path BELOW the root that admitted it: a
    dot-folder chosen as the working directory is the operator's choice."""
    _point_working_dir(box, box.home / ".work")
    _sent(_write(box.home / ".work" / "report.pdf"))


# ── Cremind's own data ─────────────────────────────────────────────────────


@pytest.mark.parametrize("rest", [
    "p/PERSONA.md",
    "p/INSTRUCTIONS.md",
    "p/telegram/c1/session.session-journal",
    "p/zalo/c1/credentials.json",
    "p/whatsapp/c1/session/creds.json",
    "p/browser-profile/Default/Network/Cookies",
    "p/exec_shell/logs/run.log",
    "p/report.pdf",
])
def test_the_profiles_own_slice_is_cremind_data_outside_its_user_folders(box, rest):
    """The slice is one of the profile's roots, and its user-facing folders are
    sendable; everything else in it is Cremind's state — logins included."""
    assert _refusal(_write(box.sys / rest)) == REASON_CREMIND_DATA


@pytest.mark.parametrize("rest", [
    "storage/cremind.db",
    "storage/cremind.db-wal",
    "storage/documents/uid/index.db",
    "tokens/p.token",
    "tls/ca.key",
    "bootstrap.toml",
    "cli-sessions.json",
    "q/uploads_tmp/conv/their-file.pdf",
])
def test_the_system_dir_stays_shut_even_inside_an_admins_folder(box, rest):
    """An admin whose working directory contains the whole system dir still
    cannot send the database, the tokens, the TLS keys or another profile's
    files out of it."""
    _point_working_dir(box, box.home)
    assert _refusal(_write(box.sys / rest)) == REASON_CREMIND_DATA


def test_a_hidden_system_dir_inside_an_admins_folder_is_refused_too(box, monkeypatch):
    """The real layout: ``~/.cremind`` inside a working directory of ``~``."""
    sysdir = box.home / ".cremind"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    wd.invalidate()
    _point_working_dir(box, box.home)
    assert _refusal(_write(sysdir / "storage" / "cremind.db")) == REASON_CREMIND_DATA


def test_the_default_workspace_inside_the_system_dir_is_the_profiles(box):
    assert box.sys in box.work.parents  # the default layout this relies on
    _sent(_write(box.work / "notes.txt", b"hello"))


def test_another_profiles_uploads_are_not_this_profiles(box):
    reason = _refusal(_write(box.sys / "q" / "uploads_tmp" / "c" / "x.pdf"))
    assert reason == "outside this profile's allowed directories"


# ── hidden files, credential stores, key files ─────────────────────────────


@pytest.mark.parametrize("rest", [
    "skills/gmail/scripts/.env",
    "skills/gcalendar/scripts/.google_token.json",
    "skills/jira/scripts/.atlassian_token.json",
])
def test_a_skills_tokens_and_env_file_never_leave(box, rest):
    assert _refusal(_write(box.sys / "p" / rest, b"SECRET=1")) == REASON_HIDDEN


@pytest.mark.parametrize("rest", [
    ".env",
    ".ssh/id_rsa",
    "project/.aws/credentials",
    "project/.git/config",
    ".config/gcloud/application_default_credentials.json",
])
def test_hidden_files_and_folders_in_the_working_dir_never_leave(box, rest):
    assert _refusal(_write(box.work / rest, b"secret")) == REASON_HIDDEN


@pytest.mark.parametrize("rest", [
    "coding-cli/claude/token",
    "AppData/Local/Google/Chrome/prefs",
    "prod.env",
    "account.session",
    "account.session-journal",
    "cert.p12",
    "cert.pfx",
    "store.jks",
    "passwords.kdbx",
    "putty.ppk",
    "api.token",
    "id_ed25519",
    "id_rsa",
    "credentials.json",
    "token.json",
    "Cookies",
    "Login Data",
    "logins.json",
    "key4.db",
])
def test_credential_stores_and_key_files_never_leave(box, rest):
    assert _refusal(_write(box.work / rest, b"secret")) == REASON_CREDENTIAL


@pytest.mark.parametrize("name", ["server.key", "notes.txt", "combined.pem"])
def test_a_private_key_is_refused_whatever_the_file_is_called(box, name):
    data = _PEM_CERT + _PEM_KEY if name == "combined.pem" else _PEM_KEY
    assert _refusal(_write(box.work / name, data)) == REASON_CREDENTIAL


def test_one_refused_file_does_not_hide_the_others(box):
    good = _write(box.work / "report.pdf")
    bad = _write(box.work / ".env", b"SECRET=1")
    ok, rejected = validate_outbound_paths("p", [str(bad), str(good)])
    assert [o["name"] for o in ok] == ["report.pdf"]
    assert rejected == [{"path": str(bad), "reason": REASON_HIDDEN}]


# ── spellings ──────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name != "nt", reason="Windows path spelling")
def test_the_literal_windows_spelling_of_an_allowed_file_is_that_file(box):
    path = _write(box.work / "report.pdf")
    entry = _sent(Path("\\\\?\\" + str(path)))
    assert not entry["path"].startswith("\\\\?\\")


def test_a_link_into_cremind_data_is_judged_where_it_points(box):
    """A working directory that contains the system dir admits the link's
    target, so what refuses it is the Cremind-data rule — the one under test —
    rather than the roots check that a narrower folder would trip first."""
    _point_working_dir(box, box.home)
    target = _write(box.sys / "storage" / "cremind.db", b"SQLite format 3\x00")
    link = box.home / "innocent.pdf"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")
    assert _refusal(link) == REASON_CREMIND_DATA


def test_a_link_out_of_the_profiles_folders_is_refused(box, tmp_path):
    outside = _write(tmp_path / "elsewhere" / "report.pdf")
    link = box.work / "report.pdf"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")
    assert _refusal(link) == "outside this profile's allowed directories"
