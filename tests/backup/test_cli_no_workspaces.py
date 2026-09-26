"""`cremind backup create --no-workspaces`: the flag reaches the engine (offline)
and the request body (online), and the summary says what was archived."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

typer_testing = pytest.importorskip("typer.testing")

from app.backup.manifest import Manifest, SourcePaths  # noqa: E402
from app.cli.commands.backup import backup_app  # noqa: E402


def _result(tmp_path: Path, included: bool, count: int):
    manifest = Manifest(
        app_version="0", alembic_revision=None, db_provider="sqlite", platform="x", hostname="h",
        source_paths=SourcePaths("/s", "/h", "", "/", False, workspaces_root="/s/workspaces"),
        workspaces_included=included,
        working_dirs={
            "admin": {"path": "/h/Documents", "default": False, "in_workspaces": False, "archived": False},
            "bob": {"path": "/s/workspaces/bob", "default": True, "in_workspaces": True, "archived": included},
        },
    )
    return SimpleNamespace(
        path=tmp_path / "b.cremind-backup", bytes_written=10, file_count=4, skipped=[],
        manifest=manifest, workspaces_file_count=count,
    )


@pytest.mark.parametrize(("args", "included"), [([], True), (["--no-workspaces"], False)])
def test_offline_create_passes_the_flag_and_reports_it(tmp_path, monkeypatch, args, included):
    import app.backup.engine as engine

    seen = {}

    def fake_create_backup(options, progress=None):
        seen["include_workspaces"] = options.include_workspaces
        return _result(tmp_path, options.include_workspaces, 3 if options.include_workspaces else 0)

    monkeypatch.setattr(engine, "create_backup", fake_create_backup)
    monkeypatch.delenv("CREMIND_BACKUP_PASSPHRASE", raising=False)
    out = typer_testing.CliRunner().invoke(backup_app, ["create", "--offline", *args])
    assert out.exit_code == 0, out.output
    assert seen["include_workspaces"] is included
    if included:
        assert "working directories: 3 file(s) from /s/workspaces" in out.output
        assert "outside it are not included" in out.output
    else:
        assert "working directories: not included (--no-workspaces)" in out.output
    # Either way the folder an admin chose elsewhere is named, bob's is not.
    assert "not included: profile 'admin' works in /h/Documents" in out.output
    assert "profile 'bob'" not in out.output


def test_the_client_sends_only_the_opt_out():
    from app.cli.client import backup as client_api

    class _Client:
        def __init__(self):
            self.bodies = []

        async def post_json(self, path, body):
            self.bodies.append((path, body))
            return {"ok": True}

    c = _Client()
    asyncio.run(client_api.create(c, None))
    asyncio.run(client_api.create(c, "pw", include_workspaces=False))
    assert c.bodies == [
        ("/api/backup/create", None),
        ("/api/backup/create", {"passphrase": "pw", "include_workspaces": False}),
    ]
