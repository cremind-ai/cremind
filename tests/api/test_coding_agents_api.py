"""``/api/coding-agents`` - readiness, CLI sign-in, sign-out and CLI location.

Same harness as ``test_tools_config_api.py``: the route endpoints are driven
directly with a fake Request, backed by a real ``ToolRegistry`` on a temp SQLite
DB. Pins the contract the Coding Agents card and ``cremind tools coding-agents``
both read:

- both delegates are always listed, always in the order claude_code, codex -
  including one whose feature is not installed (that is the row worth showing);
- every row says where the credential comes from, whose login it is
  (``credential_scope``), which CLI home it lives in, and whether the login
  *binary* is even present - the card's Sign-in button runs that binary, and an
  SDK can be installed without one;
- ``sign_in`` describes a command, never a route into Settings -> LLM Providers:
  the ``claude`` CLI keeps its login independently of the Anthropic provider
  credentials, so sending the user there was sending them to the wrong page;
- ``probe`` runs the real status leaf, coalesces + briefly caches per profile,
  and ``{"fresh": true}`` is the escape hatch for "I just changed this";
- the Codex device-code trio starts, polls and cancels one sign-in, and a poll
  for another profile's id is a 403 (a 404 would tell a user their own sign-in
  had expired);
- ``login-terminal`` spawns the login binary itself under the PTY with the
  profile's home FORCED, and ``shared`` - the login every other profile
  inherits - is admin-only; inside a container it also takes ``DISPLAY`` away,
  or the CLI opens a browser on the VNC desktop instead of printing the URL
  into the terminal the user is looking at;
- ``logout`` refuses to sign a profile out of a login it does not own, which is
  the one refusal the UI cannot be trusted to make on its own (the CLI hits the
  same route);
- a host whose CPU cannot run the delegate's CLI is reported on every surface
  and reported FIRST - the listing's sentence, a 409 from ``login-terminal``
  with nothing spawned, and ``cli_blocked`` in the ``/cli`` payload so the shell
  door refuses too. On such a host "install it" / "sign in" / "switch it on"
  are all still true and all beside the point.

Isolation is by environment: ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` point at
empty temp directories and ``BaseConfig.CREMIND_SYSTEM_DIR`` at ``tmp_path``, so
the developer's own ``claude auth login`` can never decide an assertion.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402 - registers tables on Base.metadata
from sqlalchemy import text  # noqa: E402

import app.api.coding_agents as coding_agents_api  # noqa: E402
import app.api.terminals as terminals_api  # noqa: E402
from app.config import runtime_env  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.tool_storage import ToolStorage  # noqa: E402
from app.tools.builtin import claude_code_runner, codex_runner  # noqa: E402
from app.tools.builtin import codex_login  # noqa: E402
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult  # noqa: E402
from app.tools.builtin.tool import BuiltInToolGroup  # noqa: E402
from app.tools.config_manager import ToolConfigManager  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402


class _FakeLeaf(BuiltInTool):
    name = "noop"
    description = "fake leaf"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:  # pragma: no cover
        return BuiltInToolResult(structured_content={"ok": True})


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch, tmp_path):
    """Make credential resolution depend only on what a test seeds.

    Both runners resolve a CLI home and ask whether it holds a login, and the
    machine running the suite may well be signed in to both. Pointing the two
    home variables at empty temp directories (and the System Directory at
    ``tmp_path``, which is where the per-profile homes hang off) is the whole
    isolation story - the modules read those live, so nothing private needs
    patching.
    """
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    shared_claude = tmp_path / "shared" / "claude"
    shared_codex = tmp_path / "shared" / "codex"
    shared_claude.mkdir(parents=True)
    shared_codex.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_claude))
    monkeypatch.setenv("CODEX_HOME", str(shared_codex))
    # The container branch adds a legacy ``~`` tier to home resolution, and the
    # suite may itself run inside Docker.
    monkeypatch.setattr(runtime_env, "is_container", lambda: False, raising=False)
    for var in (
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_cli_binaries(monkeypatch):
    """No ``claude`` / ``codex`` on this box, and a CPU that can run both.

    ``find_cli`` ends at ``shutil.which``, so a developer with either CLI on
    PATH would otherwise flip ``cli_available`` and let the sign-in routes past
    their "no binary" refusal.

    ``host_blocker`` is pinned to "nothing known to be wrong" for the same
    reason and one more: it reads the real /proc/cpuinfo, so on a machine that
    genuinely lacks the x86-64-v2 instructions every assertion in this file
    about a listing message or a sign-in refusal would change meaning - the
    blocker leads the summary and precedes both refusals by design. Tests that
    want that host say so with :func:`_block_host`."""
    for runner in (claude_code_runner, codex_runner):
        monkeypatch.setattr(runner, "find_cli", lambda variables=None: None)
        monkeypatch.setattr(runner, "cli_binary_source", lambda variables=None: None)
        monkeypatch.setattr(runner, "host_blocker", lambda variables=None: None)


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    """The probe coalescing state is module-level, so one test's cached answer
    would otherwise decide the next test's assertion."""
    def _clear():
        coding_agents_api._probe_cache.clear()
        coding_agents_api._probe_locks.clear()
        coding_agents_api._login_observed.clear()

    _clear()
    yield
    _clear()


@pytest.fixture
def sdk_present(monkeypatch):
    """Speak for a server where the delegate's extra IS installed.

    Every route that spawns something refuses when the feature is absent, and
    the suite runs on machines with and without the optional extras - so the
    answer has to be pinned rather than inherited from whoever's venv is
    running the tests."""
    import app.features.manifest as manifest

    monkeypatch.setattr(manifest, "is_installed", lambda key: True)


@pytest.fixture
def sdk_absent(monkeypatch):
    import app.features.manifest as manifest

    monkeypatch.setattr(manifest, "is_installed", lambda key: False)


def _with_cli(monkeypatch, runner, binary: str, source: str = "bundled") -> None:
    monkeypatch.setattr(runner, "find_cli", lambda variables=None: binary)
    monkeypatch.setattr(runner, "cli_binary_source", lambda variables=None: source)


