"""What the Setup Wizard is allowed to persist into ``llm_config``.

The wizard collects a provider's ``auth_method`` alongside its credentials,
but it cannot finish a browser OAuth sign-in — that flow needs a running
server, a loopback listener and a user in a browser. A payload that names an
OAuth method (``kind = "oauth"`` in the provider catalog; today only OpenAI's
``codex_oauth``) with no tokens to go with it therefore leaves the brand-new
profile pinned to a backend it cannot reach: "Not Configured" on the LLM page
and only the Codex-servable models on offer, with the API key the user typed
sitting unused.

``POST /api/config/setup`` is unauthenticated on first setup, so the frontend
not sending that key is not a guarantee. These tests pin the backend defense:

  Seam — app.api.config._drop_unusable_oauth_auth_methods (the filter itself)
  Handler — the same filter as reached through the setup handler's persist
  loop, which also pins that ordinary keys (API keys, model groups, the Plan
  model) still land under the new profile.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api import config as config_api


# ── Seam — the filter itself ──────────────────────────────────────────────


def test_codex_oauth_without_tokens_is_dropped() -> None:
    out = config_api._drop_unusable_oauth_auth_methods(
        {"openai.auth_method": "codex_oauth"}
    )
    assert "openai.auth_method" not in out


def test_dropping_the_auth_method_keeps_everything_else() -> None:
    """Only the one offending key goes — the credentials the user typed and
    the model choices around it must survive untouched."""
    payload = {
        "provider": "openai",
        "openai.auth_method": "codex_oauth",
        "openai.api_key": "sk-test",
        "model_group.plan": "openai/gpt-5.4",
    }
    out = config_api._drop_unusable_oauth_auth_methods(payload)
    assert out == {
        "provider": "openai",
        "openai.api_key": "sk-test",
        "model_group.plan": "openai/gpt-5.4",
    }
    # The caller's dict is not mutated.
    assert "openai.auth_method" in payload


def test_codex_oauth_with_tokens_is_kept() -> None:
    """Restore-from-backup and SETUP_WIZARD_ENV presets hand us the tokens
    alongside the choice, so the selection is genuinely usable."""
    out = config_api._drop_unusable_oauth_auth_methods(
        {
            "openai.auth_method": "codex_oauth",
            "openai.oauth_token": "{\"access_token\": \"abc\"}",
        }
    )
    assert out["openai.auth_method"] == "codex_oauth"


def test_api_key_auth_method_is_untouched() -> None:
    out = config_api._drop_unusable_oauth_auth_methods(
        {"openai.auth_method": "api_key", "openai.api_key": "sk-test"}
    )
    assert out["openai.auth_method"] == "api_key"


def test_device_code_auth_method_is_untouched() -> None:
    """GitHub Copilot's device-code flow DOES complete during setup, so it is
    deliberately outside the filter."""
    out = config_api._drop_unusable_oauth_auth_methods(
        {"github-copilot.auth_method": "device_code"}
    )
    assert out["github-copilot.auth_method"] == "device_code"


def test_unknown_provider_keeps_its_selection() -> None:
    """No catalog file (a ``custom:<slug>`` provider, or a typo) means no
    oauth methods to match against — the key falls through rather than being
    guessed at."""
    out = config_api._drop_unusable_oauth_auth_methods(
        {"custom:my-gateway.auth_method": "api_key", "nope.auth_method": "codex_oauth"}
    )
    assert out["custom:my-gateway.auth_method"] == "api_key"
    assert out["nope.auth_method"] == "codex_oauth"


def test_an_unreadable_catalog_keeps_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog problem must never break setup — fall through, don't raise."""

    def _boom(_name: str) -> dict:
        raise OSError("catalog on fire")

    monkeypatch.setattr("app.config.load_provider_catalog", _boom)
    out = config_api._drop_unusable_oauth_auth_methods(
        {"openai.auth_method": "codex_oauth"}
    )
    assert out["openai.auth_method"] == "codex_oauth"


def test_bare_auth_method_is_untouched() -> None:
    """An unprefixed ``auth_method`` names no provider, so there is no catalog
    to check it against."""
    out = config_api._drop_unusable_oauth_auth_methods({"auth_method": "codex_oauth"})
    assert out["auth_method"] == "codex_oauth"


# ── Handler — POST /api/config/setup ──────────────────────────────────────


