"""The "User documents" clean component, and the clean vocabulary's mirrors.

Cleaning removes the index, its vectors, the captions, the quota counters and
the settings — and never a single file of the user's. The component list is
defined once on the server and mirrored by the CLI (which must not import
server code) and the web UI; this pins the three together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.storage.userdocs_storage as uds_storage_module  # noqa: E402
from app.config.settings import BaseConfig  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.userdocs_storage import UserDocsStorage  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]


def test_the_clean_vocabulary_is_mirrored_everywhere():
    from app.cli.commands.clean import _COMPONENTS as CLI
    from app.reset.components import COMPONENTS
    from app.reset.engine import _ORDER

    assert [k for k, _ in CLI] == list(COMPONENTS)
    assert {k for k, _ in _ORDER} == set(COMPONENTS)
    ui = (_REPO / "ui" / "src" / "services" / "cleanApi.ts").read_text(encoding="utf-8")
    ui_keys = re.findall(r"key: '([a-z_]+)'", ui)
    assert set(ui_keys) >= set(COMPONENTS), set(COMPONENTS) - set(ui_keys)
    assert "user_documents" in COMPONENTS


def test_clean_deletes_the_index_but_never_the_files(tmp_path: Path, monkeypatch):
    from app.userdocs import service as svc_module
    from app.userdocs.index import IndexDB, index_dir, index_path

    sysdir = tmp_path / "system"
    root = tmp_path / "work"
    root.mkdir()
    (root / "keep-me.txt").write_text("my file", encoding="utf-8")
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.setattr(svc_module, "_service", None)

    provider = SqliteDatabaseProvider(str(tmp_path / "main.db"))
    eng = provider.sync_engine()
    for name in ("profiles", "userdoc_sources", "userdoc_captions", "userdoc_vision_usage"):
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id,name,created_at,updated_at) VALUES ('uid-a','alice',0,0)"))
    storage = UserDocsStorage(provider)
    monkeypatch.setattr(uds_storage_module, "_instance", storage)
    storage.upsert_source("alice", "local", enabled=True, root_path=str(root))
    storage.put_caption("alice", "h", variant="image", caption_text="a photo")
    storage.reserve_vision("alice", "2026-09-25", cap=5)

    db = IndexDB.open(index_path("uid-a"), profile_uid="uid-a")
    db.close()
    assert Path(index_dir("uid-a")).exists()

    report = svc_module.clean_profile("alice")

    assert not Path(index_dir("uid-a")).exists()
    assert storage.get_source("alice", "local") is None
    assert storage.get_caption("alice", "h") is None
    assert report["sources"] == 1
    assert (root / "keep-me.txt").read_text(encoding="utf-8") == "my file"


def test_backups_never_walk_the_index(tmp_path: Path):
    """Backups are include-list driven (browser-profile + each profile's tree);
    the index lives under storage/, which is dumped logically, never walked."""
    from app.backup.rules import include_roots

    roots = include_roots(["alice", "bob"])
    assert "storage" not in roots
    assert all(not r.startswith("storage") for r in roots)
