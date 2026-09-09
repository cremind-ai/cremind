"""Claude Code's credentials belong to the ``claude`` CLI, not to the profile.

Three things are pinned here, and each one had a way of going wrong:

* **Which credential wins, and whose it is.** The tiers are key-first (tool
  variable, then the server environment), then the CLI's own login - this
  profile's, and only then the server's shared one. Two profiles are used
  throughout, because every bug this design exists to prevent (one profile
  reading, or overwriting, another's login) is invisible with one.
* **``resolve_auth_env`` always states the home.** A run that inherits an
  unstated ``CLAUDE_CONFIG_DIR`` authenticates - and resumes sessions - out of
  whichever home the server process happened to have.
* **Nothing here costs money.** ``probe_auth`` used to run a real one-turn SDK
  query, so every "Check sign-in" click was a billed API call; it now asks
  ``claude auth status --json`` (local, unbilled) for a *login*, and validates a
  *key* with an unbilled ``GET /v1/models`` listing - never an inference call.
* **Which check is authoritative is the credential's tier, not the order the
  checks run in.** ``claude auth status`` reports a key it has never validated
  as logged in, so a probe that stopped at the first "yes" called every stale,
  revoked or mistyped key "Signed in".
* **The CLI names the tier, Cremind's ranking does not.** The tier has to be the
  one the ``claude`` binary would really pick (its own ``authMethod``), not the
  one Cremind ranks first: Cremind puts a key in the server environment above a
  CLI login, so on a host that exports ``ANTHROPIC_API_KEY`` for something else,
  the probe validated that key while every run authenticated with the profile's
  own login - and reported a working install as signed out.
* **"Held" is not "verified".** A key is validated against the API (on the same
  ``ANTHROPIC_BASE_URL`` the CLI would call); a login is only ever *present*,
  because the local status check cannot tell an expired or revoked one from a
  live one. The payload has to say which of the two happened.

Isolation is by environment only (``CREMIND_SYSTEM_DIR``, ``CLAUDE_CONFIG_DIR``,
``HOME``), so a test creates a login by writing the file the CLI would have
written. The subprocess is faked at ``asyncio.create_subprocess_exec``: the real
binary is interactive, may not be installed, and must never be run by a test.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import httpx
import pytest

import app.tools.builtin.claude_code_runner as runner
from app.config import runtime_env
from app.config.settings import BaseConfig
from app.tools.builtin.claude_code_runner import Var

# Exactly what the bundled CLI prints today (claude_agent_sdk 0.2.116):
# `claude auth status --json` exits 0 with this object and makes no network call.
REAL_STATUS_JSON = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "email": "dev@example.com",
    "orgId": "1f0a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "orgName": "Acme Inc",
    "subscriptionType": "max",
}

CLI = "/opt/claude/claude"
CLI_VARS = {Var.CLI_PATH: CLI}


@pytest.fixture(autouse=True)
def homes(monkeypatch, tmp_path):
    """A System Directory, a shared CLI home and a ``~`` that are this test's."""
    system_dir = tmp_path / "system"
    shared = tmp_path / "shared"
    home = tmp_path / "home"
    system_dir.mkdir()
    home.mkdir()
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(system_dir))
    # posixpath.expanduser reads HOME, ntpath.expanduser reads USERPROFILE.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    # A developer with a gateway install exports this; it moves the endpoint the
    # probe validates against, so it must be this test's to set.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    # The container-only legacy ``~`` tier must never fire from a dev box / CI.
    monkeypatch.setattr(runtime_env, "is_container", lambda *a, **k: False)
    runner._models_cache.clear()
    yield types.SimpleNamespace(system=system_dir, shared=shared, home=home)
    runner._models_cache.clear()


