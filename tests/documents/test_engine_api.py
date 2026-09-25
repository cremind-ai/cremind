"""The engine-backed API routes: error mapping and profile scoping.

The routes are thin — the engine does the work in a worker thread — so what
is worth pinning is the contract around them: an engine error becomes its own
status and code, a missing engine is a 503 (not a crash), and the profile is
always the caller's.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from starlette.datastructures import QueryParams  # noqa: E402

from app.api import documents as api  # noqa: E402
from app.documents import service as svc_module  # noqa: E402
from app.documents.errors import EngineError, NotFound  # noqa: E402


class _Req:
    def __init__(self, username="alice", body=None, query="", path_params=None):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.query_params = QueryParams(query)
        self.path_params = path_params or {}
        self._body = body or {}

    async def json(self):
        return self._body


class _StubService:
    def __init__(self):
        self.calls = []

    def control(self, profile, action, **kw):
        self.calls.append(("control", profile, action, kw))
        if action == "rebuild":
            raise EngineError("confirm", code="ConfirmationRequired", plan={"effects": []}, confirm="tok")
        return {"accepted": True}

    def list_files(self, profile, **kw):
        self.calls.append(("files", profile, kw))
        return {"files": [], "next": None}

    def file_detail(self, profile, fid):
        raise NotFound("No such file.")

    def activity(self, profile, **kw):
        return {"events": [{"kind": "added", "profile": profile}]}

    def get_estimate(self, profile):
        return {"state": "none"}

    def start_estimate(self, profile):
        return {"state": "running"}

    def storage(self, profile):
        return {"level": "ok"}


def _route(path, method):
    for r in api.get_documents_routes():
        if r.path == path and method in (r.methods or ()):
            return r.endpoint
    raise AssertionError(path)


def _call(path, method, **req):
    resp = asyncio.run(_route(path, method)(_Req(**req)))
    return resp.status_code, json.loads(resp.body)


def test_no_engine_is_a_503(monkeypatch):
    monkeypatch.setattr(svc_module, "_service", None)
    status, body = _call("/api/documentation-search/files", "GET")
    assert status == 503
    assert body["error"] == "EngineNotRunning"


def test_errors_keep_their_status_and_fields(monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(svc_module, "_service", stub)
    status, body = _call("/api/documentation-search/control", "POST", body={"action": "rebuild"})
    assert status == 409
    assert body["error"] == "ConfirmationRequired" and body["confirm"] == "tok"
    status, body = _call("/api/documentation-search/files/{fid}", "GET", path_params={"fid": "nope1234"})
    assert status == 404


def test_the_profile_is_always_the_callers(monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(svc_module, "_service", stub)
    status, _ = _call("/api/documentation-search/control", "POST", username="bob",
                      body={"action": "pause", "profile": "alice"})
    assert status == 202
    name, profile, action, kw = stub.calls[-1]
    assert profile == "bob" and action == "pause"
    # A "profile" in the body is dropped, never the target.
    assert "profile" not in kw

    _call("/api/documentation-search/files", "GET", username="bob", query="status=error&limit=5")
    assert stub.calls[-1][1] == "bob"
    assert stub.calls[-1][2]["status"] == "error" and stub.calls[-1][2]["limit"] == 5


def test_bad_query_numbers_are_a_400(monkeypatch):
    monkeypatch.setattr(svc_module, "_service", _StubService())
    status, body = _call("/api/documentation-search/files", "GET", query="limit=lots")
    assert status == 400
