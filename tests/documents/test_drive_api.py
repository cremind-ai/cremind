"""API: Google Drive as a Documentation search source.

What these pin, in order of how much damage a regression would do:

- **Turning Drive off deletes the Drive index.** A confirmed disable asks the
  engine to purge set D (the PR1 bug: a confirmed disable purged nothing);
  turning the *local* folder off never touches Drive.
- **A whole-Drive account names its folders first.** ``drive`` or
  ``drive.readonly`` access with no ``include_folders`` is refused with 409
  ``DriveFoldersRequired`` — one switch must never index an entire Drive.
- **Drive bytes are the caller's own and capped.** The viewer proxies a Drive
  file through the caller's link only, up to 20 MB; anything else is the
  409 ``DriveFile`` with the web link. Rows of a hidden Drive (revoked or
  unlinked) are 404 like a file that never existed.
- **Profiles stay apart.** Every Drive call is made with the caller's profile,
  never one named in the request.

The Drive client (builder A) and the engine (B1/B2) are faked: the routes are
driven directly, with the client replaced at the ``_drive_client`` seam.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402
from starlette.datastructures import QueryParams  # noqa: E402

import app.drive.skill_token as drive_token  # noqa: E402
import app.storage as storage_pkg  # noqa: E402
import app.storage.documents_storage as uds_storage_module  # noqa: E402
from app.api import documents as api  # noqa: E402
from app.api import documents_files as files_api  # noqa: E402
from app.config.settings import BaseConfig  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.documents_storage import DocumentsStorage  # noqa: E402
from app.documents import settings as uds  # noqa: E402
from app.documents import state as uds_state  # noqa: E402
from tests.documents._workspaces import install as install_working_dirs  # noqa: E402

WHOLE = "https://www.googleapis.com/auth/drive"
READONLY = "https://www.googleapis.com/auth/drive.readonly"
PER_FILE = "https://www.googleapis.com/auth/drive.file"


class _Req:
    def __init__(self, username="dog", body=None, query="", path_params=None, authenticated=True):
        self.user = SimpleNamespace(is_authenticated=authenticated, username=username)
        self.query_params = QueryParams(query)
        self.path_params = path_params or {}
        self._body = body or {}

    async def json(self):
        return self._body


def _body(resp) -> dict:
    return json.loads(resp.body)


def _route(routes, path: str, method: str):
    for r in routes:
        if r.path == path and method in (r.methods or ()):
            return r.endpoint
    raise AssertionError(f"no route {method} {path}")


def _call(path, method, **req):
    return asyncio.run(_route(api.get_documents_routes(), path, method)(_Req(**req)))


class DriveErr(Exception):
    """Stands in for the Drive client's DriveError: all the API reads is ``kind``."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


