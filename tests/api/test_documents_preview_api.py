"""API: the file tree's index lookup and the "Indexed content" preview.

- ``POST /files/lookup`` answers only for paths inside the caller's own
  indexed folder: another profile's working directory, the system folder, a
  link out of the folder and anything outside it are ``outside`` — whoever
  asks, the admin included. With the local folder off, no status is shown.
- ``GET /files/{fid}/preview`` reads the index only (no extraction, no model),
  pages through every passage, and a cursor from before a re-index is a
  ``409 StalePreview``.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from starlette.datastructures import QueryParams  # noqa: E402

from app.api import documents_files as api  # noqa: E402
from app.documents.discovery.walker import path_hash  # noqa: E402
from app.documents.textnorm import text_hash  # noqa: E402
from app.documents.types import Chunk, ChunkDiff  # noqa: E402
from tests.documents._citations_env import build, close  # noqa: E402


class _Req:
    def __init__(self, username="alice", body=None, query="", path_params=None, authenticated=True):
        self.user = SimpleNamespace(is_authenticated=authenticated, username=username)
        self.query_params = QueryParams(query)
        self.path_params = path_params or {}
        self._body = body or {}

    async def json(self):
        return self._body


def _route(path: str, method: str):
    for r in api.get_documents_files_routes():
        if r.path == path and method in (r.methods or ()):
            return r.endpoint
    raise AssertionError(f"no route {method} {path}")


def _call(path, method, **req):
    resp = asyncio.run(_route(path, method)(_Req(**req)))
    return resp.status_code, json.loads(resp.body)


LOOKUP = "/api/documentation-search/files/lookup"
PREVIEW = "/api/documentation-search/files/{fid}/preview"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = build(tmp_path, monkeypatch)
    # A file indexed under its real path key, with a card and passages.
    (e.root / "Reports").mkdir()
    (e.root / "Reports" / "annual.pdf").write_bytes(b"%PDF-1.4 fake")
    rel = "Reports/annual.pdf"
    row = e.db.insert_file("local", rel, path_hash(rel), name="annual.pdf", kind="pdf", status="indexed")
    texts = ["File: annual.pdf"] + [f"Section {i} of the annual report." for i in range(40)]
    chunks = [Chunk(ordinal=-1 if i == 0 else i - 1, ctype="file_card" if i == 0 else "body", heading="",
                    text=x, text_hash=text_hash("", x), locator={} if i == 0 else {"page": i})
              for i, x in enumerate(texts)]
    e.db.apply_chunks(file_id=row["id"], folder_id=None, source="local", diff=ChunkDiff(add=chunks))
    e.annual = e.db.get_file(row["id"])
    yield e
    close(e)


def test_routes_need_a_token(env):
    status, _ = _call(LOOKUP, "POST", authenticated=False, body={"paths": []})
    assert status == 401
    status, _ = _call(PREVIEW, "GET", authenticated=False, path_params={"fid": env.annual["cite_id"]})
    assert status == 401


def test_lookup_reports_indexed_unmatched_and_outside(env, tmp_path):
    inside = str(env.root / "Reports" / "annual.pdf")
    unindexed = str(env.root / "Reports" / "draft.pdf")
    (env.root / "Reports" / "draft.pdf").write_bytes(b"x")
    outside = str(tmp_path / "elsewhere.pdf")
    status, body = _call(LOOKUP, "POST", body={"paths": [inside, unindexed, outside, "relative.pdf"]})
    assert status == 200 and body["enabled"] is True
    item = body["items"][inside]
    assert item["state"] == "indexed" and item["fid"] == env.annual["cite_id"]
    assert item["summary"]["state"] == "complete" and item["summary"]["readable"]["passages"] == 40
    assert body["items"][unindexed]["state"] == "unmatched"
    assert body["items"][outside]["state"] == "outside"
    assert body["items"]["relative.pdf"]["state"] == "outside"


def test_lookup_refuses_another_profiles_folder_and_links_out(env, tmp_path):
    # bob's working directory placed inside alice's folder: never looked up.
    bob = env.root / "Bob"
    bob.mkdir()
    (bob / "secret.pdf").write_bytes(b"x")
    env.working_dirs.rows["bob"] = str(bob)
    from app.config import working_dirs

    working_dirs._snapshot = None
    status, body = _call(LOOKUP, "POST", body={"paths": [str(bob / "secret.pdf")]})
    assert body["items"][str(bob / "secret.pdf")]["state"] == "outside"
    # A link inside the folder pointing out of it.
    target = tmp_path / "outside.txt"
    target.write_text("x", encoding="utf-8")
    link = env.root / "Reports" / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    status, body = _call(LOOKUP, "POST", body={"paths": [str(link)]})
    assert body["items"][str(link)]["state"] == "outside"


def test_lookup_is_the_callers_own_index(env):
    inside = str(env.root / "Reports" / "annual.pdf")
    # bob has the feature off: no status at all, even for alice's path.
    status, body = _call(LOOKUP, "POST", username="bob", body={"paths": [inside]})
    assert status == 200 and body == {"enabled": False, "root": None, "items": {}}


def test_lookup_hides_everything_when_the_folder_is_off(env):
    env.storage.upsert_source("alice", "local", enabled=False)
    status, body = _call(LOOKUP, "POST", body={"paths": [str(env.root / "Reports" / "annual.pdf")]})
    assert body["enabled"] is False and body["items"] == {}


@pytest.mark.parametrize("body", [{"paths": "x"}, {"paths": [1]}, {"paths": ["a"] * 501}])
def test_lookup_validates(env, body):
    status, out = _call(LOOKUP, "POST", body=body)
    assert status == 400 and out["error"] == "ValidationFailed"


def test_preview_pages_and_goes_stale(env):
    fid = env.annual["cite_id"]
    status, first = _call(PREVIEW, "GET", path_params={"fid": fid}, query="limit=30")
    assert status == 200
    assert len(first["segments"]) == 30 and first["total_segments"] == 40
    assert first["metadata"]["card"] == "File: annual.pdf"
    assert first["summary"]["state"] == "complete"
    status, second = _call(PREVIEW, "GET", path_params={"fid": fid}, query=f"cursor={first['next_cursor']}")
    assert status == 200 and len(second["segments"]) == 10 and second["next_cursor"] is None
    assert [s["text"] for s in first["segments"] + second["segments"]] == [
        f"Section {i} of the annual report." for i in range(40)]
    # Re-indexed in between: the old cursor is refused, specifically.
    from app.documents.chunking import diff_chunks

    old = env.db.get_chunks(env.annual["id"])
    new_text = "Section 0 was rewritten."
    rows = env.db.chunk_rows([c.id for c in old])
    new = [Chunk(ordinal=r["ordinal"], ctype=r["ctype"], heading="",
                 text=new_text if r["text"].startswith("Section 0 ") else r["text"],
                 text_hash=text_hash("", new_text if r["text"].startswith("Section 0 ") else r["text"]),
                 locator=r["locator"]) for r in rows]
    env.db.apply_chunks(file_id=env.annual["id"], folder_id=None, source="local", diff=diff_chunks(old, new))
    status, stale = _call(PREVIEW, "GET", path_params={"fid": fid}, query=f"cursor={first['next_cursor']}")
    assert status == 409 and stale["error"] == "StalePreview"


def test_preview_is_profile_scoped_and_validates(env):
    fid = env.annual["cite_id"]
    status, body = _call(PREVIEW, "GET", username="bob", path_params={"fid": fid})
    assert status == 404
    status, body = _call(PREVIEW, "GET", path_params={"fid": fid}, query="limit=abc")
    assert status == 400
    status, body = _call(PREVIEW, "GET", path_params={"fid": fid}, query="cursor=garbage")
    assert status == 400
    status, body = _call(PREVIEW, "GET", path_params={"fid": "zzzzzzzz"})
    assert status == 404


def test_preview_calls_no_model_and_extracts_nothing(env, monkeypatch):
    from app.documents import runtime as runtime_mod
    from app.documents.vision import captioner

    def boom(*a, **k):
        raise AssertionError("the preview must not run the pipeline or a model")

    monkeypatch.setattr(captioner, "run_vision", boom)
    monkeypatch.setattr(runtime_mod.ProfileRuntime, "process", boom)
    status, body = _call(PREVIEW, "GET", path_params={"fid": env.annual["cite_id"]})
    assert status == 200
    status, body = _call(LOOKUP, "POST", body={"paths": [str(env.root / "Reports" / "annual.pdf")]})
    assert status == 200
