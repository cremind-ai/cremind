"""API: citation resolution and the file viewer routes.

What these pin, in order of how much damage a regression would do:

- **Profile isolation.** A fid is looked up in the caller's own index only:
  bob asking for alice's file gets the same 404 as for an id that never
  existed, on every route. A conversation id must be the caller's too.
- **The root boundary for raw bytes.** A file is served only when its real
  path is inside the indexed folder *now* — a path that escapes it (a
  rewritten row, a symlink planted since indexing) is refused, and so is
  Cremind's own system folder.
- **Thumbnails leave nothing behind.** They are made in memory; generating
  one writes no file anywhere.
- **Executable formats are never served inline** from the app's origin.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import tempfile
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from starlette.datastructures import QueryParams  # noqa: E402

from app.api import documents_files as api  # noqa: E402
from app.documents import citations as cit  # noqa: E402
from tests.documents._citations_env import build, close  # noqa: E402


class _Req:
    def __init__(self, username="alice", body=None, query="", path_params=None, authenticated=True):
        self.user = SimpleNamespace(is_authenticated=authenticated, username=username)
        self.query_params = QueryParams(query)
        self.path_params = path_params or {}
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = build(tmp_path, monkeypatch)
    api.clear_thumbnail_cache()
    yield e
    api.clear_thumbnail_cache()
    close(e)


def _route(path: str, method: str):
    for r in api.get_documents_files_routes():
        if r.path == path and method in (r.methods or ()):
            return r.endpoint
    raise AssertionError(f"no route {method} {path}")


def _call(path, method, **req):
    return asyncio.run(_route(path, method)(_Req(**req)))


def _json(resp):
    return json.loads(resp.body)


def _body_bytes(resp) -> bytes:
    """A FileResponse's bytes, collected through the ASGI interface."""
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    scope = {"type": "http", "method": "GET", "headers": [], "path": "/"}
    asyncio.run(resp(scope, receive, send))
    return b"".join(chunks)


# ── auth ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,method", [
    ("/api/documentation-search/citations/resolve", "POST"),
    ("/api/documentation-search/files/{fid}/text", "GET"),
    ("/api/documentation-search/files/{fid}/raw", "GET"),
    ("/api/documentation-search/files/{fid}/thumbnail", "GET"),
])
def test_everything_needs_a_token(env, path, method):
    resp = _call(path, method, authenticated=False, path_params={"fid": env.law["cite_id"]})
    assert resp.status_code == 401


# ── resolve ────────────────────────────────────────────────────────────────


def test_resolve(env):
    law, c1 = env.law, env.law_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1)])
    token = env.token(law, c1)
    resp = _call("/api/documentation-search/citations/resolve", "POST",
                 body={"conversation_id": "c-web", "tokens": [token, "[doc:zzzzzzzz]", "junk"]})
    assert resp.status_code == 200
    items = _json(resp)["items"]
    assert items[token]["status"] == "verified"
    assert items[token]["file"]["name"] == "luat-dat-dai.pdf"
    assert items[token]["n"] is None and items[token]["quote_status"] is None
    assert items["[doc:zzzzzzzz]"]["status"] == "invalid"
    assert items["junk"]["status"] == "invalid"
    # Without a conversation, a registered token is "elsewhere".
    items = _json(_call("/api/documentation-search/citations/resolve", "POST", body={"tokens": [token]}))["items"]
    assert items[token]["status"] == "verified_elsewhere"


def test_resolve_is_profile_scoped(env):
    law, c1 = env.law, env.law_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1)])
    token = env.token(law, c1)
    # Another profile's conversation is not a way in.
    resp = _call("/api/documentation-search/citations/resolve", "POST", username="bob",
                 body={"conversation_id": "c-web", "tokens": [token]})
    assert resp.status_code == 404
    items = _json(_call("/api/documentation-search/citations/resolve", "POST", username="bob",
                        body={"tokens": [token]}))["items"]
    assert items[token]["status"] == "invalid" and items[token]["file"] is None