# The blocker one runner hands back on the node this feature was written for: a
# QEMU virtual CPU advertising none of the four x86-64-v2 flags the bundled
# Claude Code executable was built for. Kept as data rather than produced by
# pointing ``_CPUINFO_PATH`` at a fixture file, because these tests are about
# what the ROUTES do with a blocker; the CPU probe itself is pinned where it
# lives (tests/config, tests/tools).
_CPU_BLOCKER: Dict[str, Any] = {
    "code": claude_code_runner.HOST_BLOCKER_CPU,
    "cpu_model": "QEMU Virtual CPU version 2.5+",
    "missing": ["ssse3", "sse4_1", "sse4_2", "popcnt"],
    "hypervisor": True,
    "message": (
        "The Claude Code CLI cannot run on this server's CPU: it is a Bun "
        "single-file executable built for the x86-64-v2 instruction level, and "
        'this virtual CPU "QEMU Virtual CPU version 2.5+" advertises none of '
        "ssse3, sse4_1, sse4_2, popcnt."
    ),
    "remedy": (
        "The fix is on the hypervisor: in Proxmox, set the VM's Hardware -> "
        "Processors -> Type to 'host'. The node has to be shut down and started "
        "again afterwards."
    ),
}


def _block_host(monkeypatch, runner, blocker: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Speak for a host whose CPU cannot run ``runner``'s CLI.

    One runner at a time on purpose: the two delegates answer this question
    independently (Codex declares no instruction-set requirement at all), and a
    fixture that blocked both could not show that.
    """
    payload = dict(blocker or _CPU_BLOCKER)
    monkeypatch.setattr(runner, "host_blocker", lambda variables=None: dict(payload))
    return payload


def _seed_profile(storage: ToolStorage, name: str) -> None:
    now = time.time() * 1000
    with storage._engine.begin() as conn:  # noqa: SLF001 - test seeding
        conn.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES (:id, :name, :c, :u)"
            ),
            {"id": name, "name": name, "c": now, "u": now},
        )


def _make_registry(tmp_path: Path, *, register: tuple[str, ...] = ("claude_code", "codex")) -> ToolRegistry:
    provider = SqliteDatabaseProvider(str(tmp_path / "tools.db"))
    Base.metadata.create_all(bind=provider.sync_engine())
    storage = ToolStorage(provider)
    reg = ToolRegistry(storage, ToolConfigManager(storage))
    _seed_profile(storage, "admin")
    names = {"claude_code": "Claude Code", "codex": "Codex"}
    for config_name in register:
        reg.register_builtin(
            BuiltInToolGroup(
                config_name=config_name, display_name=names[config_name],
                description=config_name, functions=[_FakeLeaf()], llm=object(),
            ),
            source=config_name,
        )
    return reg


# -- CLI homes the tests seed -------------------------------------------------

def _profile_claude_home(tmp_path: Path, profile: str = "admin") -> Path:
    return tmp_path / profile / "coding-cli" / "claude"


def _profile_codex_home(tmp_path: Path, profile: str = "admin") -> Path:
    return tmp_path / profile / "coding-cli" / "codex"


def _shared_claude_home(tmp_path: Path) -> Path:
    return tmp_path / "shared" / "claude"


def _shared_codex_home(tmp_path: Path) -> Path:
    return tmp_path / "shared" / "codex"


def _sign_in_claude(config_dir: Path, email: str = "dev@example.com") -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-01"}}), encoding="utf-8",
    )
    (config_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email, "organizationName": "Acme"}}),
        encoding="utf-8",
    )


def _sign_in_codex(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": None, "tokens": {"id_token": ""}}), encoding="utf-8",
    )


# -- request / handler plumbing ----------------------------------------------

def _handler(state, path: str, method: str) -> Callable:
    for route in coding_agents_api.get_coding_agents_routes(state):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _list_handler(state) -> Callable:
    return _handler(state, "/api/coding-agents", "GET")


def _probe_handler(state) -> Callable:
    return _handler(state, "/api/coding-agents/{tool_id}/probe", "POST")


def _login_start_handler(state) -> Callable:
    return _handler(state, "/api/coding-agents/codex/login", "POST")


def _login_get_handler(state) -> Callable:
    return _handler(state, "/api/coding-agents/codex/login/{login_id}", "GET")


def _login_cancel_handler(state) -> Callable:
    return _handler(state, "/api/coding-agents/codex/login/{login_id}/cancel", "POST")


def _login_terminal_handler(state) -> Callable:
    return _handler(state, "/api/coding-agents/{tool_id}/login-terminal", "POST")


def _logout_handler(state) -> Callable:
    return _handler(state, "/api/coding-agents/{tool_id}/logout", "POST")


def _cli_handler(state) -> Callable:
    return _handler(state, "/api/coding-agents/{tool_id}/cli", "GET")


def _req(username="admin", path_params=None, *, authed=True, body=None):
    async def _json():
        if body is None:
            raise ValueError("no body")
        return body
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=authed, username=username),
        path_params=path_params or {},
        query_params={},
        json=_json,
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


def _agents(tmp_path: Path, **kwargs) -> list[dict]:
    reg = _make_registry(tmp_path, **kwargs)
    state = SimpleNamespace(registry=reg)
    resp = asyncio.run(_list_handler(state)(_req()))
    assert resp.status_code == 200
    return _body(resp)["agents"]


def _by_id(agents: list[dict]) -> dict[str, dict]:
    return {a["tool_id"]: a for a in agents}


# -- listing -----------------------------------------------------------------

def test_lists_both_agents_in_order(tmp_path: Path) -> None:
    agents = _agents(tmp_path)
    assert [a["tool_id"] for a in agents] == ["claude_code", "codex"]
    cc, cx = agents
    assert cc["display_name"] == "Claude Code"
    assert cc["feature_key"] == "claude_code"
    assert cc["extras"] == ["claude-code"]
    assert cc["requires_restart_after_install"] is False
    # Every row carries a one-sentence summary for the card / CLI stderr note.
    assert all(a["message"] for a in agents)


def test_sign_in_describes_a_cli_command_not_a_settings_page(tmp_path: Path) -> None:
    """The one thing the previous version got wrong: the Claude CLI's login is
    independent of the Anthropic provider credentials, so a descriptor that
    routed the user into Settings -> LLM Providers sent them to a page the
    coding tool never reads."""
    agents = _by_id(_agents(tmp_path))
    claude = agents["claude_code"]["sign_in"]
    codex = agents["codex"]["sign_in"]
    assert claude["method"] == "terminal"
    assert claude["cli_login"] == "claude auth login"
    assert claude["cli_logout"] == "claude auth logout"
    assert codex["method"] == "device_code"
    assert codex["cli_login"] == "codex login --device-auth"
    assert codex["cli_logout"] == "codex logout"
    for sign_in in (claude, codex):
        assert sign_in["label"] and sign_in["instructions"]
        assert "provider" not in sign_in
        assert "auth_method" not in sign_in
        assert "route" not in sign_in


def test_sdk_installed_follows_feature_probe(tmp_path: Path, monkeypatch) -> None:
    import app.features.manifest as manifest

    monkeypatch.setattr(manifest, "is_installed", lambda key: key == "claude_code")
    agents = _by_id(_agents(tmp_path))
    assert agents["claude_code"]["sdk_installed"] is True
    assert agents["codex"]["sdk_installed"] is False
    # The uninstalled row's summary names the install as the next step.
    assert "not installed" in agents["codex"]["message"]


def test_enabled_reflects_the_profile_row(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    reg.set_profile_tool_enabled("admin", "claude_code", True)
    reg.set_profile_tool_enabled("admin", "codex", False)
    state = SimpleNamespace(registry=reg)
    agents = _by_id(_body(asyncio.run(_list_handler(state)(_req())))["agents"])
    assert agents["claude_code"]["enabled"] is True
    assert agents["codex"]["enabled"] is False


def test_agent_absent_from_registry_reports_disabled(tmp_path: Path) -> None:
    """A feature that was never installed never registered its tool group, so
    the row is missing from the registry - that must read as off, not blow up."""
    agents = _by_id(_agents(tmp_path, register=("claude_code",)))
    assert agents["codex"]["enabled"] is False
    assert agents["codex"]["tool_id"] == "codex"


def test_credential_source_surfaces(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    reg.config.set_variable(
        "claude_code", "admin", "CLAUDE_CODE_API_KEY", "sk-ant-test", is_secret=True,
    )
    state = SimpleNamespace(registry=reg)
    agents = _by_id(_body(asyncio.run(_list_handler(state)(_req())))["agents"])
    assert agents["claude_code"]["credential_source"] == "tool_variable_api_key"
    assert agents["claude_code"]["credentials_configured"] is True
    # An API key has no login behind it, so there is nothing to scope and
    # nothing the card could offer to sign out of.
    assert agents["claude_code"]["credential_scope"] is None
    # Nothing was seeded for Codex, and the fixture cleared every ambient source.
    assert agents["codex"]["credential_source"] is None
    assert agents["codex"]["credentials_configured"] is False
    assert agents["codex"]["credential_scope"] is None


def test_credential_source_is_per_profile(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "other")
    reg.config.set_variable(
        "claude_code", "admin", "CLAUDE_CODE_API_KEY", "sk-ant-test", is_secret=True,
    )
    state = SimpleNamespace(registry=reg)
    other = _by_id(
        _body(asyncio.run(_list_handler(state)(_req(username="other"))))["agents"]
    )
    assert other["claude_code"]["credential_source"] is None


def test_a_profiles_own_login_is_reported_with_its_home_and_account(
    tmp_path: Path, sdk_present,
) -> None:
    # The summary asserted below is the one an INSTALLED agent gets; without
    # the extra, every row says "not installed" instead and the credential
    # sentence never appears. Pinned, not inherited from the venv running the
    # tests: the optional extras are present on a dev box and absent in CI.
    _sign_in_claude(_profile_claude_home(tmp_path), email="dev@example.com")
    row = _by_id(_agents(tmp_path))["claude_code"]
    assert row["credential_source"] == "profile_claude_login"
    assert row["credential_scope"] == "profile"
    assert Path(row["cli_home"]) == _profile_claude_home(tmp_path)
    assert row["account_hint"]["email"] == "dev@example.com"
    assert "this profile's own login" in row["message"]


def test_an_inherited_server_login_is_reported_as_shared(
    tmp_path: Path, sdk_present,
) -> None:
    """A profile that never signed in still gets the server's login - and has
    to be told that is whose it is, because it is not one it can sign out.

    ``sdk_present`` for the same reason as the test above: the sentence only
    exists on an installed row."""
    _sign_in_codex(_shared_codex_home(tmp_path))
    row = _by_id(_agents(tmp_path))["codex"]
    assert row["credential_source"] == "host_codex_login"
    assert row["credential_scope"] == "shared"
    assert Path(row["cli_home"]) == _shared_codex_home(tmp_path)
    assert "shared server login" in row["message"]


def test_cli_available_follows_the_binary_not_the_sdk(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """The SDK wheel can be installed with no binary in it, and the Sign-in
    button runs the binary - so the card gates on this, not on sdk_installed."""
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    agents = _by_id(_agents(tmp_path))
    assert agents["claude_code"]["cli_available"] is True
    assert agents["codex"]["cli_available"] is False
    assert agents["codex"]["sdk_installed"] is True
    assert "command-line tool is not on this server" in agents["codex"]["message"]


def test_a_blocked_host_leads_the_row_even_when_everything_else_is_ready(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """The ordering claim, stated against the hardest case for it.

    This row has the extra installed, a login of its own, a binary on disk and
    the tool switched on - every other branch of the summary is either satisfied
    or irrelevant - and the sentence is still the blocker's. That is the whole
    point: on this host "signed in and using profile_claude_login" would be true
    and useless, because the binary that credential is for cannot run.
    """
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    blocker = _block_host(monkeypatch, claude_code_runner)
    _sign_in_claude(_profile_claude_home(tmp_path))
    reg = _make_registry(tmp_path)
    reg.set_profile_tool_enabled("admin", "claude_code", True)
    agents = _by_id(
        _body(asyncio.run(_list_handler(SimpleNamespace(registry=reg))(_req())))["agents"]
    )

    row = agents["claude_code"]
    assert row["cli_blocked"]["code"] == claude_code_runner.HOST_BLOCKER_CPU
    assert row["cli_blocked"]["cpu_model"] == "QEMU Virtual CPU version 2.5+"
    assert row["cli_blocked"]["missing"] == ["ssse3", "sse4_1", "sse4_2", "popcnt"]
    assert row["message"] == blocker["message"]
    # The remedy is a paragraph and stays out of the sentence - the card renders
    # it from ``cli_blocked``, and repeating it here would be the summary's whole
    # length twice over.
    assert blocker["remedy"] not in row["message"]
    assert row["cli_blocked"]["remedy"] == blocker["remedy"]
    # Everything else on the row is still reported honestly; the blocker decides
    # what to SAY, not what to hide.
    assert row["credential_source"] == "profile_claude_login"
    assert row["enabled"] is True
    assert row["cli_available"] is True

    # And it is per delegate: Codex is a Rust binary built for the x86-64
    # baseline, declares no such requirement, and must not inherit its
    # neighbour's verdict in the same response.
    assert agents["codex"]["cli_blocked"] is None
    assert agents["codex"]["message"] != blocker["message"]


def test_a_blocked_host_also_leads_the_not_installed_sentence(
    tmp_path: Path, monkeypatch, sdk_absent,
) -> None:
    """Installing the extra is the usual next step and here it is the wrong one:
    the wheel would arrive with the very executable this CPU cannot run."""
    blocker = _block_host(monkeypatch, claude_code_runner)
    row = _by_id(_agents(tmp_path))["claude_code"]
    assert row["message"] == blocker["message"]
    assert "not installed" not in row["message"]


def test_list_unauthenticated_401(tmp_path: Path) -> None:
    state = SimpleNamespace(registry=_make_registry(tmp_path))
    resp = asyncio.run(_list_handler(state)(_req(authed=False)))
    assert resp.status_code == 401


def test_list_registry_none_503() -> None:
    state = SimpleNamespace(registry=None)
    resp = asyncio.run(_list_handler(state)(_req()))
    assert resp.status_code == 503


# -- probe -------------------------------------------------------------------

class _StubStatusLeaf:
    def __init__(self, payload: dict):
        self.payload = payload
        self.seen: Dict[str, Any] = {}

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        self.seen = arguments
        return BuiltInToolResult(structured_content=self.payload)


class _CountingStatusLeaf:
    """Records how many times it actually ran, and yields to the event loop
    while it does so two concurrent callers genuinely overlap."""

    def __init__(self) -> None:
        self.runs = 0
        self.profiles: list = []

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        self.runs += 1
        self.profiles.append(arguments.get("_profile"))
        await asyncio.sleep(0.05)
        return BuiltInToolResult(structured_content={"logged_in": True, "run": self.runs})


def test_probe_returns_the_status_payload(tmp_path: Path, monkeypatch, sdk_present) -> None:
    reg = _make_registry(tmp_path)
    reg.config.set_variable(
        "codex", "admin", "CODEX_API_KEY", "sk-codex", is_secret=True,
    )
    leaf = _StubStatusLeaf({
        "available": True,
        "sdk_installed": True,
        "logged_in": True,
        "probe_detail": "Codex has an active account credential.",
    })
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    state = SimpleNamespace(registry=reg)
    resp = asyncio.run(_probe_handler(state)(_req(path_params={"tool_id": "codex"})))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["tool_id"] == "codex"
    assert body["logged_in"] is True
    # The leaf ran the same way the agent runs it: probe on, this profile, this
    # profile's variables (secrets included) and a real working directory.
    assert leaf.seen["probe"] is True
    assert leaf.seen["_profile"] == "admin"
    assert leaf.seen["_variables"]["CODEX_API_KEY"] == "sk-codex"
    assert leaf.seen["_working_directory"]


def test_probe_unknown_tool_id_400(tmp_path: Path) -> None:
    state = SimpleNamespace(registry=_make_registry(tmp_path))
    resp = asyncio.run(_probe_handler(state)(_req(path_params={"tool_id": "exec_shell"})))
    assert resp.status_code == 400
    assert "claude_code" in _body(resp)["error"]


def test_probe_failure_is_reported_not_raised(tmp_path: Path, monkeypatch, sdk_present) -> None:
    """A diagnostic that 500s tells the user nothing - the failure itself is
    the answer, so it comes back as a 200 body with ``error``."""
    class _Boom:
        async def run(self, arguments):
            raise RuntimeError("app-server would not start")

    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: _Boom())
    state = SimpleNamespace(registry=_make_registry(tmp_path))
    resp = asyncio.run(
        _probe_handler(state)(_req(path_params={"tool_id": "claude_code"}))
    )
    assert resp.status_code == 200
    assert "app-server would not start" in _body(resp)["error"]


def test_probe_unauthenticated_401(tmp_path: Path) -> None:
    state = SimpleNamespace(registry=_make_registry(tmp_path))
    resp = asyncio.run(
        _probe_handler(state)(_req(authed=False, path_params={"tool_id": "codex"}))
    )
    assert resp.status_code == 401


def test_probe_registry_none_503() -> None:
    state = SimpleNamespace(registry=None)
    resp = asyncio.run(_probe_handler(state)(_req(path_params={"tool_id": "codex"})))
    assert resp.status_code == 503


# -- probe gating: installed, but not necessarily enabled --------------------

def test_probe_refuses_when_the_feature_is_not_installed(
    tmp_path: Path, monkeypatch, sdk_absent,
) -> None:
    """With the extra absent there is no binary to spawn and nothing to learn,
    so the route must not start work for a delegate this server never got."""
    leaf = _StubStatusLeaf({"logged_in": True})
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    state = SimpleNamespace(registry=_make_registry(tmp_path))
    resp = asyncio.run(_probe_handler(state)(_req(path_params={"tool_id": "codex"})))
    assert resp.status_code == 409
    assert "not installed" in _body(resp)["error"]
    # And the refusal happened before anything ran.
    assert leaf.seen == {}


def test_probe_stays_available_while_the_tool_is_switched_off(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """"Check sign-in" is a PRE-ENABLE diagnostic - it answers "will this work
    if I switch it on?" - so the probe is deliberately not gated on the
    per-profile enabled flag, only on the feature being installed."""
    reg = _make_registry(tmp_path)
    reg.set_profile_tool_enabled("admin", "codex", False)
    leaf = _StubStatusLeaf({"logged_in": True})
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    state = SimpleNamespace(registry=reg)
    resp = asyncio.run(_probe_handler(state)(_req(path_params={"tool_id": "codex"})))
    assert resp.status_code == 200
    assert _body(resp)["logged_in"] is True


# -- probe coalescing + cache ------------------------------------------------

def test_concurrent_probes_run_the_leaf_once(tmp_path: Path, monkeypatch, sdk_present) -> None:
    """Two clicks in flight at once must not become two subprocesses and two
    billed upstream calls: the second caller waits for the first and reads its
    answer."""
    leaf = _CountingStatusLeaf()
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    handler = _probe_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    async def _both():
        return await asyncio.gather(
            handler(_req(path_params={"tool_id": "codex"})),
            handler(_req(path_params={"tool_id": "codex"})),
        )

    first, second = asyncio.run(_both())
    assert leaf.runs == 1
    assert _body(first)["run"] == 1
    assert _body(second)["run"] == 1


def test_repeat_probe_inside_the_ttl_is_served_from_cache(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    leaf = _CountingStatusLeaf()
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    handler = _probe_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    async def _twice():
        a = await handler(_req(path_params={"tool_id": "codex"}))
        b = await handler(_req(path_params={"tool_id": "codex"}))
        return a, b

    first, second = asyncio.run(_twice())
    assert leaf.runs == 1
    assert _body(second) == _body(first)


def test_fresh_bypasses_the_cache(tmp_path: Path, monkeypatch, sdk_present) -> None:
    """"Check sign-in" and a just-closed login dialog send ``fresh``: the user
    has changed the very thing being reported, so the held answer predates the
    change and re-reporting it would look like the sign-in did nothing."""
    leaf = _CountingStatusLeaf()
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    handler = _probe_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    async def _twice():
        await handler(_req(path_params={"tool_id": "codex"}))
        return await handler(_req(path_params={"tool_id": "codex"}, body={"fresh": True}))

    second = asyncio.run(_twice())
    assert leaf.runs == 2
    assert _body(second)["run"] == 2


def test_probe_runs_again_once_the_ttl_has_passed(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """The window is a burst absorber, not a memo: a user who goes and fixes
    their credential has to be able to get a fresh answer."""
    monkeypatch.setattr(coding_agents_api, "_PROBE_CACHE_TTL", 0.0)
    leaf = _CountingStatusLeaf()
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    handler = _probe_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    async def _twice():
        await handler(_req(path_params={"tool_id": "codex"}))
        return await handler(_req(path_params={"tool_id": "codex"}))

    second = asyncio.run(_twice())
    assert leaf.runs == 2
    assert _body(second)["run"] == 2


def test_probe_cache_never_crosses_profiles(tmp_path: Path, monkeypatch, sdk_present) -> None:
    """The answer is "is THIS profile signed in?", so it is keyed by profile:
    a second profile gets its own run, never the first profile's verdict."""
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "other")
    leaf = _CountingStatusLeaf()
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    handler = _probe_handler(SimpleNamespace(registry=reg))

    async def _both():
        a = await handler(_req(username="admin", path_params={"tool_id": "codex"}))
        b = await handler(_req(username="other", path_params={"tool_id": "codex"}))
        return a, b

    first, second = asyncio.run(_both())
    assert leaf.runs == 2
    assert leaf.profiles == ["admin", "other"]
    assert _body(first)["run"] == 1
    assert _body(second)["run"] == 2