def _sign_in(config_dir: Path, token: str = "tok") -> Path:
    """Write the ``.credentials.json`` a completed ``claude auth login`` leaves."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}), encoding="utf-8")
    return path


def _record_account(config_dir: Path, email: str, org: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email, "organizationName": org}}),
        encoding="utf-8",
    )


class _FakeProcess:
    """A ``claude`` child process that never was."""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
                 hang: bool = False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.Event().wait()  # cancelled by wait_for
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self._hang = False

    async def wait(self):
        return self.returncode


def _patch_exec(monkeypatch, *, proc=None, error=None, calls=None):
    """Replace the process spawn with a fake, optionally recording each call."""
    async def _spawn(*argv, **kwargs):
        if calls is not None:
            calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
        if error is not None:
            raise error
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)


def _json_proc(payload, *, prefix: str = "", returncode: int = 0) -> _FakeProcess:
    return _FakeProcess(
        stdout=(prefix + json.dumps(payload)).encode("utf-8"), returncode=returncode,
    )


# -- resolve_auth_env ---------------------------------------------------------


def test_resolve_auth_env_always_states_the_config_dir(homes):
    """Every run must be told which home it authenticates from.

    Left unset, the child inherits the server process's ``CLAUDE_CONFIG_DIR``:
    it would authenticate as whoever the server is signed in as and write this
    profile's session transcripts into a directory other profiles read.
    """
    env = runner.resolve_auth_env({}, "alice")
    assert env == {"CLAUDE_CONFIG_DIR": str(homes.shared)}

    _sign_in(homes.system / "alice" / "coding-cli" / "claude")
    env = runner.resolve_auth_env({}, "alice")
    assert env == {"CLAUDE_CONFIG_DIR": str(homes.system / "alice" / "coding-cli" / "claude")}


def test_resolve_auth_env_layers_the_tool_api_key(homes):
    env = runner.resolve_auth_env({Var.API_KEY: " sk-tool "}, "alice")
    assert env["ANTHROPIC_API_KEY"] == "sk-tool"
    # ... and still names the home: the key authenticates, the home holds the
    # sessions, and dropping one for the other is what the old code did.
    assert env["CLAUDE_CONFIG_DIR"] == str(homes.shared)


def test_resolve_auth_env_never_reads_the_profiles_llm_credentials(homes, monkeypatch):
    """No profile Anthropic provider credential may leak into a coding run."""
    from app.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod.BaseConfig, "get_provider_api_key",
        lambda *a, **k: pytest.fail("the profile's LLM credentials must not be read"),
    )
    assert runner.resolve_auth_env({}, "alice") == {"CLAUDE_CONFIG_DIR": str(homes.shared)}
    assert runner.credential_source({}, "alice") is None


# -- credential tiers, with two profiles --------------------------------------


def test_each_profile_resolves_its_own_login(homes):
    alice_home = homes.system / "alice" / "coding-cli" / "claude"
    _sign_in(alice_home, "alice-tok")
    _record_account(alice_home, "alice@example.com", "Alice LLC")

    alice = runner.credential_info({}, "alice")
    assert alice == {
        "source": "profile_claude_login",
        "scope": "profile",
        "cli_home": str(alice_home),
        "account_hint": {"type": "oauth", "email": "alice@example.com", "org_name": "Alice LLC"},
    }

    # bob never signed in and there is no shared login either.
    bob = runner.credential_info({}, "bob")
    assert bob["source"] is None
    assert bob["scope"] is None
    assert bob["account_hint"] is None
    assert bob["cli_home"] == str(homes.shared)


def test_a_profile_without_a_login_borrows_the_servers(homes):
    _sign_in(homes.system / "alice" / "coding-cli" / "claude", "alice-tok")
    _sign_in(homes.shared, "host-tok")
    _record_account(homes.shared, "ops@example.com", "Acme Inc")

    bob = runner.credential_info({}, "bob")
    assert bob["source"] == "host_claude_login"
    assert bob["scope"] == "shared"
    assert bob["cli_home"] == str(homes.shared)
    assert bob["account_hint"]["email"] == "ops@example.com"

    # ... and alice keeps her own: a shared login must never shadow one.
    assert runner.credential_info({}, "alice")["source"] == "profile_claude_login"


def test_key_tiers_beat_a_cli_login(homes, monkeypatch):
    """An operator who states a key is stating which account gets billed."""
    _sign_in(homes.system / "alice" / "coding-cli" / "claude", "alice-tok")

    info = runner.credential_info({Var.API_KEY: "sk-tool"}, "alice")
    assert info["source"] == "tool_variable_api_key"
    # A key has no login to sign out of, and no account to attribute.
    assert info["scope"] is None
    assert info["account_hint"] is None
    # The home is reported all the same - it is where the sessions live.
    assert info["cli_home"] == str(homes.system / "alice" / "coding-cli" / "claude")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert runner.credential_source({}, "alice") == "env_anthropic_api_key"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-env")
    assert runner.credential_source({}, "alice") == "env_oauth_token"


# -- auth_status --------------------------------------------------------------


def test_auth_status_parses_the_cli_json(monkeypatch, homes):
    calls = []
    _patch_exec(monkeypatch, proc=_json_proc(REAL_STATUS_JSON), calls=calls)

    out = asyncio.run(runner.auth_status(CLI_VARS, "alice"))
    assert out["logged_in"] is True
    assert out["auth_method"] == "claude.ai"
    assert out["api_provider"] == "firstParty"
    assert out["email"] == "dev@example.com"
    assert out["org_name"] == "Acme Inc"
    assert out["subscription_type"] == "max"
    assert "dev@example.com" in out["detail"]

    # It asks the CLI about the home this profile would actually run in.
    assert calls[0]["argv"] == [CLI, "auth", "status", "--json"]
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] == str(homes.shared)


def test_auth_status_can_be_pointed_at_one_home(monkeypatch, homes):
    calls = []
    _patch_exec(monkeypatch, proc=_json_proc(REAL_STATUS_JSON), calls=calls)
    target = homes.system / "alice" / "coding-cli" / "claude"

    asyncio.run(runner.auth_status(CLI_VARS, "alice", config_dir=target))
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] == str(target)


def test_auth_status_reports_a_signed_out_cli(monkeypatch):
    # Non-zero exit with a usable answer: the answer wins over the exit code.
    _patch_exec(monkeypatch, proc=_json_proc({"loggedIn": False}, returncode=1))
    out = asyncio.run(runner.auth_status(CLI_VARS, "alice"))
    assert out["logged_in"] is False
    assert out["email"] is None
    assert "no login" in out["detail"]


def test_auth_status_skips_a_banner_before_the_json(monkeypatch):
    """The CLI may print an update notice first; that is not a parse failure."""
    _patch_exec(monkeypatch, proc=_json_proc(
        REAL_STATUS_JSON,
        prefix="A new version of Claude Code is available! {run npm i -g}\n",
    ))
    out = asyncio.run(runner.auth_status(CLI_VARS, "alice"))
    assert out["logged_in"] is True
    assert out["email"] == "dev@example.com"


def test_auth_status_garbage_is_unknown_not_signed_out(monkeypatch):
    _patch_exec(monkeypatch, proc=_FakeProcess(stdout=b"command not recognised\n", returncode=2))
    out = asyncio.run(runner.auth_status(CLI_VARS, "alice"))
    assert out["logged_in"] is None
    assert "no JSON object" in out["detail"]


def test_auth_status_timeout_is_unknown_and_kills_the_child(monkeypatch):
    proc = _FakeProcess(hang=True)
    _patch_exec(monkeypatch, proc=proc)
    out = asyncio.run(runner.auth_status(CLI_VARS, "alice", timeout=0.05))
    assert out["logged_in"] is None
    assert "did not answer" in out["detail"]
    assert proc.killed is True


def test_auth_status_missing_binary_is_unknown(monkeypatch):
    _patch_exec(monkeypatch, error=FileNotFoundError("no such file"))
    out = asyncio.run(runner.auth_status(CLI_VARS, "alice"))
    assert out["logged_in"] is None
    assert "could not be started" in out["detail"]


def test_auth_status_without_any_cli_is_unknown(monkeypatch):
    """No binary at all: still "cannot tell", never "signed out"."""
    monkeypatch.setattr(runner, "find_cli", lambda variables=None: None)
    out = asyncio.run(runner.auth_status({}, "alice"))
    assert out["logged_in"] is None
    assert "not found" in out["detail"]


# -- probe_auth ---------------------------------------------------------------
#
# The premise, observed by running the real bundled binary (claude_agent_sdk
# 0.2.116) against an empty, scratch ``CLAUDE_CONFIG_DIR``:
#
#   ANTHROPIC_API_KEY=sk-ant-totally-bogus-key-0000 -> exit 0, and
#     {"loggedIn": true, "authMethod": "api_key", "apiProvider": "firstParty",
#      "apiKeySource": "ANTHROPIC_API_KEY"}
#   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-bogus      -> exit 0, and
#     {"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}
#   neither set                                     -> exit 1, and
#     {"loggedIn": false, "authMethod": "none", "apiProvider": "firstParty"}
#
# So ``loggedIn`` is a PRESENCE check on the environment, not a validation - and
# ``resolve_auth_env`` puts the CLAUDE_CODE_API_KEY tool variable into that same
# environment. A probe that stops at the first check answering "yes" therefore
# green-lights every stale, revoked or mistyped key. Which check is
# authoritative has to follow the credential's TIER.

BOGUS_KEY_STATUS_JSON = {
    "loggedIn": True,
    "authMethod": "api_key",
    "apiProvider": "firstParty",
    "apiKeySource": "ANTHROPIC_API_KEY",
}


def _patch_status(monkeypatch, **fields):
    result = {
        "logged_in": None, "auth_method": None, "api_provider": None, "email": None,
        "org_name": None, "subscription_type": None, "detail": "",
    }
    result.update(fields)

    async def _status(variables, profile, *, config_dir=None, timeout=15.0):
        return dict(result)

    monkeypatch.setattr(runner, "auth_status", _status)


def _patch_fetch(monkeypatch, *, rows=None, error=None, calls=None):
    """Replace the one network call a probe may make: ``GET /v1/models``.

    Stubbed at ``_fetch_models`` and never higher up. ``list_models`` picks the
    credential by Cremind's ranking, so a stub sitting there would answer for
    whichever credential the *test* imagined - hiding the bug where the probe
    validates one credential while the CLI runs on another. Here the headers the
    fetch is handed ARE the credential under test.
    """
    async def _fetch(headers):
        if calls is not None:
            calls.append(dict(headers))
        if error is not None:
            raise error
        return list(rows or [])

    monkeypatch.setattr(runner, "_fetch_models", _fetch)


def _rejection(code: int = 401) -> httpx.HTTPStatusError:
    """What ``GET /v1/models`` raises when the API REFUSES the credential."""
    request = httpx.Request("GET", runner._MODELS_URL)
    return httpx.HTTPStatusError(
        "nope", request=request, response=httpx.Response(code, request=request),
    )


def _forbid_network(monkeypatch, why: str):
    """Fail the test if the probe reaches for the API at all."""
    async def _never(*a, **k):
        pytest.fail(why)

    monkeypatch.setattr(runner, "_fetch_models", _never)
    monkeypatch.setattr(runner, "list_models", _never)


def test_probe_rejects_a_key_the_cli_only_saw_in_the_environment(monkeypatch, homes):
    """The regression: a bogus key that ``claude auth status`` calls signed in.

    Driven through the real :func:`auth_status` with the exact JSON the bundled
    binary prints for an unvalidated key (see the note above), so the test fails
    the way a user does - green chip, LOGGED_IN yes, every run failing.
    """
    _patch_exec(monkeypatch, proc=_json_proc(BOGUS_KEY_STATUS_JSON))
    calls = []
    _patch_fetch(monkeypatch, error=_rejection(401), calls=calls)
    # A cached list for this very key must not be able to answer for it: a probe
    # that reads its verdict out of the cache stops being a check at all.
    runner._models_cache[runner._cache_key(
        {"x-api-key": "sk-ant-bogus", "anthropic-version": runner._ANTHROPIC_VERSION}
    )] = (99999999.0, [{"id": "stale"}])

    out = asyncio.run(runner.probe_auth(
        None, cwd="", variables={**CLI_VARS, Var.API_KEY: "sk-ant-bogus"}, profile="alice",
    ))
    assert out["logged_in"] is False
    assert out["credential_verified"] is False
    assert out["credential_source"] == "tool_variable_api_key"
    assert "401" in out["detail"]
    assert "tool_variable_api_key" in out["detail"]
    # The unbilled listing is the check that settled it, and it really ran.
    assert calls == [{"x-api-key": "sk-ant-bogus", "anthropic-version": runner._ANTHROPIC_VERSION}]


def test_probe_validates_an_environment_key_the_same_way(monkeypatch, homes):
    """Both server-environment key tiers get the same free pass from the CLI, so
    both must be settled by the models listing too."""
    _patch_status(monkeypatch, logged_in=True, auth_method="api_key")
    _patch_fetch(monkeypatch, error=_rejection(401))

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-bad")
    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is False
    assert out["credential_source"] == "env_anthropic_api_key"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-bogus")
    _patch_status(monkeypatch, logged_in=True, auth_method="oauth_token")
    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is False
    assert out["credential_source"] == "env_oauth_token"


def test_probe_validates_a_key_with_an_unbilled_model_listing(monkeypatch, homes):
    """A working key: the listing is a listing, not an inference call, so the
    probe still spends nothing while proving the key is accepted."""
    _patch_status(monkeypatch, logged_in=True, auth_method="api_key")
    calls = []
    _patch_fetch(monkeypatch, rows=[{"id": "claude-sonnet-4-5"}], calls=calls)

    out = asyncio.run(runner.probe_auth(
        None, cwd="", variables={Var.API_KEY: "sk-tool"}, profile="alice",
    ))
    assert out["logged_in"] is True
    assert out["credential_verified"] is True
    assert len(calls) == 1
    assert "authenticates" in out["detail"]
    # The CLI's own account fields survive the key path.
    assert out["account"]["auth_method"] == "api_key"


def test_probe_keeps_the_cli_account_details_on_the_key_path(monkeypatch, homes):
    """The listing decides the verdict; it must not erase who the CLI said we are."""
    _patch_status(
        monkeypatch, logged_in=True, auth_method="api_key", api_provider="firstParty",
        email="ops@example.com", org_name="Acme Inc", subscription_type="max",
    )
    _patch_fetch(monkeypatch, rows=[{"id": "m1"}])

    out = asyncio.run(runner.probe_auth(
        None, cwd="", variables={Var.API_KEY: "sk-tool"}, profile="alice",
    ))
    assert out["email"] == "ops@example.com"
    assert out["api_provider"] == "firstParty"
    assert out["account"] == {
        "auth_method": "api_key", "email": "ops@example.com",
        "org_name": "Acme Inc", "subscription_type": "max",
    }


def test_probe_cannot_tell_when_the_models_call_never_reached_the_api(monkeypatch, homes):
    """Offline is not signed out.

    Only the API refusing the credential (401/403) proves a key is bad; a
    transport failure leaves it unjudged, and calling that "Not signed in" sends
    a user with a perfectly good key off to sign in again.
    """
    _patch_status(monkeypatch, logged_in=True, auth_method="api_key")
    _patch_fetch(monkeypatch, error=RuntimeError("[Errno -2] Name or service not known"))

    out = asyncio.run(runner.probe_auth(
        None, cwd="", variables={Var.API_KEY: "sk-tool"}, profile="alice",
    ))
    assert out["logged_in"] is None
    assert out["credential_verified"] is None
    assert "not the same as being signed out" in out["detail"]


def test_probe_trusts_a_cli_login_without_a_network_call(monkeypatch, homes):
    """A login lives in the CLI's own store, which only the CLI can read - so
    ``claude auth status`` is the authoritative answer and nothing is called."""
    _sign_in(homes.system / "alice" / "coding-cli" / "claude", "alice-tok")
    assert runner.credential_source({}, "alice") == "profile_claude_login"
    _patch_status(
        monkeypatch, logged_in=True, auth_method="claude.ai", email="dev@example.com",
        org_name="Acme Inc", subscription_type="max",
    )
    _forbid_network(monkeypatch, "a CLI login is settled locally - no network call")

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is True
    assert out["account"] == {
        "auth_method": "claude.ai", "email": "dev@example.com",
        "org_name": "Acme Inc", "subscription_type": "max",
    }


def test_probe_follows_the_cli_not_cremind_s_ranking(monkeypatch, homes):
    """THE regression: an unrelated ``ANTHROPIC_API_KEY`` on the server host.

    Cremind ranks a key in the server environment above the CLI's own login, so
    the probe used to validate that key - while every actual run authenticates
    with the profile's own ``claude auth login``. On a host that exports the
    variable for something else entirely (another tool, a gateway, a leftover),
    a perfectly good login was reported as signed out; the reverse - a live key
    blessing a dead login - is the same bug wearing the other hat.

    The CLI is asked with the very environment a run gets, and its ``authMethod``
    names what it would really use. That answer wins.
    """
    _sign_in(homes.system / "alice" / "coding-cli" / "claude", "alice-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-for-some-other-service")
    # Cremind's own ranking still says the environment key wins ...
    assert runner.credential_source({}, "alice") == "env_anthropic_api_key"
    # ... but the CLI, asked with that same environment, says it runs on the login.
    _patch_status(
        monkeypatch, logged_in=True, auth_method="claude.ai", email="alice@example.com",
    )
    _forbid_network(
        monkeypatch,
        "the CLI runs on a login: validating an unrelated environment key proves "
        "nothing about it",
    )

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is True
    # The credential the probe SPEAKS FOR is the one the CLI named, so the
    # reported source and the check that ran can never be about different things.
    assert out["credential_source"] == "profile_claude_login"


def test_probe_validates_the_credential_the_cli_names(monkeypatch, homes):
    """With two keys in the environment, the CLI says which one it uses.

    Cremind ranks ``ANTHROPIC_API_KEY`` above ``CLAUDE_CODE_OAUTH_TOKEN``; when
    the CLI reports ``oauth_token`` it is the token that must be sent, or the
    probe's verdict belongs to a credential no run will ever use.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-env")
    _patch_status(monkeypatch, logged_in=True, auth_method="oauth_token")
    calls = []
    _patch_fetch(monkeypatch, rows=[{"id": "m1"}], calls=calls)

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["credential_source"] == "env_oauth_token"
    assert out["logged_in"] is True
    assert calls[0]["Authorization"] == "Bearer oat-env"
    assert "x-api-key" not in calls[0]


