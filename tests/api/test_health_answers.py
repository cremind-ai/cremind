"""Every answer ``GET /health`` gives, as the desktop app reads them.

The Electron shell asks ``/health`` whether a backend is up: after "Continue to
Setup Wizard" starts one, and on every launch to adopt one already running. A
fresh install answers ``setup_pending`` until its Setup Wizard has run, and a
shell that took only ``ok`` stranded every new install at that step.

``ui/electron/fixtures/health.json`` records the answers, and the shell's own
test (``ui/electron/https.test.mjs``) requires it to adopt exactly the 200s.
These tests keep that record true: change what ``/health`` says and they fail
until the fixture — and with it the shell's test — follows.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest

from app.api import version as version_api
from app.config import bootstrap
from app.config.settings import BaseConfig

_FIXTURE = Path(__file__).resolve().parents[2] / "ui" / "electron" / "fixtures" / "health.json"
_ANSWERS = json.loads(_FIXTURE.read_text(encoding="utf-8"))["answers"]


class _Database:
    def __init__(self, up: bool) -> None:
        self.up = up

    async def health_check(self) -> None:
        if not self.up:
            raise ConnectionError("database unreachable")


def _arrange(monkeypatch: pytest.MonkeyPatch, db: str, vectorstore: str) -> None:
    """Put each subsystem in the state its ``/health`` field names."""
    monkeypatch.setattr(bootstrap, "bootstrap_exists", lambda: db != "deferred")
    monkeypatch.setattr("app.databases.get_database_provider", lambda: _Database(db == "ok"))

    def embedding_enabled() -> bool:
        if vectorstore == "error":
            raise RuntimeError("embedding config unreadable")
        return vectorstore == "ok"

    monkeypatch.setattr(BaseConfig, "is_embedding_enabled", embedding_enabled)


def _health(monkeypatch: pytest.MonkeyPatch, db: str, vectorstore: str) -> tuple[int, dict]:
    _arrange(monkeypatch, db, vectorstore)
    response = asyncio.run(version_api.get_health(None))
    return response.status_code, json.loads(response.body)


def test_the_record_covers_every_state() -> None:
    recorded = [(a["body"]["db"], a["body"]["vectorstore"]) for a in _ANSWERS]
    assert sorted(recorded) == sorted(itertools.product(("deferred", "ok", "error"), ("disabled", "ok", "error")))


@pytest.mark.parametrize(
    "answer", _ANSWERS, ids=lambda a: f"db={a['body']['db']},vectorstore={a['body']['vectorstore']}",
)
def test_health_answers_as_recorded(monkeypatch: pytest.MonkeyPatch, answer: dict) -> None:
    code, body = _health(monkeypatch, answer["body"]["db"], answer["body"]["vectorstore"])
    assert isinstance(body.pop("boot_id"), str)
    assert (code, body) == (answer["code"], answer["body"])


@pytest.mark.parametrize("vectorstore", ["disabled", "ok"])
def test_a_fresh_install_is_serving(monkeypatch: pytest.MonkeyPatch, vectorstore: str) -> None:
    """Before its Setup Wizard has run, a backend still answers 200: the
    desktop app starts it for that wizard and waits for exactly this."""
    code, body = _health(monkeypatch, "deferred", vectorstore)
    assert (code, body["status"]) == (200, "setup_pending")