def test_probe_of_a_different_tool_is_not_served_from_the_first(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    leaf = _CountingStatusLeaf()
    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: leaf)
    handler = _probe_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    async def _both():
        await handler(_req(path_params={"tool_id": "codex"}))
        return await handler(_req(path_params={"tool_id": "claude_code"}))

    second = asyncio.run(_both())
    assert leaf.runs == 2
    assert _body(second)["tool_id"] == "claude_code"


def test_a_failed_probe_is_not_cached(tmp_path: Path, monkeypatch, sdk_present) -> None:
    """A transient failure (the app-server did not start) must not stick for the
    whole window - the next click has to be allowed to try again."""
    calls = {"n": 0}

    class _BoomThenFine:
        async def run(self, arguments):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("app-server would not start")
            return BuiltInToolResult(structured_content={"logged_in": True})

    monkeypatch.setattr(coding_agents_api, "_status_leaf", lambda tool_id: _BoomThenFine())
    handler = _probe_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    async def _twice():
        a = await handler(_req(path_params={"tool_id": "codex"}))
        b = await handler(_req(path_params={"tool_id": "codex"}))
        return a, b

    first, second = asyncio.run(_twice())
    assert "app-server would not start" in _body(first)["error"]
    assert _body(second)["logged_in"] is True
    assert calls["n"] == 2


