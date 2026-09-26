"""Each profile's working directory is its own on every ``/api/files`` route.

Before per-profile working directories the file panel's root was one
server-wide folder every profile shared. Now each profile has its own
(``<workspaces root>/<profile>`` by default, see
:mod:`app.config.working_dirs`), and a path inside another profile's folder is
off limits to every route — the admin included, just as it is for another
profile's ``exports``. The default workspaces root sits inside the System
Directory, which every profile may otherwise browse, so without the rule
``<system dir>/workspaces/<other profile>`` would be one click away.

Two real profiles (``admin`` + ``javis``) throughout: a rule that only ever
met ``admin`` would pass on a single-profile box and leak on the first
install with a second profile.
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
from app.utils.context_storage import clear_context, get_context, set_context
from app.utils.working_directory import WORKING_DIR_OVERRIDE_KEY

# By module path: ``app.config.settings`` the attribute is a Dynaconf object.
cfg = importlib.import_module("app.config.settings")

FOREIGN = "foreign_working_directory"


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


class _Req:
    def __init__(self, username, query=None, body=None, form=None, path_params=None, connected=False):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.query_params = query or {}
        self.path_params = path_params or {}
        self._body = body or {}
        self._form = form
        self._connected = connected

    async def json(self):
        return self._body

    async def form(self):
        return self._form

    async def is_disconnected(self):
        return not self._connected


class _Form:
    def __init__(self, fields: dict, files: list):
        self._fields = fields
        self._files = files

    def get(self, key, default=None):
        return self._fields.get(key, default)

    def multi_items(self):
        return [*self._fields.items(), *((f.filename, f) for f in self._files)]


class _Upload:
    def __init__(self, filename: str, data: bytes = b"x"):
        self.filename = filename
        self._chunks = [data]

    async def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


class _FakeConversations:
    def __init__(self, rows: dict[str, dict]):
        self.rows = rows
        self.updates: list[tuple[str, dict]] = []

    async def get_conversation(self, conversation_id):
        return self.rows.get(conversation_id)

    async def update_conversation(self, conversation_id, **kwargs):
        self.updates.append((conversation_id, kwargs))


class _RecordingBus:
    def __init__(self):
        self.published: list = []

    async def publish(self, channel, event, payload):
        self.published.append((channel, event, payload))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A System Directory holding two profiles' default working directories,
    each with a file and a sub-folder; one conversation per profile."""
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", os.path.realpath(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    store = _Store({"admin": None, "javis": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    wd.invalidate()

    dirs = {}
    for who in ("admin", "javis"):
        d = os.path.realpath(cfg.get_user_working_directory(who))
        os.makedirs(os.path.join(d, "sub"))
        Path(d, "secret.txt").write_text(f"{who}'s", encoding="utf-8")
        Path(d, "sub", "deep.txt").write_text(f"{who}'s", encoding="utf-8")
        dirs[who] = d

    conversations = _FakeConversations({
        f"conv-{who}": {"id": f"conv-{who}", "context_id": f"conv-{who}", "profile": who}
        for who in ("admin", "javis")
    })
    import app.events.runner as runner

    monkeypatch.setattr(runner, "get_conversation_storage", lambda: conversations)
    bus = _RecordingBus()
    monkeypatch.setattr(files_api, "get_event_stream_bus", lambda: bus)
    try:
        yield SimpleNamespace(
            sys=os.path.realpath(sysdir), store=store, dirs=dirs,
            ws=os.path.realpath(wd.workspaces_root()), conversations=conversations, bus=bus,
        )
    finally:
        for who in ("admin", "javis"):
            clear_context(f"conv-{who}", WORKING_DIR_OVERRIDE_KEY)
        wd.invalidate()


def _run(coro):
    return asyncio.run(coro)


def _body(resp) -> dict:
    return json.loads(resp.body)


def _list(who, path, *, show_hidden=False, conversation_id=None):
    query = {"path": path, "show_hidden": "1" if show_hidden else "0"}
    if conversation_id:
        query["conversation_id"] = conversation_id
    return _run(files_api._list_directory(_Req(who, query=query)))


def _names(resp) -> list[str]:
    assert resp.status_code == 200, _body(resp)
    return [e["name"] for e in _body(resp)["entries"]]


def _assert_foreign(resp, label=""):
    assert resp.status_code == 403, (label, resp.status_code)
    body = _body(resp)
    assert body.get("code") == FOREIGN, (label, body)
    assert "belongs to another profile" in body["error"], (label, body)


class _StubObserver:
    captured: dict = {}

    def schedule(self, handler, path, recursive=False):
        _StubObserver.captured["handler"] = handler

    def start(self):
        return None

    def stop(self):
        return None

    def join(self, timeout=None):
        return None


# ── the seed path ─────────────────────────────────────────────────────────────