# ── settings: enable / disable ─────────────────────────────────────────────


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "ud.db"))
    eng = provider.sync_engine()
    for name in ("profiles", "document_sources", "document_captions", "document_vision_usage"):
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    now = time.time() * 1000
    with eng.begin() as c:
        for i, name in enumerate(("admin", "dog", "cat")):
            c.execute(text(
                "INSERT INTO profiles (id,name,created_at,updated_at) "
                f"VALUES ('p{i}','{name}',:n,:n)"
            ), {"n": now})
    storage = DocumentsStorage(provider)
    monkeypatch.setattr(uds_storage_module, "_instance", storage)

    install_working_dirs(monkeypatch, tmp_path / "system", {"admin": None, "dog": None, "cat": None})
    rows: dict[str, str] = {"documentation_search.allowed": "true"}
    monkeypatch.setattr(uds, "get_dynamic", lambda table, key, *a, **k: rows.get(key))
    monkeypatch.setattr(storage_pkg, "get_dynamic_config_storage", lambda *a, **k: None)
    monkeypatch.setattr(BaseConfig, "is_embedding_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(api, "_feature_missing", lambda: [])
    monkeypatch.setattr(api, "_drive_view", lambda profile: {"linked": True})
    monkeypatch.setattr(api, "_TOKEN_REREAD_S", 0)

    # Each profile's gdrive token, as the skill would have written it.
    tokens: dict[str, dict[str, Any] | None] = {
        "dog": {"scopes": ["openid", "email", PER_FILE], "email": "dog@example.com"},
        "cat": {"scopes": ["openid", "email", WHOLE], "email": "cat@example.com"},
    }
    monkeypatch.setattr(drive_token, "read_token", lambda profile: tokens.get(profile))
    monkeypatch.setattr(drive_token, "skill_dir", lambda profile: tmp_path if profile in tokens else None)

    purged: list[tuple[str, str]] = []
    uds_state.set_purge_handler(lambda p, k: purged.append((p, k)))
    yield SimpleNamespace(storage=storage, tokens=tokens, purged=purged, monkeypatch=monkeypatch)
    uds_state.set_purge_handler(None)


def _put(profile: str, body: dict[str, Any]):
    return _call("/api/documentation-search/settings", "PUT", username=profile, body=body)


def _put_confirmed(profile: str, body: dict[str, Any]):
    """PUT, answering a confirmation if the plan asks for one — for tests that
    are not about the confirmation itself."""
    resp = _put(profile, body)
    if resp.status_code == 409 and _body(resp).get("error") == "ConfirmationRequired":
        resp = _put(profile, {**body, "confirm": _body(resp)["confirm"]})
    return resp


def test_a_whole_drive_account_must_choose_folders_first(env):
    resp = _put("cat", {"kind": "drive", "enabled": True})
    assert resp.status_code == 409
    body = _body(resp)
    assert body["error"] == "DriveFoldersRequired"
    assert body["whole_drive"] is True
    assert "folder" in body["message"]
    assert env.storage.get_source("cat", "drive") is None

    ok = _put_confirmed("cat", {"kind": "drive", "enabled": True, "options": {"include_folders": ["F1a-_x"]}})
    assert ok.status_code == 200, ok.body
    row = env.storage.get_source("cat", "drive")
    assert row["enabled"] is True
    assert uds.normalize_options(row["options"])["include_folders"] == ["F1a-_x"]


def test_drive_readonly_counts_as_whole_drive(env):
    env.tokens["cat"] = {"scopes": [READONLY]}
    assert _body(_put("cat", {"kind": "drive", "enabled": True}))["error"] == "DriveFoldersRequired"


def test_a_per_file_account_needs_no_folders(env):
    resp = _put_confirmed("dog", {"kind": "drive", "enabled": True})
    assert resp.status_code == 200, resp.body
    assert env.storage.get_source("dog", "drive")["enabled"] is True


def test_clearing_the_folders_of_an_enabled_whole_drive_source_is_refused(env):
    _put_confirmed("cat", {"kind": "drive", "enabled": True, "options": {"include_folders": ["F1"]}})
    resp = _put("cat", {"kind": "drive", "options": {"include_folders": []}})
    assert resp.status_code == 409
    assert _body(resp)["error"] == "DriveFoldersRequired"
    row = env.storage.get_source("cat", "drive")
    assert uds.normalize_options(row["options"])["include_folders"] == ["F1"]


def test_folder_ids_are_validated(env):
    resp = _put("dog", {"kind": "drive", "enabled": True,
                        "options": {"include_folders": ["ok_id", "x' or name contains 'y"]}})
    assert resp.status_code == 400
    assert "include_folders" in _body(resp)["details"]


def test_drive_needs_a_linked_account_and_rereads_a_mid_swap_token(env):
    env.tokens.pop("dog")
    resp = _put("dog", {"kind": "drive", "enabled": True})
    assert resp.status_code == 409
    assert _body(resp)["error"] == "DriveNotLinked"

    # The skill replaces its token file atomically: the first read can land
    # in the swap and see nothing. One re-read decides.
    reads = iter([None, {"scopes": [PER_FILE]}])
    env.monkeypatch.setattr(drive_token, "read_token", lambda profile: next(reads))
    env.monkeypatch.setattr(drive_token, "skill_dir", lambda profile: Path("."))
    assert api._drive_linked("dog") is True


def test_no_second_read_when_the_skill_is_not_installed(env):
    calls: list[str] = []

    def read(profile):
        calls.append(profile)
        return None

    env.monkeypatch.setattr(drive_token, "read_token", read)
    env.monkeypatch.setattr(drive_token, "skill_dir", lambda profile: None)
    assert api._drive_linked("dog") is False
    assert calls == ["dog"]


def test_a_confirmed_drive_disable_purges_the_drive_index(env):
    _put_confirmed("dog", {"kind": "drive", "enabled": True})
    first = _put("dog", {"kind": "drive", "enabled": False})
    assert first.status_code == 409
    body = _body(first)
    assert body["error"] == "ConfirmationRequired"
    assert [e["kind"] for e in body["plan"]["effects"]] == ["purge_drive"]
    assert env.purged == []  # nothing goes before the confirmation

    done = _put("dog", {"kind": "drive", "enabled": False, "confirm": body["confirm"]})
    assert done.status_code == 200, done.body
    assert env.storage.get_source("dog", "drive")["enabled"] is False
    assert env.purged == [("dog", "drive")]


def test_disabling_drive_purges_even_when_the_plan_had_nothing_to_confirm(env):
    """With real counts, an empty Drive index needs no dialog — but the switch
    still means "delete the Drive index" (its cursor, identity, folders)."""
    _put_confirmed("dog", {"kind": "drive", "enabled": True})
    uds.set_effect_counter(lambda profile, kind, what, ctx: uds.Effect(what, files=0))
    try:
        resp = _put("dog", {"kind": "drive", "enabled": False})
    finally:
        uds.set_effect_counter(None)
    assert resp.status_code == 200, resp.body
    assert env.purged == [("dog", "drive")]


def test_drive_already_off_and_local_off_never_purge_drive(env):
    assert _put("dog", {"kind": "drive", "enabled": False}).status_code == 200
    _put_confirmed("dog", {"enabled": True})
    _put_confirmed("dog", {"kind": "drive", "enabled": True})
    assert _put("dog", {"enabled": False}).status_code == 200  # the local folder, index kept
    assert env.purged == []


def test_a_drive_disable_purges_only_the_callers_profile(env):
    env.tokens["cat"] = {"scopes": [PER_FILE]}
    for p in ("dog", "cat"):
        _put_confirmed(p, {"kind": "drive", "enabled": True})
    _put_confirmed("dog", {"kind": "drive", "enabled": False})
    assert env.purged == [("dog", "drive")]
    assert env.storage.get_source("cat", "drive")["enabled"] is True


# ── the settings view ──────────────────────────────────────────────────────


def test_drive_view_reports_access_staleness_and_the_index(monkeypatch):
    monkeypatch.setattr(drive_token, "status", lambda profile: {
        "linked": True, "email": "new@example.com", "scopes": [READONLY],
        "scopes_stale": True, "whole_drive": False, "access_model": "per-file (…)",
    })
    index = {"enabled": True, "state": "hold", "reason": "drive_unreachable",
             "account_email": "old@example.com", "counts": {"indexed": 3}}
    monkeypatch.setattr(uds_state, "build_snapshot", lambda profile: {"drive": index})
    view = api._drive_view("dog")
    assert view["linked"] is True
    assert view["whole_drive"] is True  # drive.readonly lists the whole Drive too
    assert view["scopes_stale"] is True
    assert view["identity_email"] == "old@example.com"
    assert view["index"] == index


def test_drive_view_survives_a_broken_status(monkeypatch):
    def boom(profile):
        raise RuntimeError("broker down")

    monkeypatch.setattr(drive_token, "status", boom)
    monkeypatch.setattr(uds_state, "build_snapshot", lambda profile: {})
    view = api._drive_view("dog")
    assert view["linked"] is False and view["index"] is None


# ── control and file listing parameters ────────────────────────────────────


class _Svc:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def control(self, profile, action, **kw):
        self.calls.append((profile, action, kw))
        return {"accepted": True}

    def list_files(self, profile, **kw):
        self.calls.append((profile, "list_files", kw))
        return {"files": [], "next": None, "counts": {}}


@pytest.fixture
def svc(monkeypatch):
    import app.documents.service as service_module

    fake = _Svc()
    monkeypatch.setattr(service_module, "_service", fake)
    return fake


def test_control_passes_source_and_drops_everything_else(svc):
    resp = _call("/api/documentation-search/control", "POST", username="dog",
                 body={"action": "sync_now", "source": "drive", "profile": "cat"})
    assert resp.status_code == 202
    assert svc.calls == [("dog", "sync_now", {"source": "drive"})]

    bad = _call("/api/documentation-search/control", "POST", body={"action": "rescan", "source": "dropbox"})
    assert bad.status_code == 400
    assert len(svc.calls) == 1


@pytest.mark.parametrize("query,expected", [
    ("source=drive", "drive"), ("source=local", "local"), ("source=all", None), ("", None),
])
def test_files_source_filter(svc, query, expected):
    resp = _call("/api/documentation-search/files", "GET", query=query)
    assert resp.status_code == 200
    assert svc.calls[-1][2]["source"] == expected


def test_files_rejects_an_unknown_source(svc):
    assert _call("/api/documentation-search/files", "GET", query="source=dropbox").status_code == 400
    assert svc.calls == []


# ── the Drive folder picker ────────────────────────────────────────────────


class FakeClient:
    """The two calls the picker makes, recorded, with the caller's profile."""

    def __init__(self, profile: str, *, folders=None, names=None, error=None):
        self.profile = profile
        self.folders = folders if folders is not None else []
        self.names = names or {}
        self.error = error
        self.calls: list[tuple] = []
        self.closed = False

    def get(self, file_id):
        self.calls.append(("get", file_id))
        if self.error is not None:
            raise self.error
        return {"id": file_id, "name": self.names.get(file_id, file_id)}

    def list_child_folders(self, parent_id):
        self.calls.append(("list", parent_id))
        if self.error is not None:
            raise self.error
        return self.folders

    def close(self):
        self.closed = True


@pytest.fixture
def picker(monkeypatch):
    tokens: dict[str, Any] = {
        "dog": {"scopes": [PER_FILE]},
        "cat": {"scopes": [WHOLE]},
    }
    made: list[FakeClient] = []
    spec: dict[str, Any] = {"folders": [], "names": {}, "error": None}

    def factory(profile):
        c = FakeClient(profile, folders=spec["folders"], names=spec["names"], error=spec["error"])
        made.append(c)
        return c

    monkeypatch.setattr(drive_token, "read_token", lambda profile: tokens.get(profile))
    monkeypatch.setattr(drive_token, "skill_dir", lambda profile: None)
    monkeypatch.setattr(api, "_drive_client", factory)
    return SimpleNamespace(tokens=tokens, made=made, spec=spec)


def _folders(profile="dog", query=""):
    return _call("/api/documentation-search/drive/folders", "GET", username=profile, query=query)


def test_folders_top_level_sorted_with_the_access_model(picker):
    picker.spec["folders"] = [
        {"id": "b1", "name": "beta", "mimeType": "application/vnd.google-apps.folder"},
        {"id": "a1", "name": "Alpha"},
        {"name": "no id — dropped"},
    ]
    resp = _folders("cat")
    assert resp.status_code == 200
    body = _body(resp)
    assert body == {
        "folders": [{"id": "a1", "name": "Alpha"}, {"id": "b1", "name": "beta"}],
        "parent": None,
        "whole_drive": True,
    }
    assert picker.made[0].calls == [("list", None)]
    assert picker.made[0].closed


def test_folders_under_a_parent_name_it(picker):
    picker.spec["names"] = {"P9": "Reports"}
    body = _body(_folders("dog", "parent=P9"))
    assert body["parent"] == {"id": "P9", "name": "Reports"}
    assert body["whole_drive"] is False
    assert picker.made[0].calls == [("get", "P9"), ("list", "P9")]


def test_folders_use_the_callers_profile_only(picker):
    _folders("dog", "profile=cat")
    assert [c.profile for c in picker.made] == ["dog"]


def test_folders_refuse_a_bad_parent_and_an_unlinked_profile(picker):
    assert _folders("dog", "parent=x'%20or%201=1").status_code == 400
    resp = _folders("nobody")
    assert resp.status_code == 409 and _body(resp)["error"] == "DriveNotLinked"
    assert picker.made == []


@pytest.mark.parametrize("kind,status,error", [
    ("auth_revoked", 409, "DriveNotLinked"),
    ("unlinked", 409, "DriveNotLinked"),
    ("not_found", 404, "NotFound"),
    ("unreachable", 503, "DriveUnreachable"),
    ("rate_limited", 503, "DriveUnreachable"),
])
def test_folder_listing_errors(picker, kind, status, error):
    picker.spec["error"] = DriveErr(kind)
    resp = _folders("dog", "parent=P1")
    assert resp.status_code == status
    assert _body(resp)["error"] == error
    assert _body(resp)["reason"] == kind
    assert picker.made[0].closed


def test_a_transport_failure_is_unreachable(picker):
    picker.spec["error"] = OSError("connection reset")
    resp = _folders("dog")
    assert resp.status_code == 503 and _body(resp)["error"] == "DriveUnreachable"


# ── viewing Drive files: raw bytes and thumbnails ──────────────────────────


@pytest.fixture
def files_env(tmp_path, monkeypatch):
    from tests.documents._citations_env import build, close

    e = build(tmp_path, monkeypatch)
    files_api.clear_thumbnail_cache()
    made: list[SimpleNamespace] = []
    spec: dict[str, Any] = {"meta": {}, "content": None, "error": None}

    def factory(profile):
        calls: list[tuple] = []

        def get(file_id):
            calls.append(("get", file_id))
            return {"id": file_id, "mimeType": "application/pdf", **spec["meta"]}

        def fetch_content(meta, *, max_bytes):
            calls.append(("fetch", meta["id"], max_bytes))
            if spec["error"] is not None:
                raise spec["error"]
            return spec["content"]

        client = SimpleNamespace(profile=profile, calls=calls, get=get, fetch_content=fetch_content,
                                 close=lambda: calls.append(("close",)))
        made.append(client)
        return client

    monkeypatch.setattr(files_api, "_drive_client", factory)
    e.made, e.spec = made, spec
    yield e
    files_api.clear_thumbnail_cache()
    close(e)


def _content(data: bytes, ext: str = "", export_mime: str | None = None):
    return SimpleNamespace(data=data, ext=ext, export_mime=export_mime, kind=None)


def _drive_row(env, rel: str, name: str, **fields):
    defaults = {"kind": "pdf", "status": "indexed", "drive_file_id": "FILE1",
                "drive_web_link": "https://drive.google.com/file/d/FILE1/view"}
    return env.db.insert_file("drive", rel, f"h-{rel}", name=name, **{**defaults, **fields})


def _files(path: str, fid: str, *, username="alice", query=""):
    return asyncio.run(_route(files_api.get_documents_files_routes(), path, "GET")(
        _Req(username=username, path_params={"fid": fid}, query=query)))


def test_raw_proxies_a_drive_file_through_the_callers_link(files_env):
    rec = _drive_row(files_env, "Drive/Reports/plan.pdf", "plan.pdf", mime="application/pdf", size=13)
    files_env.spec["content"] = _content(b"%PDF-1.4 drive")
    resp = _files("/api/documentation-search/files/{fid}/raw", rec["cite_id"])
    assert resp.status_code == 200
    assert resp.body == b"%PDF-1.4 drive"
    assert resp.media_type == "application/pdf"
    assert resp.headers["content-disposition"] == 'inline; filename="plan.pdf"'
    assert resp.headers["x-content-type-options"] == "nosniff"
    (client,) = files_env.made
    assert client.profile == "alice"
    assert client.calls == [("get", "FILE1"), ("fetch", "FILE1", files_api.DRIVE_PROXY_MAX_BYTES), ("close",)]


def test_a_google_doc_is_served_as_the_export_it_was_indexed_from(files_env):
    rec = _drive_row(files_env, "Drive/Notes/Plan", "Plan", kind="markdown",
                     drive_mime="application/vnd.google-apps.document")
    files_env.spec["meta"] = {"mimeType": "application/vnd.google-apps.document"}
    files_env.spec["content"] = _content(b"# Plan\n", ".md", "text/markdown")
    resp = _files("/api/documentation-search/files/{fid}/raw", rec["cite_id"])
    assert resp.status_code == 200
    assert resp.media_type == "text/markdown"
    assert resp.headers["content-disposition"] == 'inline; filename="Plan.md"'


def test_active_drive_content_is_never_inline(files_env):
    rec = _drive_row(files_env, "Drive/page.html", "page.html", kind="html", mime="text/html")
    files_env.spec["content"] = _content(b"<script>alert(1)</script>")
    resp = _files("/api/documentation-search/files/{fid}/raw", rec["cite_id"])
    assert resp.status_code == 200
    assert resp.media_type == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in resp.headers["content-security-policy"]


def test_a_non_ascii_drive_name_uses_the_rfc5987_form(files_env):
    rec = _drive_row(files_env, "Drive/luật.pdf", "luật.pdf")
    files_env.spec["content"] = _content(b"%PDF")
    resp = _files("/api/documentation-search/files/{fid}/raw", rec["cite_id"])
    assert resp.headers["content-disposition"] == "inline; filename*=utf-8''lu%E1%BA%ADt.pdf"


@pytest.mark.parametrize("fields,meta,reason", [
    ({"size": files_api.DRIVE_PROXY_MAX_BYTES + 1}, {}, "too_large"),  # known from the index
    ({}, {"size": str(files_api.DRIVE_PROXY_MAX_BYTES + 1)}, "too_large"),  # grown since
    ({"status": "metadata_only", "status_reason": "not_downloadable"}, {}, "not_downloadable"),
])
def test_large_or_undownloadable_drive_files_open_in_drive(files_env, fields, meta, reason):
    rec = _drive_row(files_env, "Drive/big.pdf", "big.pdf", **fields)
    files_env.spec["meta"] = meta
    resp = _files("/api/documentation-search/files/{fid}/raw", rec["cite_id"])
    assert resp.status_code == 409
    assert _body(resp) == {
        "error": "DriveFile", "message": "This file lives in Google Drive; open it there.",
        "web_link": "https://drive.google.com/file/d/FILE1/view", "reason": reason,
    }
    # No bytes were pulled for a file that was never going to be served.
    assert not any(c[0] == "fetch" for m in files_env.made for c in m.calls)


@pytest.mark.parametrize("error,content,reason", [
    (DriveErr("not_downloadable"), None, "not_downloadable"),
    (DriveErr("unreachable"), None, "unreachable"),
    (DriveErr("too_large"), None, "too_large"),
    (RuntimeError("boom"), None, "unavailable"),
    (None, None, "no_content"),
])
def test_drive_fetch_failures_fall_back_to_the_web_link(files_env, error, content, reason):
    rec = _drive_row(files_env, "Drive/x.pdf", "x.pdf")
    files_env.spec["error"], files_env.spec["content"] = error, content
    resp = _files("/api/documentation-search/files/{fid}/raw", rec["cite_id"])
    assert resp.status_code == 409
    assert _body(resp)["error"] == "DriveFile" and _body(resp)["reason"] == reason
    assert files_env.made[0].calls[-1] == ("close",)


def test_a_drive_image_thumbnail_is_made_from_the_fetched_bytes(files_env):
    pytest.importorskip("PIL")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (900, 600), (10, 120, 200)).save(buf, "PNG")
    rec = _drive_row(files_env, "Drive/Photos/sea.png", "sea.png", kind="image", mime="image/png",
                     drive_md5="md5-1")
    files_env.spec["content"] = _content(buf.getvalue())
    resp = _files("/api/documentation-search/files/{fid}/thumbnail", rec["cite_id"], query="size=90")
    assert resp.status_code == 200 and resp.media_type == "image/jpeg"
    with Image.open(io.BytesIO(resp.body)) as im:
        assert max(im.size) == 90
    # Cached per (profile, file, version, size): the second view asks Google nothing.
    again = _files("/api/documentation-search/files/{fid}/thumbnail", rec["cite_id"], query="size=90")
    assert again.status_code == 200 and again.body == resp.body
    assert len(files_env.made) == 1


