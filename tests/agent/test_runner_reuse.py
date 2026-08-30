"""The reasoning runner is reused between turns while its config holds.

More than an object saved. A provider instance carries per-connection state that
is meant to span a conversation — most visibly the Codex path's prompt-cache
session id, which is generated in its constructor. ``_ensure_runner`` used to
write ``self._runners[profile]`` and never read it back, so a fresh provider was
built for every turn and the API was handed a brand-new cache key each time: a
conversation never read back a single cached token no matter how stable its
prompt was.

The cache has to key on what the config resolves to, so that changing the model
in Settings still takes effect on the next turn.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

from app.agent.agent import CremindAgent  # noqa: E402


class _Manager:
    """A model-group manager whose resolution the test can move."""

    def __init__(self):
        self.provider = "openai"
        self.model = "gpt-5.4-mini"
        self.effort = None
        self.built = 0

    def get_provider_and_model(self, _group, profile=None):
        return self.provider, self.model

    def _get_group_reasoning_effort(self, _group, profile=None):
        return self.effort

    def create_llm_for_model(self, profile=None):
        self.built += 1
        return object()


def _agent(manager):
    agent = CremindAgent.__new__(CremindAgent)
    agent._runners = {}
    agent._runner_keys = {}
    agent._ensure_setup = lambda _profile: manager
    return agent


def test_two_turns_share_one_provider():
    manager = _Manager()
    agent = _agent(manager)

    first = agent._ensure_runner("admin")
    second = agent._ensure_runner("admin")

    assert first is second
    assert manager.built == 1


def test_changing_the_model_swaps_it_on_the_next_turn():
    """No explicit invalidation anywhere — the key IS the resolved config."""
    manager = _Manager()
    agent = _agent(manager)

    first = agent._ensure_runner("admin")
    manager.model = "gpt-5.4"
    second = agent._ensure_runner("admin")

    assert first is not second
    assert manager.built == 2


def test_changing_the_reasoning_effort_swaps_it_too():
    manager = _Manager()
    agent = _agent(manager)

    agent._ensure_runner("admin")
    manager.effort = "high"
    agent._ensure_runner("admin")

    assert manager.built == 2


def test_each_profile_gets_its_own():
    """Profiles are independent tenants; one profile's provider must never
    answer for another's, whatever the two happen to resolve to."""
    manager = _Manager()
    agent = _agent(manager)

    assert agent._ensure_runner("admin") is not agent._ensure_runner("dog")
    assert manager.built == 2


def test_unreadable_config_still_produces_a_runner():
    """A turn must run. What it must not do is cache under a key it could not
    compute — the next turn would then reuse a provider for a config it never
    checked."""
    manager = _Manager()

    def _boom(*_a, **_kw):
        raise RuntimeError("config storage is down")

    manager.get_provider_and_model = _boom
    agent = _agent(manager)

    assert agent._ensure_runner("admin") is not None
    assert agent._runners == {}
    assert manager.built == 1