def test_get_cwd_returns_each_profiles_own_folder(env) -> None:
    for who in ("admin", "javis"):
        resp = _run(files_api._get_cwd(_Req(who)))
        assert resp.status_code == 200
        assert os.path.realpath(_body(resp)["cwd"]) == env.dirs[who]
    assert env.dirs["admin"] != env.dirs["javis"]
    assert os.path.dirname(env.dirs["javis"]) == env.ws


def test_each_profile_still_reaches_its_own_folder(env) -> None:
    for who in ("admin", "javis"):
        assert _names(_list(who, env.dirs[who])) == ["sub", "secret.txt"]
        opened = _run(files_api._serve_file_by_path(
            _Req(who, query={"path": os.path.join(env.dirs[who], "secret.txt")})
        ))
        assert opened.status_code == 200


# ── another profile's folder, on every route — admin not exempt ──────────────


@pytest.mark.parametrize(("caller", "owner"), [("javis", "admin"), ("admin", "javis")])
def test_no_route_reaches_into_another_profiles_working_directory(env, caller, owner) -> None:
    theirs = env.dirs[owner]
    mine = env.dirs[caller]
    secret = os.path.join(theirs, "secret.txt")
    Path(mine, "mine.txt").write_text("x", encoding="utf-8")

    _assert_foreign(_list(caller, theirs), "list")
    _assert_foreign(_list(caller, os.path.join(theirs, "sub")), "list sub")
    _assert_foreign(_run(files_api._serve_file_by_path(_Req(caller, query={"path": secret}))), "open")
    # The legacy relative route reaches the default workspaces through the
    # System Directory: same rule.
    rel = os.path.relpath(secret, env.sys)
    _assert_foreign(_run(files_api._serve_file(_Req(caller, path_params={"path": rel}))), "{path}")

    _assert_foreign(_run(files_api._upload_files(_Req(caller, form=_Form(
        {"path": theirs}, [_Upload("planted.txt")],
    )))), "upload")
    _assert_foreign(_run(files_api._mkdir(_Req(caller, body={"path": os.path.join(theirs, "new")}))), "mkdir")
    _assert_foreign(_run(files_api._delete_entry(_Req(caller, body={"path": secret}))), "delete")
    _assert_foreign(_run(files_api._move_entry(_Req(caller, body={
        "src": secret, "dest": os.path.join(mine, "stolen.txt"),
    }))), "move out")
    _assert_foreign(_run(files_api._move_entry(_Req(caller, body={
        "src": os.path.join(mine, "mine.txt"), "dest": os.path.join(theirs, "planted.txt"),
    }))), "move in")

    # The watch gate refuses before any observer starts.
    _assert_foreign(_run(files_api._watch_directory(_Req(caller, query={"path": theirs}))), "watch")

    # POST /cwd, on the caller's own conversation: the agent's shell must not
    # be aimed there either.
    resp = _run(files_api._set_cwd(_Req(caller, body={
        "conversation_id": f"conv-{caller}", "path": os.path.join(theirs, "sub"),
    })))
    _assert_foreign(resp, "set cwd")
    assert get_context(f"conv-{caller}", WORKING_DIR_OVERRIDE_KEY) is None
    assert env.conversations.updates == [] and env.bus.published == []

    # Nothing moved, nothing planted, nothing deleted.
    assert sorted(os.listdir(theirs)) == ["secret.txt", "sub"]
    assert sorted(os.listdir(mine)) == ["mine.txt", "secret.txt", "sub"]


def test_a_conversation_override_never_widens_into_another_profiles_folder(env) -> None:
    """An override set before this rule existed (or by any other means) that
    points into another profile's folder buys nothing."""
    set_context("conv-admin", WORKING_DIR_OVERRIDE_KEY, env.dirs["javis"])
    _assert_foreign(_list("admin", env.dirs["javis"], conversation_id="conv-admin"))
    _assert_foreign(_run(files_api._serve_file_by_path(_Req("admin", query={
        "path": os.path.join(env.dirs["javis"], "secret.txt"), "conversation_id": "conv-admin",
    }))))


def test_set_cwd_also_refuses_another_profiles_exports(env) -> None:
    exports = Path(env.sys, "admin", "exports")
    exports.mkdir(parents=True)
    resp = _run(files_api._set_cwd(_Req("javis", body={
        "conversation_id": "conv-javis", "path": str(exports),
    })))
    assert resp.status_code == 403
    assert get_context("conv-javis", WORKING_DIR_OVERRIDE_KEY) is None
    # Its owner may.
    resp = _run(files_api._set_cwd(_Req("admin", body={
        "conversation_id": "conv-admin", "path": str(exports),
    })))
    assert resp.status_code == 200


# ── listings ─────────────────────────────────────────────────────────────────