# -- Codex device-code sign-in -----------------------------------------------

class _FakeLoginSession:
    """The subset of ``CodexLoginSession`` the routes touch."""

    def __init__(
        self,
        *,
        profile: str = "admin",
        login_id: str = "login-1",
        status: str = "pending",
        detail: str | None = None,
        account: dict | None = None,
    ) -> None:
        self.profile = profile
        self.login_id = login_id
        self.status = status
        self.detail = detail
        self.account = account
        self.verification_url = "https://auth.openai.com/device"
        self.user_code = "ABCD-EFGH"
        self.home = "/system/admin/coding-cli/codex"

    def public(self) -> dict:
        return {
            "login_id": self.login_id,
            "status": self.status,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "detail": self.detail,
            "account": self.account,
            "home": self.home,
            "created_at": 1.0,
            "finished_at": None,
        }


def test_start_codex_login_returns_the_code(tmp_path: Path, monkeypatch, sdk_present) -> None:
    session = _FakeLoginSession()
    seen: Dict[str, Any] = {}

    async def _start(profile, variables):
        seen["profile"] = profile
        seen["variables"] = variables
        return session

    monkeypatch.setattr(codex_login, "start", _start)
    reg = _make_registry(tmp_path)
    reg.config.set_variable("codex", "admin", "CODEX_BIN", "/opt/codex", is_secret=False)
    resp = asyncio.run(_login_start_handler(SimpleNamespace(registry=reg))(_req()))
    assert resp.status_code == 201
    body = _body(resp)
    assert body["login_id"] == "login-1"
    assert body["verification_url"] == "https://auth.openai.com/device"
    assert body["user_code"] == "ABCD-EFGH"
    assert body["expires_in"] == int(codex_login._LOGIN_TIMEOUT)
    # The flow runs with this profile's own tool variables, not the server's.
    assert seen["profile"] == "admin"
    assert seen["variables"]["CODEX_BIN"] == "/opt/codex"


