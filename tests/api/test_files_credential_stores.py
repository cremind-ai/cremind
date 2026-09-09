"""The file API must never serve a coding-agent credential store.

``app.api.files`` sandboxes requests to the System Directory, but that sandbox
is a path-traversal guard, not an authorization boundary: every authenticated
profile may read anything *inside* it. That was survivable while the directory
held per-profile working data.

It stopped being survivable when the coding agents moved their sign-in there.
A Claude or ChatGPT login is a long-lived OAuth refresh token that nothing
revokes upstream, it is written at the completely predictable path
``<system dir>/<profile>/coding-cli/...``, and the sign-in is offered to every
profile from Settings. Before the guard these tests pin, an ordinary member
profile holding only its own token could list ``alice/coding-cli/codex``, fetch
``auth.json``, and spend alice's subscription outside Cremind entirely. The
dotfile filter was no help: it only affects listings, and the ``{path:path}``
route does no name filtering at all, so ``.credentials.json`` came back
verbatim.

The same applies to ``codex-home``, where an API-key credential is installed
for the Codex app-server.

These paths have no legitimate reader over HTTP -- the SPA never asks for them,
the runners read them straight off disk -- so the routes refuse them outright
rather than trying to work out who is asking.

The second half of this file is the second round. The first guard was anchored
to the System Directory and asked only whether the path *is* a store, which
left four ways past it: ``/move`` could carry a profile directory (store and
all) out into the user working directory where the rule stops applying and read
it there; the shared server login on a native install (``~/.claude`` /
``~/.codex``, or wherever ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` point) was not
a credential path at all, and ``POST /cwd`` could aim a conversation at it;
``/move`` and ``/mkdir`` validated only the *parent* of a create target, so a
caller could put a directory named ``coding-cli`` anywhere -- where the listing
filter would then hide it, and its own contents, from the user who made it; and
the recursive change-stream re-advertised every name the listing filter hides.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from app.api import files as files_api  # noqa: E402
from app.config import coding_cli_homes as homes  # noqa: E402
from app.config.settings import BaseConfig  # noqa: E402
from app.utils.context_storage import clear_context, get_context  # noqa: E402
from app.utils.working_directory import WORKING_DIR_OVERRIDE_KEY  # noqa: E402

CONV = "conv-credential-guard"


class _Req:
    """The smallest request the file routes actually read."""

    def __init__(
        self, username="bob", query=None, path_params=None, body=None,
        connected=False,
    ):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.query_params = query or {}
        self.path_params = path_params or {}
        self._body = body or {}
        # The watch generator returns as soon as the client is gone, so the
        # streaming test needs a request that stays connected.
        self._connected = connected

    async def json(self):
        return self._body

    async def is_disconnected(self):
        return not self._connected


@pytest.fixture()
def system_dir(monkeypatch, tmp_path):
    """Point every copy of the System Directory at a scratch tree.

    ``files`` and ``coding_cli_homes`` each read ``BaseConfig`` at call time,
    so one patch covers both -- but the file module resolves realpaths, and on
    Windows a temp dir arrives through a short-name link, so resolve here too
    or the sandbox comparison fails for reasons unrelated to the test.

    ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` are pinned *inside the user working
    directory* on purpose. That is the native install this suite has to model:
    the server's own login lives in the operator's home, the file tree browses
    the operator's home, and so the shared store sits in a directory the
    routes are otherwise happy to serve. Pinning them also keeps the suite off
    the developer's real ``~/.claude`` -- ``coding_cli_homes`` reads these
    variables live, which is exactly why it can be isolated this way.
    """
    root = tmp_path / "sysdir"
    root.mkdir()
    resolved = os.path.realpath(str(root))
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", resolved)
    monkeypatch.setattr(
        files_api, "get_user_working_directory", lambda: str(tmp_path / "work"),
    )
    (tmp_path / "work").mkdir()
    work = os.path.realpath(str(tmp_path / "work"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", os.path.join(work, ".claude"))
    monkeypatch.setenv("CODEX_HOME", os.path.join(work, ".codex"))
    return resolved


def _work(tmp_path) -> str:
    """The resolved user working directory the fixture installed."""
    return os.path.realpath(str(tmp_path / "work"))


def _seed_alice_logins(system_dir: str) -> dict[str, str]:
    """Give alice both kinds of coding-agent credential, as a sign-in would."""
    codex_home = homes.profile_codex_home("alice")
    codex_home.mkdir(parents=True, exist_ok=True)
    auth_json = codex_home / "auth.json"
    auth_json.write_text(json.dumps({"tokens": {"refresh_token": "ALICE-REFRESH"}}))

    claude_dir = homes.profile_claude_config_dir("alice")
    claude_dir.mkdir(parents=True, exist_ok=True)
    credentials = claude_dir / ".credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "ALICE-ACCESS"}}))

    # The API-key tier's managed home, which lives at the shared root.
    managed = os.path.join(system_dir, "codex-home", "abc123")
    os.makedirs(managed, exist_ok=True)
    managed_auth = os.path.join(managed, "auth.json")
    with open(managed_auth, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"OPENAI_API_KEY": "sk-managed"}))

    return {
        "codex": str(auth_json),
        "claude": str(credentials),
        "managed": managed_auth,
        "codex_dir": str(codex_home),
    }


def _status(response) -> int:
    return getattr(response, "status_code", 0)


def test_another_profile_cannot_open_a_coding_login(system_dir):
    """bob naming alice's codex auth.json by absolute path is refused."""
    seeded = _seed_alice_logins(system_dir)

    response = asyncio.run(
        files_api._serve_file_by_path(_Req(query={"path": seeded["codex"]}))
    )

    assert _status(response) == 403


def test_another_profile_cannot_open_a_claude_credential_dotfile(system_dir):
    """The dotfile name is not the protection -- the path rule is.

    ``{path:path}`` never filtered names, so this route returned
    ``.credentials.json`` as a plain FileResponse.
    """
    _seed_alice_logins(system_dir)

    response = asyncio.run(
        files_api._serve_file(
            _Req(path_params={"path": "alice/coding-cli/claude/.credentials.json"})
        )
    )

    assert _status(response) in (403, 404)


def test_the_managed_api_key_home_is_refused_too(system_dir):
    """``codex-home`` sits at the shared root rather than under a profile."""
    seeded = _seed_alice_logins(system_dir)

    response = asyncio.run(
        files_api._serve_file_by_path(_Req(query={"path": seeded["managed"]}))
    )

    assert _status(response) == 403


def test_a_credential_directory_cannot_be_listed(system_dir):
    """Refusing the file but listing the directory still names the target."""
    seeded = _seed_alice_logins(system_dir)

    response = asyncio.run(
        files_api._list_directory(_Req(query={"path": seeded["codex_dir"]}))
    )

    assert _status(response) == 403


@pytest.mark.parametrize("show_hidden", ["0", "1"])
def test_the_store_is_absent_from_its_parent_listing(system_dir, show_hidden):
    """And it is not advertised in the parent either, hidden files shown or not."""
    _seed_alice_logins(system_dir)

    response = asyncio.run(
        files_api._list_directory(
            _Req(query={
                "path": os.path.join(system_dir, "alice"),
                "show_hidden": show_hidden,
            })
        )
    )

    assert _status(response) == 200
    names = {entry["name"] for entry in json.loads(response.body)["entries"]}
    assert "coding-cli" not in names


def test_the_system_root_listing_hides_the_shared_store(system_dir):
    """The shared server login lives at the root; it must not show there."""
    _seed_alice_logins(system_dir)
    shared = os.path.join(system_dir, "coding-cli", "claude")
    os.makedirs(shared, exist_ok=True)

    response = asyncio.run(
        files_api._list_directory(_Req(query={"path": system_dir}))
    )

    assert _status(response) == 200
    names = {entry["name"] for entry in json.loads(response.body)["entries"]}
    assert "coding-cli" not in names
    assert "codex-home" not in names


def test_ordinary_profile_data_is_still_served(system_dir):
    """The guard is narrow: everything else under the System Directory works.

    Without this the fix could silently become "members cannot browse", which
    is a different change from "members cannot read credentials".
    """
    skill_dir = os.path.join(system_dir, "alice", "skills", "demo")
    os.makedirs(skill_dir, exist_ok=True)
    skill_file = os.path.join(skill_dir, "SKILL.md")
    with open(skill_file, "w", encoding="utf-8") as handle:
        handle.write("# demo\n")

    listed = asyncio.run(files_api._list_directory(_Req(query={"path": skill_dir})))
    assert _status(listed) == 200
    assert {e["name"] for e in json.loads(listed.body)["entries"]} == {"SKILL.md"}

    opened = asyncio.run(files_api._serve_file_by_path(_Req(query={"path": skill_file})))
    assert _status(opened) == 200


def test_a_traversal_into_a_store_is_refused(system_dir):
    """``..`` cannot walk back into a credential store from a served path."""
    seeded = _seed_alice_logins(system_dir)
    sideways = os.path.join(
        system_dir, "alice", "skills", "..", "coding-cli", "codex", "auth.json",
    )
    assert os.path.exists(seeded["codex"])

    response = asyncio.run(files_api._serve_file_by_path(_Req(query={"path": sideways})))

    assert _status(response) == 403


# -- Round two: the ways past the first guard ---------------------------------


def _move(src: str, dest: str):
    return asyncio.run(files_api._move_entry(_Req(body={"src": src, "dest": dest})))


def test_a_directory_holding_a_store_cannot_be_moved_out(system_dir, tmp_path):
    """The store is refused; carrying its parent out was the way around that.

    ``/move`` asked only whether the source *is* a store. A profile directory
    is not, so alice's whole directory -- tokens included -- could be moved
    into the user working directory, where the System-Directory-anchored rule
    stops applying and ``/open`` serves the file like any other.
    """
    seeded = _seed_alice_logins(system_dir)
    dest = os.path.join(_work(tmp_path), "alice")

    response = _move(os.path.join(system_dir, "alice"), dest)

    assert _status(response) == 403
    # Nothing moved: the tokens are still where only the runners can read them.
    assert os.path.exists(seeded["codex"])
    assert not os.path.exists(os.path.join(dest, "coding-cli", "codex", "auth.json"))


def test_the_system_directory_itself_cannot_be_moved_out(system_dir, tmp_path):
    """One level up is the same attack; ``_is_allowed_base`` already refuses it."""
    seeded = _seed_alice_logins(system_dir)

    response = _move(system_dir, os.path.join(_work(tmp_path), "everything"))

    assert _status(response) in (400, 403)
    assert os.path.exists(seeded["codex"])


def test_the_store_itself_is_still_refused_as_a_move_source(system_dir, tmp_path):
    """The direct half of the same rule, pinned so it cannot regress."""
    seeded = _seed_alice_logins(system_dir)

    response = _move(seeded["codex_dir"], os.path.join(_work(tmp_path), "stolen"))

    assert _status(response) == 403
    assert os.path.exists(seeded["codex"])


def test_a_move_may_not_create_a_directory_wearing_a_store_name(system_dir, tmp_path):
    """Only the dest's *parent* was validated, never the leaf.

    A directory called ``coding-cli`` is filtered out of every listing, so one
    created in the user's own working directory swallows whatever is then put
    in it -- and under the System Directory it is the path the next sign-in
    writes a token to.
    """
    work = _work(tmp_path)
    plain = os.path.join(work, "notes.txt")
    with open(plain, "w", encoding="utf-8") as handle:
        handle.write("x")

    response = _move(plain, os.path.join(work, "coding-cli"))

    assert _status(response) == 403
    assert not os.path.exists(os.path.join(work, "coding-cli"))
    assert os.path.exists(plain)


def test_mkdir_may_not_create_a_directory_wearing_a_store_name(system_dir, tmp_path):
    """``/mkdir`` composes its target the same way ``/move`` composes ``dest``."""
    work = _work(tmp_path)

    response = asyncio.run(
        files_api._mkdir(_Req(body={"path": os.path.join(work, "codex-home")}))
    )

    assert _status(response) == 403
    assert not os.path.isdir(os.path.join(work, "codex-home"))


def test_an_ordinary_rename_is_unaffected(system_dir, tmp_path):
    """The guard is narrow: normal files still move."""
    work = _work(tmp_path)
    src = os.path.join(work, "before.txt")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write("x")

    response = _move(src, os.path.join(work, "after.txt"))

    assert _status(response) == 200
    assert os.path.exists(os.path.join(work, "after.txt"))


def test_a_folder_of_ordinary_files_still_moves(system_dir, tmp_path):
    """Containment must mean "holds a store", not "is a directory"."""
    work = _work(tmp_path)
    src = os.path.join(work, "project", "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "main.py"), "w", encoding="utf-8") as handle:
        handle.write("x")

    response = _move(os.path.join(work, "project"), os.path.join(work, "renamed"))

    assert _status(response) == 200
    assert os.path.exists(os.path.join(work, "renamed", "src", "main.py"))


# -- The shared server login, wherever it points ------------------------------


def _seed_shared_native_login(tmp_path) -> dict[str, str]:
    """The operator's own ``claude``/``codex`` login, as a native install has it.

    Written through ``coding_cli_homes`` rather than at a hardcoded path, so
    this is by construction the directory a run would authenticate from.
    """
    claude_dir = homes.shared_claude_config_dir()
    claude_dir.mkdir(parents=True, exist_ok=True)
    credentials = claude_dir / ".credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "OP-ACCESS"}}))

    codex_home = homes.shared_codex_home()
    codex_home.mkdir(parents=True, exist_ok=True)
    auth_json = codex_home / "auth.json"
    auth_json.write_text(json.dumps({"tokens": {"refresh_token": "OP-REFRESH"}}))

    return {
        "claude": str(credentials),
        "claude_dir": str(claude_dir),
        "codex": str(auth_json),
        "codex_dir": str(codex_home),
    }


def test_the_shared_login_is_refused_although_it_is_outside_the_system_dir(
    system_dir, tmp_path,
):
    """It carries no name the guard can match, so its location has to be asked for.

    The per-profile stores are named by Cremind; the server's own home is
    named by the operator (or by ``~``), and on a native install it sits in the
    very directory the file tree browses.
    """
    seeded = _seed_shared_native_login(tmp_path)

    opened = asyncio.run(
        files_api._serve_file_by_path(_Req(query={"path": seeded["claude"]}))
    )
    listed = asyncio.run(
        files_api._list_directory(_Req(query={"path": seeded["codex_dir"]}))
    )

    assert _status(opened) == 403
    assert _status(listed) == 403


def test_the_shared_login_is_hidden_from_the_working_directory_listing(
    system_dir, tmp_path,
):
    """Shown-hidden-files is a display toggle, not a credential toggle."""
    _seed_shared_native_login(tmp_path)

    response = asyncio.run(
        files_api._list_directory(
            _Req(query={"path": _work(tmp_path), "show_hidden": "1"})
        )
    )

    assert _status(response) == 200
    names = {entry["name"] for entry in json.loads(response.body)["entries"]}
    assert ".claude" not in names
    assert ".codex" not in names


def test_a_directory_holding_the_shared_login_cannot_be_moved(
    system_dir, tmp_path, monkeypatch,
):
    """The move rule has to follow the shared home too, not just the named ones.

    Moving the home's parent is a disclosure and not merely a nuisance: the env
    var still names the old path, so the copy at the new one is not the shared
    home any more by any rule the guard applies -- it is an ordinary directory
    full of tokens, inside the working directory, served by ``/open``.
    """
    work = _work(tmp_path)
    monkeypatch.setenv("CODEX_HOME", os.path.join(work, "nested", ".codex"))
    seeded = _seed_shared_native_login(tmp_path)

    response = _move(os.path.join(work, "nested"), os.path.join(work, "moved"))

    assert _status(response) == 403
    assert os.path.exists(seeded["codex"])
    assert not os.path.exists(os.path.join(work, "moved", ".codex", "auth.json"))


# -- POST /cwd: the one route that accepts a path outside the allowlist -------


class _FakeStorage:
    def __init__(self, rows: dict[str, dict]):
        self.rows = rows
        self.updates: list[tuple[str, dict]] = []

    async def get_conversation(self, conversation_id):
        return self.rows.get(conversation_id)

    async def update_conversation(self, conversation_id, **kwargs):
        self.updates.append((conversation_id, kwargs))


class _RecordingBus:
    def __init__(self):
        self.published: list[tuple[str, str, dict]] = []

    async def publish(self, channel, event, payload):
        self.published.append((channel, event, payload))


@pytest.fixture()
def conversation(monkeypatch):
    """One conversation owned by ``bob``, plus a bus that records publishes."""
    storage = _FakeStorage(
        {CONV: {"id": CONV, "context_id": CONV, "profile": "bob"}}
    )
    import app.events.runner as runner

    monkeypatch.setattr(runner, "get_conversation_storage", lambda: storage)
    bus = _RecordingBus()
    monkeypatch.setattr(files_api, "get_event_stream_bus", lambda: bus)
    try:
        yield storage, bus
    finally:
        clear_context(CONV, WORKING_DIR_OVERRIDE_KEY)


def _set_cwd(path: str):
    return asyncio.run(
        files_api._set_cwd(_Req(body={"conversation_id": CONV, "path": path}))
    )


@pytest.mark.parametrize("which", ["profile", "shared"])
def test_set_cwd_may_not_aim_a_conversation_at_a_credential_store(
    system_dir, tmp_path, conversation, which,
):
    """``/cwd`` takes paths outside the allowlist, so it needs the rule stated.

    The override does not make the file routes serve the store -- they check
    the rule first -- but it does point the agent's own shell, and every tool
    that follows the cwd, straight at the tokens.
    """
    storage, bus = conversation
    target = (
        _seed_alice_logins(system_dir)["codex_dir"] if which == "profile"
        else _seed_shared_native_login(tmp_path)["claude_dir"]
    )

    response = _set_cwd(target)

    assert _status(response) == 403
    assert get_context(CONV, WORKING_DIR_OVERRIDE_KEY) is None
    assert storage.updates == []
    assert bus.published == []


def test_set_cwd_still_accepts_an_ordinary_directory(system_dir, tmp_path, conversation):
    """Narrowness again: the UI's own folder-picker must keep working."""
    storage, _bus = conversation
    chosen = os.path.join(_work(tmp_path), "project")
    os.makedirs(chosen, exist_ok=True)

    response = _set_cwd(chosen)

    assert _status(response) == 200
    assert get_context(CONV, WORKING_DIR_OVERRIDE_KEY) == os.path.realpath(chosen)
    assert storage.updates == [(CONV, {"working_directory": os.path.realpath(chosen)})]


# -- The change stream --------------------------------------------------------


class _StubObserver:
    """Watchdog's Observer, minus the thread: the handler is what's on test."""

    captured: dict = {}

    def schedule(self, handler, path, recursive=False):
        _StubObserver.captured["handler"] = handler
        _StubObserver.captured["recursive"] = recursive

    def start(self):
        return None

    def stop(self):
        return None

    def join(self, timeout=None):
        return None


def _event(path: str, is_directory: bool = False, dest_path: str = ""):
    return SimpleNamespace(
        src_path=path, dest_path=dest_path, is_directory=is_directory,
    )


def test_the_change_stream_does_not_re_advertise_a_hidden_store(
    system_dir, tmp_path, monkeypatch,
):
    """The watcher is recursive and filtered nothing.

    A watch on the System Directory (or on a profile directory) sees every
    write inside a credential store, so the SSE stream handed back the very
    names -- and the ``auth.json`` path -- that the listing filter hides.
    """
    seeded = _seed_alice_logins(system_dir)
    monkeypatch.setattr(files_api, "Observer", _StubObserver)
    _StubObserver.captured = {}
    ordinary = os.path.join(system_dir, "alice", "skills", "SKILL.md")

    async def _drive():
        response = await files_api._watch_directory(
            _Req(query={"path": system_dir}, connected=True)
        )
        assert _status(response) == 200
        frames = response.body_iterator.__aiter__()
        assert b"ready" in await frames.__anext__()

        handler = _StubObserver.captured["handler"]
        # Credential traffic first: if it is forwarded, it arrives first too.
        handler.on_created(_event(seeded["codex"]))
        handler.on_deleted(_event(seeded["codex_dir"], is_directory=True))
        handler.on_moved(_event(ordinary, dest_path=seeded["codex"]))
        handler.on_created(_event(ordinary))

        frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
        await frames.aclose()
        return json.loads(frame.decode("utf-8").split("data: ", 1)[1])

    payload = asyncio.run(_drive())

    assert payload == {"type": "created", "path": ordinary, "is_dir": False}