def test_listing_the_workspaces_root_shows_only_your_own_folder(env) -> None:
    Path(env.ws, ".deleted", "bob-20260101").mkdir(parents=True)
    Path(env.ws, "stray").mkdir()
    for who in ("admin", "javis"):
        assert _names(_list(who, env.ws, show_hidden=True)) == [who]
    # The System Directory itself still lists the workspaces folder.
    assert "workspaces" in _names(_list("javis", env.sys))


def test_the_system_listing_hides_another_profiles_exports(env) -> None:
    Path(env.sys, "admin", "exports").mkdir(parents=True)
    Path(env.sys, "admin", "skills").mkdir(parents=True)
    assert _names(_list("javis", os.path.join(env.sys, "admin"))) == ["skills"]
    assert _names(_list("admin", os.path.join(env.sys, "admin"))) == ["exports", "skills"]


def test_a_legacy_admin_folder_holding_the_workspaces_hides_the_others(env, tmp_path, monkeypatch) -> None:
    """Containers: the upgraded admin kept ``/root/Documents`` and the
    workspaces root sits inside it. The admin sees its own files and the
    workspaces folder, but not javis's folder inside."""
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "notes.txt").write_text("admin's", encoding="utf-8")
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(docs / "workspaces"))
    env.store.rows["admin"] = str(docs)
    wd.invalidate()
    javis = os.path.realpath(cfg.get_user_working_directory("javis"))
    Path(javis, "contract.pdf").write_text("javis's", encoding="utf-8")
    docs_real = os.path.realpath(docs)

    assert _names(_list("admin", docs_real)) == ["workspaces", "notes.txt"]
    assert _names(_list("admin", os.path.join(docs_real, "workspaces"))) == []
    _assert_foreign(_list("admin", javis))
    _assert_foreign(_list("javis", docs_real))
    assert _names(_list("javis", javis)) == ["contract.pdf"]


# ── roots are never deleted or moved ─────────────────────────────────────────


def test_a_working_directory_root_cannot_be_deleted_or_moved(env) -> None:
    for who in ("admin", "javis"):
        mine = env.dirs[who]
        for root in (mine, env.ws, env.sys):
            resp = _run(files_api._delete_entry(_Req(who, body={"path": root})))
            assert resp.status_code == 400, (who, root)
            resp = _run(files_api._move_entry(_Req(who, body={
                "src": root, "dest": os.path.join(mine, "sub", "moved"),
            })))
            assert resp.status_code == 400, (who, root)
        assert os.path.isdir(mine)
        # Inside its own folder a profile still deletes and moves freely.
        resp = _run(files_api._move_entry(_Req(who, body={
            "src": os.path.join(mine, "sub"), "dest": os.path.join(mine, "renamed"),
        })))
        assert resp.status_code == 200
        resp = _run(files_api._delete_entry(_Req(who, body={"path": os.path.join(mine, "renamed")})))
        assert resp.status_code == 200
        assert sorted(os.listdir(mine)) == ["secret.txt"]


def test_a_shared_folder_root_is_protected_for_both_its_profiles(env, tmp_path) -> None:
    team = tmp_path / "team"
    (team / "plan").mkdir(parents=True)
    env.store.rows["admin"] = str(team)
    env.store.rows["javis"] = str(team)
    wd.invalidate()
    team_real = os.path.realpath(team)
    for who in ("admin", "javis"):
        assert _names(_list(who, team_real)) == ["plan"]
        resp = _run(files_api._delete_entry(_Req(who, body={"path": team_real})))
        assert resp.status_code == 400, who


def test_a_parent_holding_another_profiles_folder_cannot_be_deleted_or_moved(env, tmp_path) -> None:
    """javis's folder was set inside a directory admin's conversation was
    switched into: removing (or carrying off) that parent would take javis's
    files with it."""
    data = tmp_path / "data"
    (data / "javis-work").mkdir(parents=True)
    (data / "javis-work" / "a.txt").write_text("javis's", encoding="utf-8")
    env.store.rows["javis"] = str(data / "javis-work")
    wd.invalidate()
    data_real = os.path.realpath(data)
    set_context("conv-admin", WORKING_DIR_OVERRIDE_KEY, data_real)

    assert _names(_list("admin", data_real, conversation_id="conv-admin")) == []
    resp = _run(files_api._delete_entry(_Req("admin", body={
        "path": data_real, "conversation_id": "conv-admin",
    })))
    assert resp.status_code == 403 and "working directories" in _body(resp)["error"]
    resp = _run(files_api._move_entry(_Req("admin", body={
        "src": data_real, "dest": os.path.join(env.dirs["admin"], "data"), "conversation_id": "conv-admin",
    })))
    assert resp.status_code == 403
    assert (data / "javis-work" / "a.txt").exists()