def test_start_codex_login_409_when_the_feature_is_absent(
    tmp_path: Path, monkeypatch, sdk_absent,
) -> None:
    called = {"n": 0}

    async def _start(profile, variables):  # pragma: no cover - must not run
        called["n"] += 1
        return _FakeLoginSession()

    monkeypatch.setattr(codex_login, "start", _start)
    resp = asyncio.run(
        _login_start_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(_req())
    )
    assert resp.status_code == 409
    assert called["n"] == 0


def test_start_codex_login_409_when_the_sdk_is_missing(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """``codex_login.start`` raises RuntimeError for a missing SDK; to the user
    that is the same problem as a missing feature, so it gets the same status."""
    async def _start(profile, variables):
        raise RuntimeError("openai_codex is not installed")

    monkeypatch.setattr(codex_login, "start", _start)
    resp = asyncio.run(
        _login_start_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(_req())
    )
    assert resp.status_code == 409
    assert "not installed" in _body(resp)["error"]


def test_start_codex_login_reports_a_refusal_as_a_reason(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """The flow started but never produced a code. The dialog renders ``error``
    verbatim, which beats a bare failure with nothing to read."""
    session = _FakeLoginSession(status="error", detail="Codex did not return a code.")

    async def _start(profile, variables):
        return session

    monkeypatch.setattr(codex_login, "start", _start)
    resp = asyncio.run(
        _login_start_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(_req())
    )
    assert resp.status_code == 200
    assert _body(resp)["error"] == "Codex did not return a code."


def test_get_codex_login_reports_status_and_drops_the_cached_probe_once(
    tmp_path: Path, monkeypatch,
) -> None:
    session = _FakeLoginSession(status="success", account={"email": "dev@example.com"})
    monkeypatch.setattr(codex_login, "get", lambda login_id: session)
    handler = _login_get_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    coding_agents_api._probe_cache[("admin", "codex")] = (time.monotonic(), {"logged_in": False})
    resp = asyncio.run(handler(_req(path_params={"login_id": "login-1"})))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["status"] == "success"
    assert body["account"] == {"email": "dev@example.com"}
    # A finished login changed the credential, so the stale verdict has to go.
    assert ("admin", "codex") not in coding_agents_api._probe_cache

    # ...but only the first time. The dialog keeps polling while the user reads
    # "Signed in as ...", and those polls must not throw away the fresh probe the
    # card runs straight after.
    coding_agents_api._probe_cache[("admin", "codex")] = (time.monotonic(), {"logged_in": True})
    asyncio.run(handler(_req(path_params={"login_id": "login-1"})))
    assert coding_agents_api._probe_cache[("admin", "codex")][1] == {"logged_in": True}


def test_get_codex_login_unknown_id_404(tmp_path: Path, monkeypatch) -> None:
    """Sessions live in this process only, so an unknown id means "start again"
    - which is what the SPA renders, and why it must not be a 500."""
    monkeypatch.setattr(codex_login, "get", lambda login_id: None)
    resp = asyncio.run(
        _login_get_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"login_id": "gone"})
        )
    )
    assert resp.status_code == 404