@pytest.mark.parametrize("body", [
    {"tokens": "[doc:k7m2xq9a]"},
    {"tokens": [1, 2]},
    {"tokens": ["[doc:k7m2xq9a]"] * 101},
    {"tokens": [], "conversation_id": 7},
])
def test_resolve_validates(env, body):
    assert _call("/api/documentation-search/citations/resolve", "POST", body=body).status_code == 400


# ── text ───────────────────────────────────────────────────────────────────


def test_text_around_a_chunk(env):
    law, c2 = env.law, env.law_chunks[1]
    resp = _call("/api/documentation-search/files/{fid}/text", "GET", path_params={"fid": law["cite_id"]},
                 query=f"chunk={c2.text_hash[:8]}&context=1")
    assert resp.status_code == 200
    body = _json(resp)
    assert body["name"] == "luat-dat-dai.pdf" and body["kind"] == "pdf" and body["source"] == "local"
    assert [s["highlight"] for s in body["segments"]] == [False, True, False]
    assert body["segments"][1]["token"] == env.token(law, c2)
    assert body["segments"][1]["locator_label"] == "p. 2"
    assert body["truncated"] is False

    body = _json(_call("/api/documentation-search/files/{fid}/text", "GET", path_params={"fid": law["cite_id"]},
                       query="pages=3"))
    assert [s["locator"]["page"] for s in body["segments"]] == [3]
    body = _json(_call("/api/documentation-search/files/{fid}/text", "GET", path_params={"fid": law["cite_id"]}))
    assert len(body["segments"]) == 3


def test_text_errors(env):
    law = env.law
    fid = {"fid": law["cite_id"]}
    assert _call("/api/documentation-search/files/{fid}/text", "GET", path_params=fid,
                 query="chunk=0badc0de").status_code == 404
    assert _call("/api/documentation-search/files/{fid}/text", "GET", path_params=fid,
                 query="chunk=nothex!!").status_code == 400
    assert _call("/api/documentation-search/files/{fid}/text", "GET", path_params=fid,
                 query="pages=a-b").status_code == 400
    assert _call("/api/documentation-search/files/{fid}/text", "GET",
                 path_params={"fid": "zzzzzzzz"}).status_code == 404
    assert _call("/api/documentation-search/files/{fid}/text", "GET",
                 path_params={"fid": "../../etc"}).status_code == 404


def test_another_profiles_fid_is_a_404_everywhere(env):
    fid = {"fid": env.law["cite_id"]}
    for path in ("/api/documentation-search/files/{fid}/text", "/api/documentation-search/files/{fid}/raw",
                 "/api/documentation-search/files/{fid}/thumbnail"):
        resp = _call(path, "GET", username="bob", path_params=fid)
        assert resp.status_code == 404, path
        assert _json(resp)["error"] == "NotFound"


# ── raw ────────────────────────────────────────────────────────────────────


def test_raw_serves_the_file(env):
    resp = _call("/api/documentation-search/files/{fid}/raw", "GET", path_params={"fid": env.law["cite_id"]})
    assert resp.status_code == 200
    assert resp.media_type == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert _body_bytes(resp) == b"%PDF-1.4 fake"


