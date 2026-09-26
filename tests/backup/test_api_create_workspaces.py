"""POST /api/backup/create: the profiles' working directories are archived
unless the client sends ``"include_workspaces": false`` (CLI
``--no-workspaces``, the web UI's checkbox) — and the finished status says
what happened, for the CLI's summary line."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import app.api.backup as api_backup


@pytest.fixture
def system_dir(tmp_path, monkeypatch) -> Path:
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    return tmp_path


def _client(monkeypatch) -> tuple[TestClient, list[tuple]]:
    seen: list[tuple] = []

    async def fake_run_create(passphrase, include_workspaces=True):
        seen.append((passphrase, include_workspaces))

    monkeypatch.setattr(api_backup, "require_admin", lambda request: None)
    monkeypatch.setattr(api_backup, "_run_create", fake_run_create)
    state = SimpleNamespace(storage_ready=True, config_storage=None)
    app = Starlette(routes=[r for r in api_backup.get_backup_routes(state) if r.path == "/api/backup/create"])
    return TestClient(app), seen


@pytest.mark.parametrize(("body", "expected"), [
    (None, True),
    ({}, True),
    ({"passphrase": "pw"}, True),
    ({"include_workspaces": True}, True),
    ({"include_workspaces": "false"}, True),   # only a JSON false opts out
    ({"include_workspaces": False}, False),
    ({"passphrase": "pw", "include_workspaces": False}, False),
])
def test_only_an_explicit_false_leaves_the_workspaces_out(system_dir, monkeypatch, body, expected):
    client, seen = _client(monkeypatch)
    resp = client.post("/api/backup/create", json=body) if body is not None else client.post("/api/backup/create")
    assert resp.status_code == 202, resp.text
    assert len(seen) == 1
    assert seen[0][1] is expected


def test_the_finished_status_reports_the_workspaces(system_dir, monkeypatch):
    from app.backup import status as bstatus
    from app.backup.manifest import Manifest, SourcePaths

    captured = {}

    def fake_create_backup(options, progress=None):
        captured["include_workspaces"] = options.include_workspaces
        manifest = Manifest(
            app_version="0", alembic_revision=None, db_provider="sqlite", platform="x", hostname="h",
            source_paths=SourcePaths("/s", "/h", "", "/", False, workspaces_root="/s/workspaces"),
            workspaces_included=options.include_workspaces,
            working_dirs={
                "admin": {"path": "/h/Documents", "default": False, "in_workspaces": False,
                          "archived": False},
                "bob": {"path": "/s/workspaces/bob", "default": True, "in_workspaces": True,
                        "archived": options.include_workspaces},
            },
        )
        return SimpleNamespace(
            path=system_dir / "b.cremind-backup", bytes_written=1, file_count=3, skipped=[],
            manifest=manifest, workspaces_file_count=2 if options.include_workspaces else 0,
        )

    import app.backup.engine as engine

    monkeypatch.setattr(engine, "create_backup", fake_create_backup)
    asyncio.run(api_backup._run_create(None, False))
    detail = bstatus.backup_status.read()["detail"]
    assert captured["include_workspaces"] is False
    assert detail["workspaces_included"] is False and detail["workspaces_file_count"] == 0

    asyncio.run(api_backup._run_create(None))
    detail = bstatus.backup_status.read()["detail"]
    assert detail["workspaces_included"] is True
    assert detail["workspaces_file_count"] == 2
    assert detail["workspaces_root"] == "/s/workspaces"
    # Named so the CLI can say what the archive does not carry.
    assert detail["working_dirs_elsewhere"] == {"admin": "/h/Documents"}