def test_get_codex_login_of_another_profile_403(tmp_path: Path, monkeypatch) -> None:
    """403, not 404: telling a user their own sign-in had expired when it had
    not would send them through the whole flow again."""
    monkeypatch.setattr(
        codex_login, "get", lambda login_id: _FakeLoginSession(profile="other"),
    )
    resp = asyncio.run(
        _login_get_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(username="admin", path_params={"login_id": "login-1"})
        )
    )
    assert resp.status_code == 403


def test_cancel_codex_login(tmp_path: Path, monkeypatch) -> None:
    session = _FakeLoginSession()
    cancelled: list[str] = []

    async def _cancel(login_id):
        cancelled.append(login_id)
        return True

    monkeypatch.setattr(codex_login, "get", lambda login_id: session)
    monkeypatch.setattr(codex_login, "cancel", _cancel)
    resp = asyncio.run(
        _login_cancel_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"login_id": "login-1"})
        )
    )
    assert resp.status_code == 200
    assert _body(resp)["ok"] is True
    assert cancelled == ["login-1"]


def test_cancel_codex_login_of_another_profile_403(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        codex_login, "get", lambda login_id: _FakeLoginSession(profile="other"),
    )
    resp = asyncio.run(
        _login_cancel_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"login_id": "login-1"})
        )
    )
    assert resp.status_code == 403


# -- terminal sign-in --------------------------------------------------------

class _TerminalRecorder:
    """Stands in for ``terminals.create_terminal`` and records the spawn."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(
        self, profile, *, cwd, cols, rows, extra_env, argv=None, title=None,
        drop_env=None,
    ):
        self.calls.append({
            "profile": profile, "cwd": cwd, "cols": cols, "rows": rows,
            "extra_env": extra_env, "argv": argv, "title": title,
            "drop_env": drop_env,
        })
        return SimpleNamespace(
            terminal_id="term-abc", title=title or "Terminal 1", shell="claude",
            working_dir=cwd, created_at=1234.0,
        )


def test_login_terminal_runs_the_login_binary_in_the_profiles_own_home(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """The binary is spawned directly (not through a shell), and the home is
    FORCED to the profile's own: with the resolved home a member profile
    signing in would overwrite the operator's shared login."""
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    recorder = _TerminalRecorder()
    monkeypatch.setattr(terminals_api, "create_terminal", recorder)
    coding_agents_api._probe_cache[("admin", "claude_code")] = (
        time.monotonic(), {"logged_in": False},
    )

    resp = asyncio.run(
        _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"}, body={"cols": 100, "rows": 24})
        )
    )
    assert resp.status_code == 201
    body = _body(resp)
    assert body["terminal_id"] == "term-abc"
    assert body["tool_id"] == "claude_code"
    assert body["scope"] == "profile"
    assert Path(body["cli_home"]) == _profile_claude_home(tmp_path)
    assert "auth login" in body["command"]

    call = recorder.calls[0]
    assert call["argv"] == ["/opt/claude", "auth", "login"]
    assert Path(call["extra_env"]["CLAUDE_CONFIG_DIR"]) == _profile_claude_home(tmp_path)
    assert call["title"] == "Sign in to Claude Code"
    # The CLI needs somewhere to write its credential before it starts.
    assert _profile_claude_home(tmp_path).is_dir()
    # A login is now being typed, so the last verdict is worthless.
    assert ("admin", "claude_code") not in coding_agents_api._probe_cache


