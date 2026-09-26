"""``adopt_existing`` on POST /api/config/setup.

``cremind profile create`` (and ``POST /api/profiles``) register a name and
nothing else: no model, no tools, no channels, no token. Setup used to answer
``409`` for any profile that already existed, so the only way to give that shell
a real configuration was to delete it — taking its conversations with it — and
start again. ``adopt_existing`` applies the payload on top instead.

Two properties make it safe enough to expose, and both are pinned here.

It is **never** accepted for ``admin``. The endpoint is reachable from the admin
agent's own shell, where ``CREMIND_TOKEN`` is already admin's, so a payload that
could name ``admin`` would let a prompt-injected turn rewrite the administering
profile's LLM config and mint itself a fresh token. Re-running first-run setup is
Settings → Reconfigure, a deliberate human action.

It is **not a revocation**. The new token is minted at the profile's current
serial, so every token issued to it earlier keeps working. That is the opposite
of what "a new token was issued" usually implies, so the response says so.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api import config as config_api


class _FakeConfigStorage:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def is_setup_complete(self) -> bool:
        return True  # non-first setup: the branch adoption lives in

    def get(self, table: str, key: str, **_kw: Any) -> str | None:
        if (table, key) == ("server_config", "jwt_secret"):
            return "test-jwt-secret-long-enough-for-hs256"
        return None

    def set(self, table: str, key: str, value: str, is_secret: bool = False,
            profile: str | None = None) -> None:
        self.writes.append({"table": table, "key": key, "value": value, "profile": profile})

    def llm_config_for(self, profile: str) -> dict[str, str]:
        return {
            w["key"]: w["value"]
            for w in self.writes if w["table"] == "llm_config" and w["profile"] == profile
        }


class _FakeConversationStorage:
    def __init__(self, *, exists: bool) -> None:
        self._exists = exists
        self.created: list[str] = []

    async def profile_exists(self, _name: str) -> bool:
        return self._exists

    async def create_profile(self, name: str) -> None:
        self.created.append(name)
        self._exists = True


def _handler(config_storage, conversation_storage) -> Callable:
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=config_storage,
        conversation_storage=conversation_storage,
        registry=None,
        on_first_setup=None,
        boot_fn=None,
    )
    for route in config_api.get_config_routes(state):  # type: ignore[arg-type]
        if route.path == "/api/config/setup" and "POST" in route.methods:
            return route.endpoint
    raise AssertionError("POST /api/config/setup route not registered")


def _request(body: dict[str, Any]):
    async def _json() -> dict:
        return body

    return SimpleNamespace(headers={"host": "localhost:1515"}, cookies={}, json=_json)


@pytest.fixture
def setup_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Neutralise the side effects that are not what these tests are about."""
    # Setup creates the profile's working directory; keep it out of the real one.
    monkeypatch.setenv("CREMIND_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setattr(config_api, "require_admin", lambda _req: None)
    monkeypatch.setattr(config_api, "_features_required_by_setup_payload", lambda _b: [])
    monkeypatch.setattr(config_api, "ensure_persona_file", lambda _p: None)
    monkeypatch.setattr("app.auth.write_token_file", lambda _p, _t: str(tmp_path / "t"))


def _run(body: dict[str, Any], *, exists: bool):
    storage = _FakeConfigStorage()
    conversations = _FakeConversationStorage(exists=exists)
    response = asyncio.run(_handler(storage, conversations)(_request(body)))
    return response, storage, conversations


def test_an_existing_profile_is_still_refused_without_the_flag(setup_env) -> None:
    response, _storage, conversations = _run({"profile": "javis"}, exists=True)
    assert response.status_code == 409, response.body
    assert "already exists" in json.loads(response.body)["error"]
    assert conversations.created == []


def test_adopting_configures_the_existing_profile_without_recreating_it(setup_env) -> None:
    response, storage, conversations = _run(
        {
            "profile": "javis",
            "adopt_existing": True,
            "llm_config": {"model_group.high": "anthropic/claude-sonnet-5"},
        },
        exists=True,
    )
    assert response.status_code == 200, response.body
    body = json.loads(response.body)
    assert body["profile"] == "javis"
    assert body["token"], "adoption must still mint a token — that is the point"
    assert conversations.created == [], "the profile was there; do not re-create it"
    assert storage.llm_config_for("javis") == {"model_group.high": "anthropic/claude-sonnet-5"}


def test_adoption_is_reported_as_a_warning_that_says_old_tokens_still_work(setup_env) -> None:
    """Silence here would be the dangerous reading: an operator who assumes the
    old token died would leave a live credential in circulation."""
    response, _storage, _conversations = _run(
        {"profile": "javis", "adopt_existing": True}, exists=True,
    )
    warnings = json.loads(response.body)["warnings"]
    adopted = [w for w in warnings if w.get("code") == "adopted_existing"]
    assert adopted, f"no adopted_existing warning in {warnings}"
    assert "remain valid" in adopted[0]["message"]


def test_the_flag_is_refused_for_admin(setup_env) -> None:
    """Reachable from the admin agent's own shell, so this guard has to be
    server-side — the CLI refusing it is not enough."""
    response, _storage, conversations = _run(
        {"profile": "admin", "adopt_existing": True}, exists=True,
    )
    assert response.status_code == 400, response.body
    assert "admin profile cannot be adopted" in json.loads(response.body)["error"]
    assert conversations.created == []


@pytest.mark.parametrize("raw", ["true", "1", True])
def test_the_flag_accepts_the_shapes_a_json_client_sends(setup_env, raw) -> None:
    response, _storage, _conversations = _run(
        {"profile": "javis", "adopt_existing": raw}, exists=True,
    )
    assert response.status_code == 200, response.body


@pytest.mark.parametrize("raw", ["false", "", None, 0])
def test_anything_else_leaves_the_409_in_place(setup_env, raw) -> None:
    response, _storage, _conversations = _run(
        {"profile": "javis", "adopt_existing": raw}, exists=True,
    )
    assert response.status_code == 409, response.body


def test_a_profile_that_does_not_exist_is_created_as_before(setup_env) -> None:
    """Adoption is "apply to whatever is there", not "require it to be there":
    a wizard that raced someone deleting the profile should still work."""
    response, _storage, conversations = _run(
        {"profile": "javis", "adopt_existing": True}, exists=False,
    )
    assert response.status_code == 200, response.body
    assert conversations.created == ["javis"]
    assert not [
        w for w in json.loads(response.body)["warnings"] if w.get("code") == "adopted_existing"
    ], "nothing was adopted, so nothing should claim it was"


def test_a_token_minted_before_adoption_still_verifies_afterwards(
    setup_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The serial is untouched, so adoption does not lock anyone out. If this
    ever changes, the warning above becomes a lie and every agent shell holding
    that profile's token stops working mid-turn."""
    from app.config.settings import BaseConfig

    secret = "test-jwt-secret-long-enough-for-hs256"
    monkeypatch.setattr("app.auth.serial.current_serial", lambda _p: 7)
    monkeypatch.setattr("app.auth.serial.serial_matches", lambda _p, _s: True)
    before, _expires = BaseConfig.mint_token("javis", secret=secret)

    response, _storage, _conversations = _run(
        {"profile": "javis", "adopt_existing": True}, exists=True,
    )
    assert response.status_code == 200, response.body

    import jwt

    claims = jwt.decode(before, secret, algorithms=["HS256"])
    after = jwt.decode(json.loads(response.body)["token"], secret, algorithms=["HS256"])
    assert claims["tsr"] == after["tsr"] == 7


# ── reserved names ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["shared", "cli"])
def test_a_new_profile_may_not_take_a_name_the_manual_reserves(setup_env, name: str) -> None:
    """``shared`` and ``cli`` are scope names of Cremind's own manual: a
    profile named ``shared`` would merge its pages into the bundled manual,
    and deleting it would prune that manual for everyone."""
    response, _storage, conversations = _run({"profile": name}, exists=False)
    assert response.status_code == 400, response.body
    assert "reserved" in json.loads(response.body)["error"]
    assert conversations.created == []


def test_a_profile_created_before_the_reservation_can_still_be_adopted(setup_env) -> None:
    """Refusing it would leave that profile impossible to configure."""
    response, _storage, conversations = _run(
        {"profile": "shared", "adopt_existing": True}, exists=True,
    )
    assert response.status_code == 200, response.body
    assert conversations.created == []