def test_a_hidden_drive_is_not_viewable(files_env):
    rec = _drive_row(files_env, "Drive/secret.pdf", "secret.pdf")
    folder_id = files_env.db.upsert_folder("drive", "Drive/Reports", "hf-drive-reports", name="Reports",
                                           drive_id="FOLDER1")
    folder = files_env.db.get_folder(folder_id)
    files_env.db.update_source_state("drive", state="hold", reason="auth_revoked")
    for path in ("/api/documentation-search/files/{fid}/raw", "/api/documentation-search/files/{fid}/text"):
        resp = _files(path, rec["cite_id"])
        assert resp.status_code == 404, path
        assert _body(resp)["error"] == "NotFound"
    assert _files("/api/documentation-search/files/{fid}/text", folder["cite_id"]).status_code == 404
    assert files_env.made == []
    # Local files stay viewable while Drive is held.
    assert _files("/api/documentation-search/files/{fid}/text", files_env.law["cite_id"]).status_code == 200

    files_env.db.update_source_state("drive", state="live", reason=None)
    files_env.spec["content"] = _content(b"%PDF")
    assert _files("/api/documentation-search/files/{fid}/raw", rec["cite_id"]).status_code == 200


def test_another_profile_cannot_reach_a_drive_file(files_env):
    rec = _drive_row(files_env, "Drive/plan.pdf", "plan.pdf")
    for path in ("/api/documentation-search/files/{fid}/raw", "/api/documentation-search/files/{fid}/thumbnail"):
        resp = _files(path, rec["cite_id"], username="bob")
        assert resp.status_code == 404, path
    assert files_env.made == []