class _FakeConfigStorage:
    """Records every ``set`` the setup handler makes, keyed like the real one."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def is_setup_complete(self) -> bool:
        # Non-first setup: skips the bootstrap/boot/server_config branches so
        # the test can reach the llm_config persist loop without a real DB.
        return True

    def get(self, table: str, key: str, **_kw: Any) -> str | None:
        if (table, key) == ("server_config", "jwt_secret"):
            # 32+ bytes: shorter HMAC keys make PyJWT emit a warning.
            return "test-jwt-secret-long-enough-for-hs256"
        return None

    def set(
        self,
        table: str,
        key: str,
        value: str,
        is_secret: bool = False,
        profile: str | None = None,
    ) -> None:
        self.writes.append(
            {"table": table, "key": key, "value": value, "is_secret": is_secret, "profile": profile}
        )

    def llm_config_for(self, profile: str) -> dict[str, str]:
        return {
            w["key"]: w["value"]
            for w in self.writes
            if w["table"] == "llm_config" and w["profile"] == profile
        }


class _FakeConversationStorage:
    async def profile_exists(self, _name: str) -> bool:
        return False

    async def create_profile(self, _name: str) -> None:
        return None


def _get_setup_handler(config_storage: _FakeConfigStorage) -> Callable:
    """Pull ``handle_setup`` out of the ``get_config_routes`` closure.

    Same approach as ``tests/api/test_install_secrets.py`` — the handler
    captures ``state``, so we register the routes with a stub state and look
    the endpoint up by path + method.
    """
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=config_storage,
        conversation_storage=_FakeConversationStorage(),
        # No registry → the skill-seed and tool_configs loops are skipped;
        # no on_first_setup → no post-setup tool binding. Neither is what
        # these tests are about.
        registry=None,
        on_first_setup=None,
        boot_fn=None,
    )
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    for route in routes:
        if route.path == "/api/config/setup" and "POST" in route.methods:
            return route.endpoint
    raise AssertionError("POST /api/config/setup route not registered")


def _make_request(body: dict[str, Any]) -> object:
    """Minimal Starlette-compatible Request stand-in with a JSON body."""

    async def _json() -> dict:
        return body

    return SimpleNamespace(headers={"host": "localhost:1515"}, cookies={}, json=_json)


@pytest.fixture
def setup_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> _FakeConfigStorage:
    """Neutralise everything the persist loop is not about.

    The wizard's side effects (pip installs, persona files, token files) all
    touch the real filesystem or network; stub them so the handler runs to the
    end and the recorded ``llm_config`` writes are the only thing under test.
    """
    monkeypatch.setattr(config_api, "require_admin", lambda _req: None)
    monkeypatch.setattr(config_api, "_features_required_by_setup_payload", lambda _b: [])
    monkeypatch.setattr(config_api, "ensure_persona_file", lambda _p: None)
    monkeypatch.setattr("app.auth.write_token_file", lambda _p, _t: str(tmp_path / "t"))
    return _FakeConfigStorage()


def _run_setup(storage: _FakeConfigStorage, llm_config: dict[str, str]) -> dict[str, str]:
    handler = _get_setup_handler(storage)
    response = asyncio.run(
        handler(_make_request({"profile": "second", "llm_config": llm_config}))
    )
    assert response.status_code == 200, response.body
    return storage.llm_config_for("second")


def test_setup_does_not_persist_a_token_less_oauth_method(setup_env) -> None:
    persisted = _run_setup(
        setup_env,
        {
            "provider": "openai",
            "openai.auth_method": "codex_oauth",
            "openai.api_key": "sk-test",
        },
    )
    assert "openai.auth_method" not in persisted
    # The rest of the payload is still written for the new profile.
    assert persisted["provider"] == "openai"
    assert persisted["openai.api_key"] == "sk-test"


def test_setup_persists_an_oauth_method_that_came_with_tokens(setup_env) -> None:
    persisted = _run_setup(
        setup_env,
        {
            "openai.auth_method": "codex_oauth",
            "openai.oauth_token": "{\"access_token\": \"abc\"}",
        },
    )
    assert persisted["openai.auth_method"] == "codex_oauth"


def test_setup_persists_a_plain_api_key_auth_method(setup_env) -> None:
    persisted = _run_setup(
        setup_env,
        {"openai.auth_method": "api_key", "openai.api_key": "sk-test"},
    )
    assert persisted["openai.auth_method"] == "api_key"


def test_setup_persists_the_plan_model_group(setup_env) -> None:
    """The wizard's Plan Model picker writes through the same loop; pin it so a
    future filter can't quietly eat model-group keys."""
    persisted = _run_setup(
        setup_env,
        {
            "model_group.plan": "openai/gpt-5.4",
            "model_group.plan.reasoning_effort": "high",
        },
    )
    assert persisted["model_group.plan"] == "openai/gpt-5.4"
    assert persisted["model_group.plan.reasoning_effort"] == "high"


def test_setup_still_classifies_secrets(setup_env) -> None:
    """The filter runs before the is_secret classification and must not
    disturb it: keys stay secret/non-secret exactly as before."""
    _run_setup(
        setup_env,
        {"openai.auth_method": "api_key", "openai.api_key": "sk-test"},
    )
    by_key = {
        w["key"]: w["is_secret"]
        for w in setup_env.writes
        if w["table"] == "llm_config"
    }
    assert by_key["openai.api_key"] is True
    assert by_key["openai.auth_method"] is False