def test_login_terminal_shared_scope_is_admin_only(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    recorder = _TerminalRecorder()
    monkeypatch.setattr(terminals_api, "create_terminal", recorder)
    handler = _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    resp = asyncio.run(handler(_req(
        username="member", path_params={"tool_id": "claude_code"}, body={"scope": "shared"},
    )))
    assert resp.status_code == 403
    assert recorder.calls == []


def test_login_terminal_shared_scope_writes_the_server_home_for_an_admin(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    recorder = _TerminalRecorder()
    monkeypatch.setattr(terminals_api, "create_terminal", recorder)
    handler = _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"}, body={"scope": "shared"},
    )))
    assert resp.status_code == 201
    assert _body(resp)["scope"] == "shared"
    env = recorder.calls[0]["extra_env"]
    assert Path(env["CLAUDE_CONFIG_DIR"]) == _shared_claude_home(tmp_path)
    # Both homes move together, so nothing in that session can reach back into
    # a per-profile home the admin did not mean to touch.
    assert Path(env["CODEX_HOME"]) == _shared_codex_home(tmp_path)


def test_login_terminal_409_without_a_cli_binary(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    recorder = _TerminalRecorder()
    monkeypatch.setattr(terminals_api, "create_terminal", recorder)
    resp = asyncio.run(
        _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "codex"})
        )
    )
    assert resp.status_code == 409
    assert "command-line tool" in _body(resp)["error"]
    assert recorder.calls == []


