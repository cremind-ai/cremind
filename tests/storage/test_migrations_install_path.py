"""The migration tree must be found wherever the wheel is installed.

A Windows user named "Tiep Vu" has ``site-packages`` under ``C:\\Users\\Tiep
Vu\\``. Alembic splits a ``version_locations`` value on spaces and commas
unless a ``path_separator`` is configured, and it silently skips folders that
don't exist, so the two halves of that path found no migrations at all. Then
``upgrade head`` was a no-op, boot logged "schema already at head
rev=<none>", and the first query died with ``no such table: profiles``. That
has been true for every such install since v0.0.1.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.storage import migrations

_REAL_ALEMBIC = Path(migrations._alembic_dir())


@pytest.mark.parametrize("folder", ["Tiep Vu", "a,b", "plain"])
def test_the_migrations_are_found_under_any_install_path(tmp_path, monkeypatch, folder) -> None:
    expected = migrations.heads()
    copy = tmp_path / folder / "site-packages" / "app" / "alembic"
    shutil.copytree(_REAL_ALEMBIC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(migrations, "_alembic_dir", lambda: copy)

    assert migrations.heads() == expected


def test_a_fresh_install_under_a_spaced_path_gets_its_tables(tmp_path, monkeypatch) -> None:
    """The reported boot, end to end: a new SQLite database, with the package
    installed under a folder whose name has a space."""
    from app.config.settings import BaseConfig
    from app.databases import create_database_provider, set_database_provider
    from sqlalchemy import inspect

    copy = tmp_path / "Tiep Vu" / "site-packages" / "app" / "alembic"
    shutil.copytree(_REAL_ALEMBIC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(migrations, "_alembic_dir", lambda: copy)

    system_dir = tmp_path / "Tiep Vu" / ".cremind"
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(system_dir))
    monkeypatch.delenv("CREMIND_DB_PROVIDER", raising=False)
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(system_dir), raising=False)
    monkeypatch.setattr(
        BaseConfig, "SQLITE_DB_PATH", str(system_dir / "storage" / "cremind.db"), raising=False
    )
    set_database_provider(None)
    set_database_provider(create_database_provider())
    try:
        migrations._ensure_at_head_sync()

        assert migrations.current_revision() in migrations.heads()
        from app.databases import get_database_provider
        with get_database_provider().sync_engine().connect() as conn:
            assert inspect(conn).has_table("profiles")
    finally:
        set_database_provider(None)


def test_boot_refuses_to_run_without_any_migrations(tmp_path, monkeypatch) -> None:
    """Finding no migrations must stop the boot, not report the empty
    database as being at head."""
    empty = tmp_path / "alembic"
    (empty / "versions").mkdir(parents=True)
    shutil.copy(_REAL_ALEMBIC / "env.py", empty / "env.py")
    monkeypatch.setattr(migrations, "_alembic_dir", lambda: empty)

    with pytest.raises(RuntimeError, match="No Alembic migrations found"):
        migrations._ensure_at_head_sync()
