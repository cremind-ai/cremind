"""Pure, DB-free tests for the blueprint feature.

Covers the secret file filter, the bidirectional version-compat gate, the
fail-closed export audit, staging-extraction safety, and the schedule
next-fire-at recompute — the correctness-critical logic that needs no storage.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from datetime import datetime, timedelta

import pytest

from app.blueprint import manifest as M
from app.blueprint import rules as R
from app.blueprint.engine import audit_no_secrets
from app.blueprint.manifest import (
    BlueprintError,
    BlueprintManifest,
    ComponentEntry,
    SourcePaths,
    check_importable,
)


# ── rules: skill-file secret filter ─────────────────────────────────────────


@pytest.mark.parametrize(
    "rel,is_dir,excluded",
    [
        ("scripts/.env", False, True),
        ("scripts/.google_token.json", False, True),
        ("scripts/.atlassian_token.json", False, True),
        ("scripts/.listener_state.json", False, True),
        ("scripts/my_token.json", False, True),  # credential glob, not a dotfile
        ("creds.secret", False, True),
        ("events/new_mail/msg.md", False, True),  # drop-zone payload
        ("__pycache__/x.pyc", False, True),
        (".git/config", False, True),
        ("SKILL.md", False, False),
        ("scripts/app/client.py", False, False),
        ("events/new_mail", True, False),  # the type dir itself is kept
        ("references/notes.md", False, False),
    ],
)
def test_is_skill_file_excluded(rel, is_dir, excluded):
    assert R.is_skill_file_excluded(rel, is_dir=is_dir) is excluded


def test_is_credential_member():
    assert R.is_credential_member(".env")
    assert R.is_credential_member("service_token.json")
    assert R.is_credential_member("MY_PASSWORD.txt")
    assert not R.is_credential_member("client.py")
    assert not R.is_credential_member("SKILL.md")


# ── manifest gate (both directions) ─────────────────────────────────────────


def _manifest(**kw) -> BlueprintManifest:
    m = BlueprintManifest(
        app_version=kw.pop("app_version", "0.0.8"),
        platform="win32",
        source_profile="admin",
        source_paths=SourcePaths("", "", "", "/", False),
    )
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def test_gate_accepts_v1():
    m = _manifest()
    m.components = {"persona": ComponentEntry(1, "components/persona.json", {})}
    rep = check_importable(m)
    assert rep.ok
    assert rep.supported_components == ["persona"]


def test_gate_rejects_unknown_format():
    m = _manifest(format="not-cremind")
    assert check_importable(m).fatal


def test_gate_rejects_encrypted():
    m = _manifest(encrypted=True)
    assert check_importable(m).fatal


def test_gate_rejects_newer_format_version():
    m = _manifest(format_version=99, min_app_version="9.9.9")
    rep = check_importable(m)
    assert rep.fatal and "9.9.9" in rep.fatal


def test_gate_newer_app_version_is_warning_not_fatal():
    m = _manifest(app_version="99.0.0")
    m.components = {"persona": ComponentEntry(1, "components/persona.json", {})}
    rep = check_importable(m)
    assert rep.ok
    assert rep.warnings


def test_gate_skips_unknown_component_but_imports_rest():
    m = _manifest()
    m.components = {
        "persona": ComponentEntry(1, "components/persona.json", {}),
        "quantum": ComponentEntry(1, "components/quantum.json", {}),
    }
    rep = check_importable(m)
    assert rep.ok
    assert "persona" in rep.supported_components
    assert "quantum" not in rep.supported_components
    assert any("quantum" in w for w in rep.warnings)


def test_gate_skips_too_new_component_version():
    m = _manifest()
    m.components = {"tools": ComponentEntry(99, "components/tools.json", {})}
    rep = check_importable(m)
    assert rep.ok
    assert "tools" not in rep.supported_components


# ── export audit (fail-closed) ────────────────────────────────────────────────


def test_audit_flags_secret_named_value():
    docs = {"llm": {"data": {"providers": [{"name": "openai", "api_key": "sk-leak"}]}}}
    with pytest.raises(BlueprintError):
        audit_no_secrets([], docs)


def test_audit_allows_secret_name_arrays():
    docs = {
        "llm": {"data": {"providers": [{"name": "openai", "required_secrets": ["api_key"]}]}},
        "skills": {"data": {"skills": [{"secret_variables": ["MY_API_KEY"]}]}},
    }
    audit_no_secrets([], docs)  # must not raise


def test_audit_flags_credential_skill_member():
    with pytest.raises(BlueprintError):
        audit_no_secrets(["skills/x/scripts/.google_token.json"], {})


# ── staging extraction safety ──────────────────────────────────────────────────


def _tar_gz_with(members: list[tuple[str, bytes]], *, symlink: tuple[str, str] | None = None) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if symlink is not None:
            link_name, target = symlink
            info = tarfile.TarInfo(link_name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    buf.seek(0)
    return buf


def _write(tmp_path, buf: io.BytesIO):
    p = tmp_path / "x.cremind-blueprint"
    p.write_bytes(buf.getvalue())
    return p


def test_safe_extract_rejects_traversal(tmp_path):
    from app.blueprint.plan import _safe_extract

    arc = _write(tmp_path, _tar_gz_with([("../evil.txt", b"x")]))
    with pytest.raises(BlueprintError):
        _safe_extract(arc, tmp_path / "out")


def test_safe_extract_rejects_symlink(tmp_path):
    from app.blueprint.plan import _safe_extract

    arc = _write(tmp_path, _tar_gz_with([("ok.txt", b"x")], symlink=("link", "/etc/passwd")))
    with pytest.raises(BlueprintError):
        _safe_extract(arc, tmp_path / "out")


def test_safe_extract_rejects_case_collision(tmp_path):
    from app.blueprint.plan import _safe_extract

    arc = _write(tmp_path, _tar_gz_with([("skills/A.txt", b"x"), ("skills/a.txt", b"y")]))
    with pytest.raises(BlueprintError):
        _safe_extract(arc, tmp_path / "out")


def test_safe_extract_enforces_size_cap(tmp_path, monkeypatch):
    from app.blueprint import plan

    monkeypatch.setattr(plan, "_MAX_BYTES", 10)
    arc = _write(tmp_path, _tar_gz_with([("big.txt", b"x" * 100)]))
    with pytest.raises(BlueprintError):
        plan._safe_extract(arc, tmp_path / "out")


def test_safe_extract_ok(tmp_path):
    from app.blueprint.plan import _safe_extract

    arc = _write(tmp_path, _tar_gz_with([("manifest.json", b"{}"), ("components/persona.json", b"{}")]))
    _safe_extract(arc, tmp_path / "out")
    assert (tmp_path / "out" / "manifest.json").is_file()


# ── schedule next-fire-at recompute ───────────────────────────────────────────


def test_recompute_past_one_shot_is_none():
    from app.calendar.recurrence import first_occurrence_on_or_after, format_local

    past = format_local(datetime.now() - timedelta(days=2))
    assert first_occurrence_on_or_after(rrule=None, dtstart=past, moment=datetime.now()) is None


def test_recompute_future_one_shot():
    from app.calendar.recurrence import first_occurrence_on_or_after, format_local

    future_dt = datetime.now().replace(microsecond=0) + timedelta(days=2)
    future = format_local(future_dt)
    got = first_occurrence_on_or_after(rrule=None, dtstart=future, moment=datetime.now())
    assert got == future_dt


def test_recompute_daily_recurrence_from_past_dtstart():
    from app.calendar.recurrence import first_occurrence_on_or_after, format_local

    now = datetime.now().replace(microsecond=0)
    dtstart = format_local(now - timedelta(days=10))
    got = first_occurrence_on_or_after(rrule="FREQ=DAILY", dtstart=dtstart, moment=now)
    assert got is not None
    assert got >= now  # next future occurrence, no backlog
