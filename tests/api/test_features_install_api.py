"""``GET /api/features`` and ``POST /api/features/install`` after the
version-aware update work.

The Settings page decides between "Install", "Update required" and "Restart
required" from the GET body, and the install stream's final ``done`` frame is
the only place a client learns that a feature was updated rather than
installed. Both are pre-storage routes, so the handlers are driven directly
with a stubbed ``get_state`` and request stand-ins, the way
``test_features_capabilities.py`` drives the capabilities endpoint.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
from types import SimpleNamespace

import pytest

from app.api import features as features_api
from app.features import installer, manifest
from app.features.installer import InstallEvent, InstallResult

CODEX_REQ = "openai-codex>=0.154.0,<0.155"


def _stub_state(monkeypatch: pytest.MonkeyPatch, *, setup_complete: bool) -> None:
    fake_state = SimpleNamespace(
        storage_ready=setup_complete,
        config_storage=SimpleNamespace(is_setup_complete=lambda: setup_complete),
    )
    monkeypatch.setattr(features_api, "get_state", lambda: fake_state)


def _request(username: str | None = None, body: dict | None = None) -> object:
    """Request stand-in: ``user`` for the admin gate, ``json()`` for the body."""
    user = (
        SimpleNamespace(is_authenticated=True, username=username)
        if username
        else SimpleNamespace(is_authenticated=False, username="")
    )

    async def _json() -> dict:
        return body or {}

    return SimpleNamespace(headers={}, cookies={}, client=None, user=user, json=_json)


def _venv(monkeypatch: pytest.MonkeyPatch, *, installed: set[str], versions: dict[str, str]) -> None:
    """Answer the probe and dist metadata from a fixed venv, whatever the
    interpreter running the suite really has."""

    def fake_version(dist: str) -> str:
        if dist not in versions:
            raise importlib.metadata.PackageNotFoundError(dist)
        return versions[dist]

    monkeypatch.setattr(manifest, "is_installed", lambda key: key in installed)
    monkeypatch.setattr(installer, "is_installed", lambda key: key in installed)
    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    monkeypatch.setattr(installer, "_restart_pending", set())


async def _frames(response) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    async for chunk in response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        event_line, data_line = text.strip().split("\n", 1)
        frames.append((event_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: "))))
    return frames


# ── GET /api/features ────────────────────────────────────────────────────


def test_get_features_reports_update_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_state(monkeypatch, setup_complete=False)
    _venv(monkeypatch, installed={"codex"}, versions={"openai-codex": "0.1.0b3"})

    response = asyncio.run(features_api.get_features(_request()))
    body = json.loads(response.body)

    assert response.status_code == 200
    # Still the flat dict keyed by feature id that existing clients read.
    assert set(body) == set(manifest.FEATURES)
    assert body["codex"] == {
        "installed": True,
        "requires_restart_after_install": False,
        "extras": ["codex"],
        "outdated": True,
        "required": [CODEX_REQ],
        "installed_versions": {"openai-codex": "0.1.0b3"},
        "restart_pending": False,
    }
    assert body["browser"]["outdated"] is False
    assert body["browser"]["required"] == []


def test_get_features_reports_a_pending_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_state(monkeypatch, setup_complete=True)
    _venv(monkeypatch, installed={"codex"}, versions={"openai-codex": "0.154.0"})
    installer._mark_restart_pending(["codex"])

    response = asyncio.run(features_api.get_features(_request("admin")))
    body = json.loads(response.body)

    assert body["codex"]["outdated"] is False
    assert body["codex"]["restart_pending"] is True


def test_get_features_is_admin_only_after_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_state(monkeypatch, setup_complete=True)

    assert asyncio.run(features_api.get_features(_request("alice"))).status_code == 403
    assert asyncio.run(features_api.get_features(_request())).status_code == 401


# ── POST /api/features/install ───────────────────────────────────────────


def test_done_frame_carries_upgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_state(monkeypatch, setup_complete=True)
    seen: dict[str, list[str]] = {}

    def fake_install(keys: list[str], emit) -> InstallResult:
        seen["keys"] = keys
        emit(InstallEvent("log", "pip install cremind openai-codex>=0.154.0,<0.155"))
        return InstallResult(restart_required=True, upgraded=["codex"])

    monkeypatch.setattr(installer, "install_features", fake_install)

    async def scenario() -> tuple[int, list[tuple[str, dict]]]:
        response = await features_api.post_install_features(
            _request("admin", {"features": ["codex"]}),
        )
        return response.status_code, await _frames(response)

    status, frames = asyncio.run(scenario())

    assert status == 200
    assert seen["keys"] == ["codex"]
    assert frames[0] == ("log", {
        "message": "pip install cremind openai-codex>=0.154.0,<0.155",
        "meta": {},
        "ok": True,
    })
    name, done = frames[-1]
    assert name == "done"
    assert done == {
        "restart_required": True,
        "installed": [],
        "failed": [],
        "already_present": [],
        "error": None,
        "upgraded": ["codex"],
        "ok": True,
    }


def test_install_is_admin_only_after_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_state(monkeypatch, setup_complete=True)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("a member must not reach pip")

    monkeypatch.setattr(installer, "install_features", must_not_run)

    response = asyncio.run(
        features_api.post_install_features(_request("alice", {"features": ["codex"]})),
    )

    assert response.status_code == 403
    assert json.loads(response.body) == {"error": "Admin profile required"}