def test_login_terminal_409_on_a_host_that_cannot_run_the_cli(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """Refused before anything is spawned, and refused for the right reason.

    A PTY opened here would print nothing at all and burn a core until the user
    closed the dialog - the black box this replaces - so ``create_terminal`` must
    never be reached. The refusal also has to arrive with the remedy in it,
    because the 409 body is the only text the dialog shows; and it deliberately
    precedes the "no binary" refusal below, which is why this test leaves the
    binary absent and still expects the CPU answer rather than that one.
    """
    blocker = _block_host(monkeypatch, claude_code_runner)
    recorder = _TerminalRecorder()
    monkeypatch.setattr(terminals_api, "create_terminal", recorder)

    resp = asyncio.run(
        _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    assert resp.status_code == 409
    body = _body(resp)
    assert body["tool_id"] == "claude_code"
    assert body["error"] == f"{blocker['message']} {blocker['remedy']}"
    # The two things an operator acts on: the CPU model to recognise, and where
    # the setting that fixes it lives.
    assert "QEMU Virtual CPU version 2.5+" in body["error"]
    assert "Proxmox" in body["error"]
    assert body["cli_blocked"]["code"] == claude_code_runner.HOST_BLOCKER_CPU
    # Not the "install the CLI" refusal: that fix does not apply here.
    assert "command-line tool" not in body["error"]
    assert recorder.calls == []


def test_login_terminal_409_when_the_feature_is_absent(
    tmp_path: Path, monkeypatch, sdk_absent,
) -> None:
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    recorder = _TerminalRecorder()
    monkeypatch.setattr(terminals_api, "create_terminal", recorder)
    resp = asyncio.run(
        _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    assert resp.status_code == 409
    assert recorder.calls == []


def test_login_terminal_takes_the_display_away_inside_a_container(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """In a pod the display belongs to the VNC desktop, not to the person who
    clicked Sign in in a browser tab.

    The desktop image's entrypoint exports ``DISPLAY=:0`` to the server process
    and a PTY child inherits it, so ``claude auth login`` took its open-a-browser
    path and opened Chrome on a desktop nobody was watching - while the terminal
    the user was actually looking at printed nothing. The CLI only prints the
    paste-a-code URL when it cannot open a browser, and no *value* of ``DISPLAY``
    says "there is no display", so the variables have to leave the environment
    rather than be overridden."""
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    monkeypatch.setattr(runtime_env, "is_container", lambda: True, raising=False)
    recorder = _TerminalRecorder()
    monkeypatch.setattr(terminals_api, "create_terminal", recorder)

    resp = asyncio.run(
        _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    assert resp.status_code == 201
    dropped = recorder.calls[0]["drop_env"]
    assert "DISPLAY" in dropped
    # Wayland and an explicit browser command are the same mistake by another
    # name, so they go with it.
    assert "WAYLAND_DISPLAY" in dropped
    assert "BROWSER" in dropped


def test_login_terminal_leaves_a_native_install_its_browser(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """Nothing is dropped off a container: on a native install the user's own
    browser opening on their own desktop is precisely the sign-in they want,
    and taking DISPLAY away would replace it with a URL to copy by hand."""
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    monkeypatch.setattr(runtime_env, "is_container", lambda: False, raising=False)
    recorder = _TerminalRecorder()
    monkeypatch.setattr(terminals_api, "create_terminal", recorder)

    resp = asyncio.run(
        _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    assert resp.status_code == 201
    assert not recorder.calls[0]["drop_env"]


def test_login_terminal_reports_the_per_profile_cap(
    tmp_path: Path, monkeypatch, sdk_present,
) -> None:
    """A sign-in terminal counts against the same budget as the user's own, and
    the cap has to arrive as the message the dialog shows, not a 500."""
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")

    async def _full(*args, **kwargs):
        raise terminals_api.TerminalLimitReached("Too many open terminals (max 10).")

    monkeypatch.setattr(terminals_api, "create_terminal", _full)
    resp = asyncio.run(
        _login_terminal_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    assert resp.status_code == 409
    assert "Too many open terminals" in _body(resp)["error"]


# -- sign-out ----------------------------------------------------------------

def _fake_logout(recorder: list):
    async def _logout(variables, profile, *, scope="profile"):
        recorder.append({"profile": profile, "scope": scope})
        return {"ok": True, "scope": scope, "home": "/somewhere", "detail": "Signed out."}
    return _logout


def test_logout_refuses_to_sign_out_a_login_this_profile_only_borrows(
    tmp_path: Path, monkeypatch,
) -> None:
    """The load-bearing refusal: a profile with no login of its own resolves to
    the shared server login, so an unguarded "Sign out" would have signed every
    other profile out with it."""
    _sign_in_claude(_shared_claude_home(tmp_path))
    calls: list = []
    monkeypatch.setattr(claude_code_runner, "logout", _fake_logout(calls))
    resp = asyncio.run(
        _logout_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    assert resp.status_code == 409
    error = _body(resp)["error"]
    assert "shared server login" in error
    assert "scope=shared" in error
    assert calls == []


def test_logout_refuses_when_nothing_is_signed_in_at_all(
    tmp_path: Path, monkeypatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(codex_runner, "logout", _fake_logout(calls))
    resp = asyncio.run(
        _logout_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "codex"})
        )
    )
    assert resp.status_code == 409
    assert "nothing to sign out" in _body(resp)["error"]
    assert calls == []


def test_logout_shared_scope_is_admin_only(tmp_path: Path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(claude_code_runner, "logout", _fake_logout(calls))
    resp = asyncio.run(
        _logout_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(
                username="member",
                path_params={"tool_id": "claude_code"},
                body={"scope": "shared"},
            )
        )
    )
    assert resp.status_code == 403
    assert calls == []


def test_logout_signs_this_profiles_own_login_out(tmp_path: Path, monkeypatch) -> None:
    _sign_in_claude(_profile_claude_home(tmp_path))
    calls: list = []
    monkeypatch.setattr(claude_code_runner, "logout", _fake_logout(calls))
    coding_agents_api._probe_cache[("admin", "claude_code")] = (
        time.monotonic(), {"logged_in": True},
    )
    resp = asyncio.run(
        _logout_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    assert resp.status_code == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["scope"] == "profile"
    assert calls == [{"profile": "admin", "scope": "profile"}]
    # The cached "signed in" verdict cannot survive a sign-out.
    assert ("admin", "claude_code") not in coding_agents_api._probe_cache


def test_admin_can_sign_the_shared_login_out(tmp_path: Path, monkeypatch) -> None:
    _sign_in_codex(_shared_codex_home(tmp_path))
    calls: list = []
    monkeypatch.setattr(codex_runner, "logout", _fake_logout(calls))
    resp = asyncio.run(
        _logout_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "codex"}, body={"scope": "shared"})
        )
    )
    assert resp.status_code == 200
    assert _body(resp)["scope"] == "shared"
    assert calls == [{"profile": "admin", "scope": "shared"}]


def test_logout_unknown_tool_id_400(tmp_path: Path) -> None:
    resp = asyncio.run(
        _logout_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "exec_shell"})
        )
    )
    assert resp.status_code == 400


# -- where the CLI is --------------------------------------------------------

def test_cli_payload_describes_the_binary_and_both_homes(
    tmp_path: Path, monkeypatch,
) -> None:
    """What lets ``cremind tools coding-agents login`` refuse politely instead
    of exec'ing a binary that lives on another machine."""
    import sys

    _with_cli(monkeypatch, claude_code_runner, "/opt/claude", source="bundled")
    resp = asyncio.run(
        _cli_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    assert resp.status_code == 200
    body = _body(resp)
    assert body["binary"] == "/opt/claude"
    assert body["binary_source"] == "bundled"
    assert body["login_argv"] == ["/opt/claude", "auth", "login"]
    assert body["logout_argv"] == ["/opt/claude", "auth", "logout"]
    assert body["status_argv"] == ["/opt/claude", "auth", "status", "--json"]
    assert Path(body["profile_env"]["CLAUDE_CONFIG_DIR"]) == _profile_claude_home(tmp_path)
    assert Path(body["shared_env"]["CLAUDE_CONFIG_DIR"]) == _shared_claude_home(tmp_path)
    assert Path(body["system_dir"]) == tmp_path
    assert body["server_hostname"]
    assert body["platform"] == sys.platform
    # The healthy shape, pinned: a payload that never says "not blocked" would
    # let the CLI's refusal creep in on hosts where the binary runs fine.
    assert body["cli_blocked"] is None


def test_cli_payload_tells_a_shell_sign_in_what_not_to_inherit(
    tmp_path: Path, monkeypatch,
) -> None:
    """The shell door needs the same display fix as the browser one.

    ``cremind tools coding-agents login`` runs the login in the user's own
    terminal, building its environment from ``os.environ`` - which inside our
    image carries the desktop's ``DISPLAY``. Fixing only the built-in terminal
    would leave the shell sign-in silently opening Chrome on the VNC desktop on
    exactly the hosts this feature exists for, so the server names the variables
    to drop and both doors obey it.
    """
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    handler = _cli_handler(SimpleNamespace(registry=_make_registry(tmp_path)))

    monkeypatch.setattr(runtime_env, "is_container", lambda: True, raising=False)
    resp = asyncio.run(handler(_req(path_params={"tool_id": "claude_code"})))
    assert set(_body(resp)["drop_env"]) == {"DISPLAY", "WAYLAND_DISPLAY", "BROWSER"}

    # On a native install the browser opening is the whole point, so nothing is
    # taken away and the CLI has nothing to do.
    monkeypatch.setattr(runtime_env, "is_container", lambda: False, raising=False)
    resp = asyncio.run(handler(_req(path_params={"tool_id": "claude_code"})))
    assert _body(resp)["drop_env"] == []


def test_cli_payload_carries_the_host_blocker(tmp_path: Path, monkeypatch) -> None:
    """``cremind tools coding-agents login`` cannot ask the CPU itself - modules
    under ``app/cli`` must not import ``app.tools`` - and it is the SERVER's CPU
    that decides, not the one the command is typed on. So the descriptor carries
    the whole diagnosis and the CLI refuses on it before exec'ing anything."""
    _with_cli(monkeypatch, claude_code_runner, "/opt/claude")
    blocker = _block_host(monkeypatch, claude_code_runner)
    resp = asyncio.run(
        _cli_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "claude_code"})
        )
    )
    body = _body(resp)
    assert body["cli_blocked"] == blocker
    # The binary is still reported: it is there, it is just unrunnable, and a
    # payload that hid it would send the user chasing a missing file instead.
    assert body["binary"] == "/opt/claude"


def test_cli_payload_without_a_binary_offers_no_command(
    tmp_path: Path,
) -> None:
    """A command with a null in it is worse than none: a caller that renders
    argv would print "None login" and a caller that runs it would crash."""
    resp = asyncio.run(
        _cli_handler(SimpleNamespace(registry=_make_registry(tmp_path)))(
            _req(path_params={"tool_id": "codex"})
        )
    )
    body = _body(resp)
    assert body["binary"] is None
    assert body["binary_source"] is None
    assert body["login_argv"] == []
    assert body["logout_argv"] == []
    assert body["status_argv"] == []
    assert Path(body["profile_env"]["CODEX_HOME"]) == _profile_codex_home(tmp_path)
    assert Path(body["shared_env"]["CODEX_HOME"]) == _shared_codex_home(tmp_path)
