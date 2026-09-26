"""Another profile's working directory under another name — with two profiles.

On Windows one folder has several spellings ``realpath`` does not unify:
``\\\\?\\C:\\…``, ``\\\\localhost\\C$\\…``, ``\\\\127.0.0.1\\C$\\…``,
``\\\\?\\UNC\\localhost\\C$\\…``. Zones are stored as ``C:\\…``, so before
:func:`app.config.working_dirs.owners_of` judged a path by what it IS (plain
spelling, then file identity), a non-admin could point its conversation at
another profile's folder by one of them and read it through ``/api/files``,
or open a terminal there. Every surface asks ``is_foreign``, so fixing the
predicate fixes them all — this pins it on the routes themselves.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import files as files_api
from app.config import working_dirs as wd
from app.utils.context_storage import clear_context
from app.utils.working_directory import WORKING_DIR_OVERRIDE_KEY

cfg = importlib.import_module("app.config.settings")

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows path spellings")

FOREIGN = "foreign_working_directory"


class _Store:
    def __init__(self, rows):
        self.rows = dict(rows)

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


class _Req:
    def __init__(self, username, query=None, body=None):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.query_params = query or {}
        self.path_params = {}
        self.headers = {}
        self._body = body or {}

    async def json(self):
        return self._body


class _Conversations:
    def __init__(self, rows):
        self.rows = rows

    async def get_conversation(self, conversation_id):
        return self.rows.get(conversation_id)

    async def update_conversation(self, conversation_id, **kwargs):
        return None


class _Bus:
    async def publish(self, *_a):
        return None


def _unc(path, host: str = "localhost") -> str:
    drive, rest = os.path.splitdrive(os.path.realpath(str(path)))
    return f"\\\\{host}\\{drive[0]}${rest}"


def _spellings(path) -> list[str]:
    real = os.path.realpath(str(path))
    out = ["\\\\?\\" + real]
    if os.path.isdir(_unc(real)):
        out += [_unc(real), _unc(real, "127.0.0.1"), "\\\\?\\UNC\\" + _unc(real)[2:]]
    return out


@pytest.fixture
def two(tmp_path: Path, monkeypatch):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", os.path.realpath(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    monkeypatch.setattr(cfg, "_dynamic_config_storage", _Store({"admin": None, "javis": None}))
    wd.invalidate()
    dirs = {}
    for who in ("admin", "javis"):
        d = os.path.realpath(cfg.get_user_working_directory(who))
        Path(d, "secret.txt").write_text(f"{who}'s", encoding="utf-8")
        dirs[who] = d
    import app.events.runner as runner

    conversations = _Conversations({
        f"conv-{who}": {"id": f"conv-{who}", "context_id": f"conv-{who}", "profile": who}
        for who in ("admin", "javis")
    })
    monkeypatch.setattr(runner, "get_conversation_storage", lambda: conversations)
    monkeypatch.setattr(files_api, "get_event_stream_bus", lambda: _Bus())
    try:
        yield SimpleNamespace(dirs=dirs)
    finally:
        for who in ("admin", "javis"):
            clear_context(f"conv-{who}", WORKING_DIR_OVERRIDE_KEY)
        wd.invalidate()


def _assert_foreign(resp, label):
    assert resp.status_code == 403, (label, resp.status_code)
    assert json.loads(resp.body).get("code") == FOREIGN, (label, json.loads(resp.body))


@pytest.mark.parametrize(("caller", "owner"), [("javis", "admin"), ("admin", "javis")])
def test_no_spelling_of_another_profiles_folder_gets_through_the_file_api(two, caller, owner) -> None:
    for spelled in _spellings(two.dirs[owner]):
        resp = asyncio.run(files_api._set_cwd(_Req(caller, body={
            "conversation_id": f"conv-{caller}", "path": spelled,
        })))
        _assert_foreign(resp, f"set cwd {spelled}")
        secret = spelled + "\\secret.txt"
        _assert_foreign(
            asyncio.run(files_api._serve_file_by_path(_Req(caller, query={"path": secret}))),
            f"open {secret}",
        )


def test_each_profile_still_reaches_its_own_folder_by_those_spellings(two) -> None:
    for who in ("admin", "javis"):
        for spelled in _spellings(two.dirs[who]):
            assert not wd.is_foreign(spelled + "\\secret.txt", who), spelled


def test_a_terminal_never_opens_in_another_profiles_folder_by_another_name(two, monkeypatch) -> None:
    import app.api.terminals as terminals

    opened = []

    async def _fake_create(profile, *, cwd, cols, rows, extra_env, **_kw):
        opened.append(cwd)
        return SimpleNamespace(terminal_id="t", title="Terminal 1", shell="bash", working_dir=cwd, created_at=0.0)

    monkeypatch.setattr(terminals, "create_terminal", _fake_create)
    monkeypatch.setattr(terminals, "build_system_env", lambda p: {})
    handler = next(
        r.endpoint for r in terminals.get_terminal_routes()
        if getattr(r, "path", "") == "/api/terminals" and "POST" in (r.methods or ())
    )

    async def _json(body):
        return body

    for spelled in _spellings(two.dirs["admin"]):
        req = SimpleNamespace(
            user=SimpleNamespace(is_authenticated=True, username="javis"),
            path_params={}, headers={}, json=lambda b={"cwd": spelled}: _json(b),
        )
        resp = asyncio.run(handler(req))
        assert resp.status_code == 201, spelled
        assert os.path.normcase(os.path.realpath(opened[-1])) == os.path.normcase(two.dirs["javis"]), spelled