def test_probe_validates_against_the_base_url_the_cli_would_call(monkeypatch, homes):
    """A gateway install: ``ANTHROPIC_BASE_URL`` moves the endpoint.

    The ``claude`` CLI honours it, so the key Cremind holds is scoped to that
    gateway. Sending it to api.anthropic.com gets it refused and reports a
    working install as signed out - the probe validating the wrong ENDPOINT is
    the same failure as validating the wrong credential.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.internal/anthropic/")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-gateway")
    _patch_status(monkeypatch, logged_in=True, auth_method="api_key")

    captured = {}

    def _handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    transport = httpx.MockTransport(_handle)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(runner.httpx, "AsyncClient", _factory)

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is True
    assert captured["url"].startswith("https://gateway.internal/anthropic/v1/models")


def test_probe_says_a_login_is_held_not_verified(monkeypatch, homes):
    """"The CLI holds a credential" is not "the credential works".

    ``claude auth status`` reads the local login store; an OAuth login that has
    since expired or been revoked still reports as present, and no local check
    can tell the difference. The payload therefore has to carry both facts, so
    the card and the CLI table do not turn "present" into "verified".
    """
    _sign_in(homes.system / "alice" / "coding-cli" / "claude", "alice-tok")
    _patch_status(
        monkeypatch, logged_in=True, auth_method="claude.ai", email="dev@example.com",
        detail="Signed in as dev@example.com (claude.ai, max) in /home/alice.",
    )
    _forbid_network(monkeypatch, "a login is never validated over the network")

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is True          # the CLI holds a login ...
    assert out["credential_verified"] is None  # ... and nothing proved it works
    assert "not verif" in out["detail"].lower()
    # The CLI's own words are kept: the caveat is added to them, not instead.
    assert "dev@example.com" in out["detail"]


def test_probe_does_not_claim_a_key_it_cannot_read(monkeypatch, homes):
    """The CLI can hold a key Cremind cannot see (an ``apiKeyHelper``).

    There is nothing to send, so there is nothing to validate: keep the CLI's
    answer, say plainly that it was not verified, and make no network call that
    would only judge some other credential.
    """
    _patch_status(monkeypatch, logged_in=True, auth_method="api_key_helper")
    _forbid_network(monkeypatch, "there is no readable credential to validate")

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is True
    assert out["credential_verified"] is None
    assert "not verif" in out["detail"].lower()


def test_probe_summarises_the_account_when_signed_in(monkeypatch):
    _patch_status(
        monkeypatch, logged_in=True, auth_method="claude.ai", email="dev@example.com",
        org_name="Acme Inc", subscription_type="max",
    )
    _forbid_network(monkeypatch, "a signed-in CLI needs no model call")

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is True
    assert out["account"] == {
        "auth_method": "claude.ai", "email": "dev@example.com",
        "org_name": "Acme Inc", "subscription_type": "max",
    }


def test_probe_reports_a_rejected_key(monkeypatch, homes):
    """The CLI could not be asked (no ``authMethod``), and a key is the only
    credential in sight - no login anywhere - so there is no ambiguity about
    which credential a run would use, and validating it is the honest check."""
    _patch_status(monkeypatch, logged_in=False)
    _patch_fetch(monkeypatch, error=_rejection(403))

    out = asyncio.run(runner.probe_auth(
        None, cwd="", variables={Var.API_KEY: "sk-bad"}, profile="alice",
    ))
    assert out["logged_in"] is False
    assert "403" in out["detail"]
    assert "account" not in out


def test_probe_stays_unknown_when_the_cli_could_not_be_asked(monkeypatch, homes):
    """No ``authMethod`` AND both a login and a key in sight.

    Nothing here knows which of the two a run would pick, and guessing is what
    the old code did. "Cannot tell" is the honest answer, and it must not be
    dressed up as a verdict about either credential.
    """
    _sign_in(homes.system / "alice" / "coding-cli" / "claude", "alice-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    _patch_status(
        monkeypatch, detail="`claude auth status` did not answer within 15s.",
    )
    _forbid_network(monkeypatch, "with the CLI silent there is no named credential")

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is None
    assert out["credential_verified"] is None
    assert "did not answer" in out["detail"]


def test_list_models_marks_only_a_refusal_as_rejected(monkeypatch):
    """``rejected`` is the signal the probe's tri-state hangs on, so pin it here:
    only the API refusing the credential may set it."""
    def _raise_status(code):
        async def _fetch(headers):
            request = httpx.Request("GET", runner._MODELS_URL)
            raise httpx.HTTPStatusError(
                "nope", request=request, response=httpx.Response(code, request=request),
            )
        return _fetch

    for code in (401, 403):
        monkeypatch.setattr(runner, "_fetch_models", _raise_status(code))
        assert asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "alice"))["rejected"] is True

    monkeypatch.setattr(runner, "_fetch_models", _raise_status(500))
    assert asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "alice"))["rejected"] is False

    async def _boom(headers):
        raise RuntimeError("network down")

    monkeypatch.setattr(runner, "_fetch_models", _boom)
    assert asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "alice"))["rejected"] is False


def test_probe_does_not_call_models_without_a_key(monkeypatch, homes):
    """With no credential at all there is nothing to validate - and calling the
    API anyway would just report a second, misleading failure."""
    _patch_status(monkeypatch, logged_in=False, detail="The Claude CLI reports no login.")
    _forbid_network(monkeypatch, "no key -> no model call")

    out = asyncio.run(runner.probe_auth(None, cwd="", variables={}, profile="alice"))
    assert out["logged_in"] is False
    assert "no login" in out["detail"]


# -- logout -------------------------------------------------------------------


def test_logout_signs_the_profile_out(monkeypatch, homes):
    alice_home = homes.system / "alice" / "coding-cli" / "claude"
    credentials = _sign_in(alice_home, "alice-tok")
    runner._models_cache["fingerprint"] = (0.0, [{"id": "m1"}])
    calls = []
    _patch_exec(monkeypatch, proc=_FakeProcess(), calls=calls)

    out = asyncio.run(runner.logout(CLI_VARS, "alice", scope="profile"))
    assert out["ok"] is True
    assert out["scope"] == "profile"
    assert out["home"] == str(alice_home)
    assert "this profile" in out["detail"]
    assert calls[0]["argv"] == [CLI, "auth", "logout"]
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] == str(alice_home)
    assert not credentials.exists()
    # The model cache is keyed by credential: a stale entry would keep answering
    # with the signed-out account's models.
    assert not runner._models_cache


def test_logout_shared_scope_targets_the_server_home(monkeypatch, homes):
    credentials = _sign_in(homes.shared, "host-tok")
    _sign_in(homes.system / "alice" / "coding-cli" / "claude", "alice-tok")
    calls = []
    _patch_exec(monkeypatch, proc=_FakeProcess(), calls=calls)

    out = asyncio.run(runner.logout(CLI_VARS, "alice", scope="shared"))
    assert out["ok"] is True
    assert out["home"] == str(homes.shared)
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] == str(homes.shared)
    assert not credentials.exists()
    # The profile's own login is untouched by a shared sign-out.
    assert (homes.system / "alice" / "coding-cli" / "claude" / ".credentials.json").exists()


def test_logout_removes_the_credential_even_when_the_cli_fails(monkeypatch, homes):
    """A failed ``claude auth logout`` must not leave a live refresh token on
    disk under a home the user believes they signed out of."""
    alice_home = homes.system / "alice" / "coding-cli" / "claude"
    credentials = _sign_in(alice_home, "alice-tok")
    _patch_exec(monkeypatch, error=OSError("binary is a directory"))

    out = asyncio.run(runner.logout(CLI_VARS, "alice", scope="profile"))
    assert out["ok"] is True
    assert not credentials.exists()
    assert "failed" in out["detail"]


def test_logout_without_a_cli_still_removes_the_credential(monkeypatch, homes):
    alice_home = homes.system / "alice" / "coding-cli" / "claude"
    credentials = _sign_in(alice_home, "alice-tok")
    monkeypatch.setattr(runner, "find_cli", lambda variables=None: None)

    out = asyncio.run(runner.logout({}, "alice", scope="profile"))
    assert out["ok"] is True
    assert not credentials.exists()
    assert "was not found" in out["detail"]


# -- find_cli -----------------------------------------------------------------


def _fake_sdk_package(tmp_path: Path, *, with_binary: bool = True):
    pkg = tmp_path / "site-packages" / "claude_agent_sdk"
    (pkg / "_bundled").mkdir(parents=True)
    if with_binary:
        (pkg / "_bundled" / runner._LOGIN_BINARY_NAME).write_text("#!/bin/sh\n", encoding="utf-8")
    module = types.ModuleType("claude_agent_sdk")
    module.__file__ = str(pkg / "__init__.py")
    return module, pkg / "_bundled" / runner._LOGIN_BINARY_NAME


def test_find_cli_prefers_the_tool_variable(monkeypatch, tmp_path):
    module, _bundled = _fake_sdk_package(tmp_path)
    monkeypatch.setattr(runner, "load_sdk", lambda: (module, None))
    assert runner.find_cli({Var.CLI_PATH: " /custom/claude "}) == "/custom/claude"
    assert runner.cli_binary_source({Var.CLI_PATH: "/custom/claude"}) == "tool_variable"


def test_find_cli_uses_the_sdks_bundled_binary(monkeypatch, tmp_path):
    """The bundled copy beats PATH: it is the binary the SDK itself drives, so a
    login written by a different ``claude`` would never be read by a task."""
    module, bundled = _fake_sdk_package(tmp_path)
    monkeypatch.setattr(runner, "load_sdk", lambda: (module, None))
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/claude")
    assert runner.find_cli({}) == str(bundled)
    assert runner.cli_binary_source({}) == "bundled"


def test_find_cli_falls_back_to_path(monkeypatch, tmp_path):
    module, _bundled = _fake_sdk_package(tmp_path, with_binary=False)
    monkeypatch.setattr(runner, "load_sdk", lambda: (module, None))
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/claude")
    assert runner.find_cli({}) == "/usr/bin/claude"
    assert runner.cli_binary_source({}) == "path"

    monkeypatch.setattr(runner, "load_sdk", lambda: (None, "not installed"))
    assert runner.find_cli({}) == "/usr/bin/claude"


def test_find_cli_returns_none_when_there_is_nothing(monkeypatch):
    monkeypatch.setattr(runner, "load_sdk", lambda: (None, "not installed"))
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner.find_cli({}) is None
    assert runner.cli_binary_source({}) is None


def test_argv_builders():
    assert runner.login_argv(CLI) == [CLI, "auth", "login"]
    assert runner.logout_argv(CLI) == [CLI, "auth", "logout"]
    assert runner.status_argv(CLI) == [CLI, "auth", "status", "--json"]