def test_raw_refuses_a_path_outside_the_root(env, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours", encoding="utf-8")
    env.db.update_file(env.note["id"], rel_path="../secret.txt")
    resp = _call("/api/documentation-search/files/{fid}/raw", "GET", path_params={"fid": env.note["cite_id"]})
    assert resp.status_code == 403
    assert _json(resp)["error"] == "OutsideRoot"


def test_raw_refuses_a_symlink_out_of_the_root(env, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours", encoding="utf-8")
    link = env.root / "Notes" / "link.md"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    env.db.update_file(env.note["id"], rel_path="Notes/link.md")
    resp = _call("/api/documentation-search/files/{fid}/raw", "GET", path_params={"fid": env.note["cite_id"]})
    assert resp.status_code == 403


def test_raw_never_serves_active_content_inline(env):
    page = env.root / "Notes" / "page.html"
    page.write_text("<script>alert(1)</script>", encoding="utf-8")
    rec = env.db.insert_file("local", "Notes/page.html", "h-html", name="page.html", kind="html",
                             status="indexed", mime="text/html")
    resp = _call("/api/documentation-search/files/{fid}/raw", "GET", path_params={"fid": rec["cite_id"]})
    assert resp.status_code == 200
    assert resp.media_type == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in resp.headers["content-security-policy"]


def test_raw_drive_and_missing_files(env):
    rec = env.db.insert_file("drive", "Shared/plan.gdoc", "h-drive", name="plan", kind="docx",
                             status="indexed", drive_web_link="https://docs.google.com/d/x")
    resp = _call("/api/documentation-search/files/{fid}/raw", "GET", path_params={"fid": rec["cite_id"]})
    assert resp.status_code == 409
    assert _json(resp) == {
        "error": "DriveFile", "message": "This file lives in Google Drive; open it there.",
        "web_link": "https://docs.google.com/d/x",
    }
    (env.root / "Notes" / "q3.md").unlink()
    resp = _call("/api/documentation-search/files/{fid}/raw", "GET", path_params={"fid": env.note["cite_id"]})
    assert resp.status_code == 404 and _json(resp)["error"] == "FileMissing"


def test_raw_rechecks_the_saved_root_when_the_engine_has_none(env, tmp_path, monkeypatch):
    from app.documents import settings as uds

    monkeypatch.setattr(uds, "get_user_working_directory", lambda: str(tmp_path))
    env.svc.runtimes["alice"].root = None  # e.g. the feature is switched off, index kept
    resp = _call("/api/documentation-search/files/{fid}/raw", "GET", path_params={"fid": env.law["cite_id"]})
    assert resp.status_code == 200
    # A saved root that no longer validates is not served from.
    env.storage.upsert_source("alice", "local", root_path=str(env.sysdir))
    resp = _call("/api/documentation-search/files/{fid}/raw", "GET", path_params={"fid": env.law["cite_id"]})
    assert resp.status_code == 409 and _json(resp)["error"] == "RootUnavailable"


# ── thumbnail ──────────────────────────────────────────────────────────────


def _photo(env):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 30, 30)).save(buf, "PNG")
    (env.root / "Notes" / "dog.png").write_bytes(buf.getvalue())
    return env.db.insert_file("local", "Notes/dog.png", "h-dog", name="dog.png", kind="image",
                              status="indexed", sha256="ab" * 32)


def _listing(*dirs) -> set[str]:
    out: set[str] = set()
    for d in dirs:
        for base, _dirs, files in os.walk(d):
            out.update(os.path.join(base, f) for f in files)
    return out


def test_thumbnail_is_made_in_memory_and_cached(env, monkeypatch):
    pytest.importorskip("PIL")
    rec = _photo(env)
    before = _listing(env.root, env.sysdir)

    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        if any(m in mode for m in "wax+"):
            raise AssertionError(f"thumbnail generation opened {file!r} for writing")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(tempfile, "mkstemp", lambda *a, **k: (_ for _ in ()).throw(AssertionError("mkstemp")))
    resp = _call("/api/documentation-search/files/{fid}/thumbnail", "GET", path_params={"fid": rec["cite_id"]},
                 query="size=128")
    monkeypatch.setattr(builtins, "open", real_open)

    assert resp.status_code == 200
    assert resp.media_type == "image/jpeg"
    assert resp.body[:2] == b"\xff\xd8"
    from PIL import Image
    with Image.open(io.BytesIO(resp.body)) as im:
        assert max(im.size) == 128
    assert _listing(env.root, env.sysdir) == before

    # Cached: a second request does not touch the disk at all.
    (env.root / "Notes" / "dog.png").unlink()
    again = _call("/api/documentation-search/files/{fid}/thumbnail", "GET", path_params={"fid": rec["cite_id"]},
                  query="size=128")
    assert again.status_code == 200 and again.body == resp.body


def test_thumbnail_only_for_images(env):
    resp = _call("/api/documentation-search/files/{fid}/thumbnail", "GET", path_params={"fid": env.law["cite_id"]})
    assert resp.status_code == 415