def test_nothing_is_created_directly_in_the_workspaces_root(env) -> None:
    for who in ("admin", "javis"):
        resp = _run(files_api._mkdir(_Req(who, body={"path": os.path.join(env.ws, "bob")})))
        assert resp.status_code == 403, who
        resp = _run(files_api._upload_files(_Req(who, form=_Form({"path": env.ws}, [_Upload("x.txt")]))))
        assert resp.status_code == 403, who
        resp = _run(files_api._move_entry(_Req(who, body={
            "src": os.path.join(env.dirs[who], "secret.txt"), "dest": os.path.join(env.ws, "bob"),
        })))
        assert resp.status_code == 403, who
    assert sorted(os.listdir(env.ws)) == ["admin", "javis"]


# ── unowned entries: nobody's ────────────────────────────────────────────────


def test_an_unowned_deleted_entry_is_refused_to_everyone(env) -> None:
    archived = Path(env.ws, ".deleted", "bob-20260101")
    archived.mkdir(parents=True)
    (archived / "a.txt").write_text("bob's", encoding="utf-8")
    target = os.path.realpath(archived / "a.txt")
    for who in ("admin", "javis"):
        _assert_foreign(_run(files_api._serve_file_by_path(_Req(who, query={"path": target}))), who)
        _assert_foreign(_list(who, os.path.realpath(archived)), who)
        _assert_foreign(_list(who, os.path.realpath(archived.parent)), who)
        resp = _run(files_api._delete_entry(_Req(who, body={"path": os.path.realpath(archived.parent)})))
        assert resp.status_code in (400, 403), who
        rel = os.path.relpath(target, env.sys)
        _assert_foreign(_run(files_api._serve_file(_Req(who, path_params={"path": rel}))), who)
    assert (archived / "a.txt").exists()


# ── group rooms: the admin's read bypass does not reach a member's folder ────


def test_a_group_room_admin_read_of_a_members_workspace_is_refused(env) -> None:
    seat_row, seat_context = "conv-seat", "group:g1:javis"
    env.conversations.rows[seat_row] = {"id": seat_row, "context_id": seat_context, "profile": "javis"}
    set_context(seat_context, WORKING_DIR_OVERRIDE_KEY, os.path.join(env.dirs["javis"], "sub"))
    try:
        # The member reads its own seat's tree...
        assert _names(_list("javis", os.path.join(env.dirs["javis"], "sub"), conversation_id=seat_row)) == [
            "deep.txt",
        ]
        # ...the admin's read bypass names the seat, but the folder is javis's.
        _assert_foreign(_list("admin", os.path.join(env.dirs["javis"], "sub"), conversation_id=seat_row))
        _assert_foreign(_list("admin", env.dirs["javis"], conversation_id=seat_row))
        _assert_foreign(_run(files_api._serve_file_by_path(_Req("admin", query={
            "path": os.path.join(env.dirs["javis"], "sub", "deep.txt"), "conversation_id": seat_row,
        }))))
        _assert_foreign(_run(files_api._watch_directory(_Req("admin", query={
            "path": env.dirs["javis"], "conversation_id": seat_row,
        }))))
    finally:
        clear_context(seat_context, WORKING_DIR_OVERRIDE_KEY)


# ── the change stream ────────────────────────────────────────────────────────


def test_the_change_stream_withholds_other_profiles_folders_and_exports(env, monkeypatch) -> None:
    """A watch on the System Directory spans ``workspaces/`` and every
    profile's slice: another profile's file names must not ride its events."""
    monkeypatch.setattr(files_api, "Observer", _StubObserver)
    _StubObserver.captured = {}
    theirs = os.path.join(env.dirs["admin"], "secret.txt")
    export = os.path.join(env.sys, "admin", "exports", "cremind-javis-config.md")
    newcomer = os.path.join(env.ws, "newbie", "file.txt")  # a folder no profile owns yet
    mine = os.path.join(env.dirs["javis"], "notes.txt")

    async def _drive():
        response = await files_api._watch_directory(_Req("javis", query={"path": env.sys}, connected=True))
        assert response.status_code == 200
        frames = response.body_iterator.__aiter__()
        assert b"ready" in await frames.__anext__()
        handler = _StubObserver.captured["handler"]
        handler.on_created(SimpleNamespace(src_path=theirs, is_directory=False))
        handler.on_modified(SimpleNamespace(src_path=export, is_directory=False))
        handler.on_created(SimpleNamespace(src_path=newcomer, is_directory=False))
        handler.on_moved(SimpleNamespace(src_path=mine, dest_path=theirs, is_directory=False))
        handler.on_created(SimpleNamespace(src_path=mine, is_directory=False))
        frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
        await frames.aclose()
        return json.loads(frame.decode("utf-8").split("data: ", 1)[1])

    assert _run(_drive()) == {"type": "created", "path": mine, "is_dir": False}
