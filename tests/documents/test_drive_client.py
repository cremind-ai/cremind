"""The Drive client against a fake Drive (httpx.MockTransport).

The classification of failures is the point: an ``invalid_grant`` means the
user revoked access (hide, then purge after a grace period), but a network
outage must never look like that — an offline laptop keeps its Drive index.
Just as important is what must never look like "this file is gone": a file
whose downloads are restricted, a shortcut, a Google-native type with no
export, and every 403 that is really about the account.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest

import app.drive.skill_token as st
from app.documents.sources import drive_client as dc
from app.documents.sources.drive_client import (
    EXPORTS,
    FIELDS,
    KINDS,
    ChangePage,
    Content,
    DriveClient,
    DriveError,
    Listing,
    classify_token_error,
    parse_retry_after,
)

API_PATH = "/drive/v3"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
FOLDER = "application/vnd.google-apps.folder"


def _client(handler, *, token="tok", stop_event=None, real_sleep=False):
    """A client over ``handler``; returns (client, token calls, sleeps).
    ``token`` is a string, an exception to raise, or ``fn(force) -> str``."""
    forced: list[bool] = []
    sleeps: list[float] = []

    def token_fn(profile, force_refresh=False):
        assert profile == "alice"
        forced.append(force_refresh)
        if isinstance(token, BaseException):
            raise token
        return token(force_refresh) if callable(token) else token

    client = DriveClient(
        "alice",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        token_fn=token_fn,
        stop_event=stop_event,
        sleep=None if real_sleep else sleeps.append,
    )
    return client, forced, sleeps


def _json(data, status=200, headers=None):
    return httpx.Response(status, content=json.dumps(data).encode(), headers=headers or {})


def _err(status, reason=None, *, location=None, details=None, headers=None):
    error: dict = {"code": status, "message": f"reason {reason}"}
    if reason:
        entry = {"reason": reason, "domain": "global", "message": "x"}
        if location:
            entry["location"] = location
        error["errors"] = [entry]
    if details:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": d} for d in details]
    return _json({"error": error}, status=status, headers=headers)


def _kind(fn):
    with pytest.raises(DriveError) as exc:
        fn()
    return exc.value.kind


def test_every_kind_the_client_raises_is_declared():
    assert {"auth_revoked", "auth_failed", "auth_misconfigured", "unlinked", "unreachable",
            "account_forbidden", "rate_limited", "bad_cursor", "not_found", "not_authorized",
            "not_downloadable", "file_unavailable", "too_large", "export_too_large", "http"} == set(KINDS)
    err = DriveError("rate_limited")
    assert (err.kind, err.status, err.reason, str(err)) == ("rate_limited", None, None, "rate_limited")


def test_fields_carry_checksums_extensions_and_rotation():
    for name in ("sha256Checksum", "md5Checksum", "fileExtension", "fullFileExtension", "rotation",
                 "capabilities(canDownload)", "shortcutDetails(targetId,targetMimeType)"):
        assert name in FIELDS


# ── Listing ────────────────────────────────────────────────────────────────


def test_listing_pages_past_a_thousand_files_and_is_complete():
    pages = {None: ([{"id": f"a{i}"} for i in range(1000)], "p2"), "p2": ([{"id": "b1"}, {"id": "b2"}], None)}
    seen_fields = []

    def handler(req):
        seen_fields.append(req.url.params["fields"])
        files, nxt = pages[req.url.params.get("pageToken")]
        return _json({"files": files, **({"nextPageToken": nxt} if nxt else {})})

    client, _, _ = _client(handler)
    listing = client.list_all()
    assert isinstance(listing, Listing)
    assert len(listing.files) == 1002 and listing.files[-1]["id"] == "b2"
    assert listing.complete is True and listing.reason is None
    assert all("incompleteSearch" in f and "sha256Checksum" in f for f in seen_fields)


def test_incomplete_search_makes_the_listing_incomplete():
    def handler(req):
        return _json({"files": [{"id": "a"}], "incompleteSearch": True})

    client, _, _ = _client(handler)
    listing = client.list_all()
    assert [f["id"] for f in listing.files] == ["a"]
    assert (listing.complete, listing.reason) == (False, "incomplete_search")


def test_truncation_is_reported_but_an_exact_fit_is_complete():
    def handler(req):
        if req.url.params.get("pageToken") == "p2":
            return _json({"files": [{"id": "c"}]})
        return _json({"files": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"})

    client, _, _ = _client(handler)
    cut = client.list_all(max_files=2)
    assert [f["id"] for f in cut.files] == ["a", "b"]
    assert (cut.complete, cut.reason) == (False, "truncated")
    whole = client.list_all(max_files=3)
    assert len(whole.files) == 3 and whole.complete is True


def test_folder_tree_walks_every_level_in_batches():
    # root R holds 45 subfolders (two batched queries) and a file; sub f0 holds
    # a nested file; "multi" sits under two folders and is listed once.
    subs = [{"id": f"f{i}", "name": f"Sub {i}", "mimeType": FOLDER, "parents": ["R"]} for i in range(45)]
    queries = []

    def handler(req):
        path = req.url.path
        if path == f"{API_PATH}/files/R":
            return _json({"id": "R", "name": "Root", "mimeType": FOLDER, "trashed": False})
        assert path == f"{API_PATH}/files", path
        q = req.url.params["q"]
        queries.append(q)
        assert q.endswith("and trashed = false")
        files = []
        if "'R' in parents" in q:
            files += subs + [{"id": "top", "name": "a.pdf", "mimeType": "application/pdf", "parents": ["R"]}]
        if "'f0' in parents" in q:
            files += [{"id": "deep", "name": "b.txt", "mimeType": "text/plain", "parents": ["f0"]},
                      {"id": "multi", "name": "m.txt", "mimeType": "text/plain", "parents": ["f0", "f1"]}]
        if "'f1' in parents" in q:
            files += [{"id": "multi", "name": "m.txt", "mimeType": "text/plain", "parents": ["f0", "f1"]}]
        return _json({"files": files})

    client, _, _ = _client(handler)
    listing = client.list_folders_tree(["R"])
    ids = [f["id"] for f in listing.files]
    assert ids[0] == "R"  # the include folder itself is part of the listing
    assert {"top", "deep", "multi"} <= set(ids) and ids.count("multi") == 1
    assert len(ids) == 1 + 45 + 1 + 2
    assert listing.complete is True
    # 45 subfolders do not fit one query: batched "'a' in parents or 'b' in parents".
    level_two = [q for q in queries if "'f0' in parents" in q or "'f44' in parents" in q]
    assert len(level_two) == 2
    assert all(q.count(" in parents") <= 40 for q in queries)


def test_a_missing_include_folder_makes_the_listing_incomplete():
    def handler(req):
        if req.url.path.endswith("/files/gone"):
            return _err(404, "notFound")
        if req.url.path.endswith("/files/ok"):
            return _json({"id": "ok", "name": "Ok", "mimeType": FOLDER})
        if req.url.path.endswith("/files/binned"):
            return _json({"id": "binned", "name": "Bin", "mimeType": FOLDER, "trashed": True})
        return _json({"files": [{"id": "x", "name": "x.txt", "mimeType": "text/plain", "parents": ["ok"]}]})

    client, _, _ = _client(handler)
    listing = client.list_folders_tree(["gone", "ok", "binned"])
    assert [f["id"] for f in listing.files] == ["ok", "x"]  # a trashed root lists nothing
    assert (listing.complete, listing.reason, listing.missing) == (False, "folder_missing", ["gone"])


def test_folder_ids_are_validated_before_any_request():
    calls = []
    client, _, _ = _client(lambda req: calls.append(req) or _json({"files": []}))
    for bad in ("a' or name contains 'x", "a b", "", "../x"):
        with pytest.raises(DriveError) as exc:
            client.list_folders_tree(["ok", bad])
        assert (exc.value.kind, exc.value.reason) == ("http", "invalid_id")
        with pytest.raises(DriveError):
            client.list_child_folders(bad or "a/b")
    with pytest.raises(DriveError):
        client.get("a/b")
    assert calls == []
    assert dc.valid_id("1AbC-_x9") and not dc.valid_id("a'b")


def test_child_folders_for_the_picker(monkeypatch):
    token = {"scopes": ["https://www.googleapis.com/auth/drive.file"], "client_id": "cid"}
    monkeypatch.setattr(st, "read_token", lambda profile: token)
    queries = []
    folders = [
        {"id": "A", "name": "beta", "mimeType": FOLDER, "parents": ["unseen"]},
        {"id": "B", "name": "Alpha", "mimeType": FOLDER, "parents": ["A"]},
        {"id": "C", "name": "gamma", "mimeType": FOLDER},
    ]

    def handler(req):
        q = req.url.params["q"]
        queries.append(q)
        if "in parents" in q:
            return _json({"files": [folders[1]]})
        return _json({"files": folders})

    client, _, _ = _client(handler)
    # Per-file: the tops of what the grant can see (B's parent A is visible).
    assert [f["id"] for f in client.list_child_folders(None)] == ["A", "C"]
    assert "in parents" not in queries[-1] and f"mimeType = '{FOLDER}'" in queries[-1]
    assert [f["id"] for f in client.list_child_folders("A")] == ["B"]
    assert queries[-1].startswith("'A' in parents and ")
    # Whole-Drive (drive or drive.readonly): the top is My Drive.
    token["scopes"] = ["https://www.googleapis.com/auth/drive.readonly"]
    client.list_child_folders(None)
    assert queries[-1].startswith("'root' in parents and ")


# ── Changes ────────────────────────────────────────────────────────────────


def test_changes_come_a_page_at_a_time_with_resume_tokens():
    def handler(req):
        tok = req.url.params["pageToken"]
        assert "driveId" in req.url.params["fields"] and "changeType" in req.url.params["fields"]
        if tok == "c1":
            return _json({"changes": [{"fileId": "x", "removed": False, "changeType": "file",
                                       "file": {"id": "x", "trashed": True}}], "nextPageToken": "c2"})
        return _json({"changes": [{"fileId": "y", "removed": True, "changeType": "file"},
                                  {"changeType": "drive", "driveId": "sd1", "removed": True},
                                  {"changeType": "drive", "driveId": "sd2", "removed": False}],
                      "newStartPageToken": "c9"})

    client, _, _ = _client(handler)
    pages = list(client.changes("c1"))
    assert all(isinstance(p, ChangePage) for p in pages)
    assert [(p.resume_token, p.last) for p in pages] == [("c2", False), ("c9", True)]
    # A trashed file is a change with its metadata, not a removal.
    assert pages[0].changes == [{"fileId": "x", "removed": False, "file": {"id": "x", "trashed": True}}]
    assert pages[1].changes == [{"fileId": "y", "removed": True, "file": None}]
    assert pages[1].drive_removals == ["sd1"]  # a renamed shared drive is not a removal


def test_a_failure_on_a_later_page_keeps_the_earlier_resume_point():
    def handler(req):
        if req.url.params["pageToken"] == "c1":
            return _json({"changes": [{"fileId": "x", "removed": True}], "nextPageToken": "c2"})
        return httpx.Response(503)

    client, _, _ = _client(handler)
    feed = client.changes("c1")
    first = next(feed)
    assert first.resume_token == "c2" and not first.last
    assert _kind(lambda: next(feed)) == "unreachable"


def test_a_page_with_no_token_resumes_where_it_was():
    client, _, _ = _client(lambda req: _json({"changes": []}))
    pages = list(client.changes("c5"))
    assert [(p.resume_token, p.last) for p in pages] == [("c5", True)]


@pytest.mark.parametrize("response,kind", [
    (lambda: _err(404, "notFound"), "bad_cursor"),
    (lambda: _err(410, "gone"), "bad_cursor"),
    (lambda: _err(400, "invalid", location="pageToken"), "bad_cursor"),
    # A malformed request (a future FIELDS typo) must not trigger a full
    # re-list on every sync forever.
    (lambda: _err(400, "invalid", location="fields"), "http"),
    (lambda: _err(400, "badRequest"), "http"),
])
def test_only_a_rejected_page_token_is_a_bad_cursor(response, kind):
    client, _, _ = _client(lambda req: response())
    assert _kind(lambda: list(client.changes("stale"))) == kind


# ── Tokens and 401s ────────────────────────────────────────────────────────


def test_a_401_forces_a_real_refresh_once():
    seen = []

    def handler(req):
        seen.append(req.headers["Authorization"])
        if req.headers["Authorization"] == "Bearer dead":
            return _err(401, "authError")
        return _json({"startPageToken": "s1"})

    client, forced, _ = _client(handler, token=lambda force: "fresh" if force else "dead")
    assert client.start_page_token() == "s1"
    assert forced == [False, True]
    assert seen == ["Bearer dead", "Bearer fresh"]


def test_a_second_401_after_a_successful_refresh_is_auth_failed():
    client, forced, _ = _client(lambda req: _err(401, "authError"))
    with pytest.raises(DriveError) as exc:
        client.get("f1")
    assert (exc.value.kind, exc.value.status) == ("auth_failed", 401)
    assert forced == [False, True]  # exactly one forced refresh, no loop


@pytest.mark.parametrize("kind", ["auth_revoked", "auth_misconfigured", "unreachable", "unlinked"])
def test_a_failed_forced_refresh_keeps_the_token_errors_kind(kind):
    def token(force):
        if force:
            raise st.DriveTokenError("some message", kind=kind)
        return "dead"

    client, _, _ = _client(lambda req: _err(401, "authError"), token=token)
    assert _kind(client.about) == kind


def test_a_401_refreshes_through_the_real_skill_token(monkeypatch, tmp_path):
    """The default token function skips both the cache and the file's
    still-unexpired token on the retry; before the fix it resent the same
    dead token and the 401 fell through as a generic HTTP error."""
    token_file = {"access_token": "dead", "refresh_token": "rt", "client_id": "cid",
                  "expiry": time.time() + 3000}
    monkeypatch.setattr(st, "read_token", lambda profile: dict(token_file))
    refreshed = []

    def fake_refresh(profile, data):
        refreshed.append(profile)
        st._access_cache[profile] = {"token": "fresh", "expiry": time.time() + 3600}
        return "fresh"

    monkeypatch.setattr(st, "_refresh", fake_refresh)
    st._access_cache.pop("alice", None)

    def handler(req):
        if req.headers["Authorization"] == "Bearer dead":
            return _err(401, "authError")
        return _json({"user": {"permissionId": "p1", "emailAddress": "a@x.com"}})

    client = DriveClient("alice", http=httpx.Client(transport=httpx.MockTransport(handler)),
                         sleep=lambda s: None)
    try:
        assert client.about()["user"]["permissionId"] == "p1"
        assert refreshed == ["alice"]
        assert client.about()["user"]["emailAddress"] == "a@x.com"  # the fresh token is reused
        assert refreshed == ["alice"]
    finally:
        st._access_cache.pop("alice", None)


@pytest.mark.parametrize("message,kind", [
    ("the gdrive skill is not linked to a Google account", "unlinked"),
    ("Google rejected the stored refresh token. Re-link the gdrive skill, then grant files again.", "auth_revoked"),
    ("the linked Google account has no refresh token; re-link the gdrive skill", "auth_revoked"),
    ("could not refresh the Google access token: invalid_client", "auth_misconfigured"),
    ("could not refresh the Google access token: ConnectError", "unreachable"),
])
def test_token_failures_are_classified(message, kind):
    assert classify_token_error(RuntimeError(message)) == kind
    client, _, _ = _client(lambda req: _json({}), token=RuntimeError(message))
    assert _kind(client.start_page_token) == kind


def test_the_token_errors_kind_attribute_wins_over_its_message():
    exc = st.DriveTokenError("could not refresh the Google access token: 401", kind="auth_misconfigured")
    assert classify_token_error(exc) == "auth_misconfigured"


def test_identity_is_client_and_account_from_the_token_file(monkeypatch):
    data = {"client_id": "cid", "account_key": "abc123", "email": "User@Example.com"}
    monkeypatch.setattr(st, "read_token", lambda profile: dict(data) if data else None)
    client, _, _ = _client(lambda req: _json({}))
    assert client.identity() == "cid|abc123"
    # An older token file with no account_key: the skill's own derivation.
    del data["account_key"]
    spec = importlib.util.spec_from_file_location(
        "_gdrive_account_key",
        Path(__file__).resolve().parents[2] / "app/skills/builtin/gdrive/scripts/app/google/account_key.py")
    skill_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(skill_mod)
    assert client.identity() == "cid|" + skill_mod.account_key_for("google", "User@Example.com")
    data.clear()
    assert client.identity() is None


# ── Retries, Retry-After and stopping ──────────────────────────────────────


def test_rate_limits_back_off_then_succeed():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return _err(403, "rateLimitExceeded", headers={"Retry-After": "1"})
        return _json({"startPageToken": "ok"})

    client, _, sleeps = _client(handler)
    assert client.start_page_token() == "ok"
    assert len(sleeps) == 2 and all(1 <= s <= 3 for s in sleeps)


@pytest.mark.parametrize("response", [
    lambda: _err(429, None),
    lambda: _err(403, "userRateLimitExceeded"),
])
def test_retries_exhausted_on_a_rate_limit_is_rate_limited(response):
    client, _, sleeps = _client(lambda req: response())
    assert _kind(client.start_page_token) == "rate_limited"
    assert len(sleeps) == DriveClient.MAX_ATTEMPTS - 1


def test_retry_after_is_capped():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return _err(429, None, headers={"Retry-After": "3600"})
        return _json({"startPageToken": "ok"})

    client, _, sleeps = _client(handler)
    client.start_page_token()
    assert sleeps == [dc.RETRY_AFTER_CAP_S] == [120.0]


def test_retry_after_accepts_an_http_date():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return _err(503, None, headers={"Retry-After": formatdate(time.time() + 30, usegmt=True)})
        return _json({"startPageToken": "ok"})

    client, _, sleeps = _client(handler)
    client.start_page_token()
    assert len(sleeps) == 1 and 27 <= sleeps[0] <= 32


def test_parse_retry_after():
    now = 1_000_000.0
    assert parse_retry_after("7", now=now) == 7.0
    assert parse_retry_after(formatdate(now + 90, usegmt=True), now=now) == pytest.approx(90, abs=1)
    assert parse_retry_after(formatdate(now - 90, usegmt=True), now=now) == 0.0
    assert parse_retry_after("soon", now=now) is None
    assert parse_retry_after(None) is None and parse_retry_after("") is None


def test_a_stop_ends_a_long_backoff_at_once():
    stop = threading.Event()
    client, _, _ = _client(lambda req: _err(503, None, headers={"Retry-After": "100"}),
                           stop_event=stop, real_sleep=True)
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    with pytest.raises(DriveError) as exc:
        client.start_page_token()
    assert time.monotonic() - started < 10
    assert (exc.value.kind, exc.value.reason) == ("unreachable", "stopped")


def test_a_stopped_client_sends_nothing():
    stop = threading.Event()
    stop.set()
    calls = []
    client, _, _ = _client(lambda req: calls.append(req) or _json({}), stop_event=stop)
    assert _kind(client.about) == "unreachable"
    assert calls == []


def test_a_network_outage_is_unreachable_never_revoked():
    def handler(req):
        raise httpx.ConnectError("no route to host")

    client, _, sleeps = _client(handler)
    assert _kind(client.start_page_token) == "unreachable"
    assert len(sleeps) == DriveClient.MAX_ATTEMPTS - 1


def test_5xx_on_metadata_is_unreachable_after_retries():
    client, _, _ = _client(lambda req: httpx.Response(502))
    assert _kind(lambda: client.list_all()) == "unreachable"


def test_a_non_json_200_is_unreachable():
    client, _, _ = _client(lambda req: httpx.Response(200, content=b"<html>captive portal</html>"))
    with pytest.raises(DriveError) as exc:
        client.about()
    assert (exc.value.kind, exc.value.reason) == ("unreachable", "not_json")


# ── 403s: the account, the file, or just its bytes ─────────────────────────


@pytest.mark.parametrize("reason,details", [
    ("dailyLimitExceeded", None),
    ("domainPolicy", None),
    ("accessNotConfigured", ["SERVICE_DISABLED"]),
    ("insufficientPermissions", ["ACCESS_TOKEN_SCOPE_INSUFFICIENT"]),
    (None, ["ACCESS_TOKEN_SCOPE_INSUFFICIENT"]),
])
def test_account_wide_403s_hold_the_source_on_every_endpoint(reason, details):
    calls = []

    def handler(req):
        calls.append(req)
        return _err(403, reason, details=details)

    client, _, sleeps = _client(handler)
    assert _kind(lambda: client.list_all()) == "account_forbidden"
    assert _kind(lambda: client.get("f1")) == "account_forbidden"
    assert _kind(lambda: client.download("f1", max_bytes=100)) == "account_forbidden"
    assert len(calls) == 3 and sleeps == []  # never retried, never per file


def test_per_file_errors():
    def handler(req):
        path = req.url.path
        if "gone" in path:
            return _err(404, "notFound")
        if "revoked" in path:
            return _err(403, "appNotAuthorizedToFile")
        if "odd" in path:
            return _err(403, "somethingNew")
        if "popular" in path:
            return _err(403, "downloadQuotaExceeded")
        return _err(403, "insufficientFilePermissions")

    client, _, _ = _client(handler)
    # Too many downloads of this one file today: retry it later, keep the rest going.
    assert _kind(lambda: client.download("popular", max_bytes=100)) == "file_unavailable"
    assert _kind(lambda: client.download("gone", max_bytes=100)) == "not_found"
    assert _kind(lambda: client.get("gone")) == "not_found"
    assert _kind(lambda: client.get("revoked")) == "not_authorized"
    assert _kind(lambda: client.download("revoked", max_bytes=100)) == "not_authorized"
    # Readable metadata, unreadable bytes: metadata-only, never a purge.
    assert _kind(lambda: client.download("locked", max_bytes=100)) == "not_downloadable"
    # An unknown 403 on files.get is logged, not read as "gone".
    assert _kind(lambda: client.get("odd")) == "http"


@pytest.mark.parametrize("reason", ["fileNotDownloadable", "cannotDownloadAbusiveFile",
                                    "insufficientFilePermissions", "cannotExportFile"])
def test_restricted_content_is_not_downloadable(reason):
    client, _, _ = _client(lambda req: _err(403, reason))
    assert _kind(lambda: client.download("f", max_bytes=100)) == "not_downloadable"
    assert _kind(lambda: client.export("f", "text/plain", max_bytes=100)) == "not_downloadable"


def test_can_download_false_is_checked_before_any_fetch():
    calls = []
    client, _, _ = _client(lambda req: calls.append(req) or httpx.Response(200, content=b"x"))
    for mime in ("application/pdf", DOC, SHEET):
        meta = {"id": "f1", "mimeType": mime, "capabilities": {"canDownload": False}}
        assert _kind(lambda: client.fetch_content(meta, max_bytes=100)) == "not_downloadable"
    assert calls == []


@pytest.mark.parametrize("mime", [
    "application/vnd.google-apps.shortcut", FOLDER, "application/vnd.google-apps.form",
    "application/vnd.google-apps.site", "application/vnd.google-apps.video",
    "application/vnd.google-apps.photo", "application/vnd.google-apps.unknown",
    "application/vnd.google-apps.drive-sdk", "application/vnd.google-apps.mail-layout",
])
def test_shortcuts_and_unknown_native_types_are_never_downloaded(mime):
    calls = []
    client, _, _ = _client(lambda req: calls.append(req) or _err(403, "fileNotDownloadable"))
    meta = {"id": "s1", "mimeType": mime, "shortcutDetails": {"targetId": "t1"}}
    assert client.fetch_content(meta, max_bytes=100) is None
    assert calls == []


# ── Content ────────────────────────────────────────────────────────────────


def test_downloads_are_capped_in_memory():
    client, _, _ = _client(lambda req: httpx.Response(200, content=b"x" * 5000))
    assert client.download("f", max_bytes=10_000) == b"x" * 5000
    assert _kind(lambda: client.download("f", max_bytes=100)) == "too_large"


def test_a_known_size_over_the_cap_is_refused_before_downloading():
    calls = []
    client, _, _ = _client(lambda req: calls.append(req) or httpx.Response(200, content=b"x"))
    meta = {"id": "big", "mimeType": "video/mp4", "size": "5000000000"}
    assert _kind(lambda: client.fetch_content(meta, max_bytes=dc.IN_MEMORY_CAP)) == "too_large"
    assert calls == []


def test_binary_content_keeps_its_own_name_and_kind_detection():
    client, _, _ = _client(lambda req: httpx.Response(200, content=b"%PDF-1.4 body"))
    content = client.fetch_content({"id": "p1", "mimeType": "application/pdf", "size": "13"}, max_bytes=100)
    assert content == Content(data=b"%PDF-1.4 body", ext="", export_mime=None, kind=None)


def test_content_endpoints_get_a_long_read_timeout():
    timeouts = {}

    def handler(req):
        key = "content" if req.url.params.get("alt") == "media" or req.url.path.endswith("/export") else "meta"
        timeouts[key] = req.extensions["timeout"]["read"]
        if key == "content":
            return httpx.Response(200, content=b"x")
        return _json({"id": "f"})

    client, _, _ = _client(handler)  # an injected httpx.Client keeps its own default (5 s)
    client.get("f")
    client.download("f", max_bytes=10)
    client.export("f", "text/plain", max_bytes=10)
    assert timeouts == {"meta": 5.0, "content": 120.0}
    own = DriveClient("alice")
    try:
        assert own._http.timeout.read == 30.0
    finally:
        own.close()


def test_google_docs_are_exported_with_a_fallback():
    tried = []

    def handler(req):
        mime = req.url.params.get("mimeType")
        tried.append(mime)
        if mime == "text/markdown":
            return _err(400, "badRequest")
        return httpx.Response(200, content=b"plain text body")

    client, _, _ = _client(handler)
    content = client.fetch_content({"id": "d1", "mimeType": DOC}, max_bytes=1000)
    assert content == Content(data=b"plain text body", ext=".txt", export_mime="text/plain", kind="text")
    assert tried == ["text/markdown", "text/plain"]
    assert "application/vnd.google-apps.presentation" in EXPORTS


def test_markdown_is_the_first_choice_for_docs():
    client, _, _ = _client(lambda req: httpx.Response(200, content=b"# Title\n"))
    content = client.fetch_content({"id": "d1", "mimeType": DOC}, max_bytes=1000)
    assert (content.ext, content.export_mime, content.kind) == (".md", "text/markdown", "markdown")


def test_a_5xx_on_one_export_format_tries_the_next():
    tried = []

    def handler(req):
        if req.url.path.endswith("/changes/startPageToken"):
            return _json({"startPageToken": "s"})  # the API itself answers
        mime = req.url.params.get("mimeType")
        tried.append(mime)
        if mime == "text/markdown":
            return httpx.Response(503)
        return httpx.Response(200, content=b"body")

    client, _, _ = _client(handler)
    content = client.fetch_content({"id": "d1", "mimeType": DOC}, max_bytes=1000)
    assert content.ext == ".txt"
    assert tried == ["text/markdown"] * DriveClient.CONTENT_ATTEMPTS + ["text/plain"]


def test_a_sheet_over_the_export_cap_falls_back_to_csv():
    def handler(req):
        if req.url.params.get("mimeType") == XLSX:
            return _err(403, "exportSizeLimitExceeded")
        return httpx.Response(200, content=b"a,b\n1,2\n")

    client, _, _ = _client(handler)
    content = client.fetch_content({"id": "s1", "mimeType": SHEET}, max_bytes=1000)
    assert content == Content(data=b"a,b\n1,2\n", ext=".csv", export_mime="text/csv", kind="csv")


def test_every_format_over_googles_export_cap_is_export_too_large():
    client, _, _ = _client(lambda req: _err(403, "exportSizeLimitExceeded"))
    assert _kind(lambda: client.fetch_content({"id": "d1", "mimeType": DOC}, max_bytes=1000)) \
        == "export_too_large"


def test_every_format_over_our_cap_is_too_large():
    client, _, _ = _client(lambda req: httpx.Response(200, content=b"x" * 500))
    assert _kind(lambda: client.fetch_content({"id": "d1", "mimeType": DOC}, max_bytes=100)) == "too_large"


def test_account_failures_during_an_export_stop_the_fallback():
    tried = []

    def handler(req):
        tried.append(req.url.params.get("mimeType"))
        return _err(403, "dailyLimitExceeded")

    client, _, _ = _client(handler)
    assert _kind(lambda: client.fetch_content({"id": "d1", "mimeType": DOC}, max_bytes=100)) \
        == "account_forbidden"
    assert tried == ["text/markdown"]


def test_one_failing_file_is_file_unavailable_while_the_api_answers():
    def handler(req):
        if req.url.path.endswith("/changes/startPageToken"):
            return _json({"startPageToken": "s"})
        raise httpx.ReadTimeout("slow export")

    client, _, sleeps = _client(handler)
    meta = {"id": "p1", "mimeType": "application/pdf"}
    assert _kind(lambda: client.fetch_content(meta, max_bytes=100)) == "file_unavailable"
    assert len(sleeps) == DriveClient.CONTENT_ATTEMPTS - 1


def test_content_failing_while_the_api_is_down_is_unreachable():
    client, _, _ = _client(lambda req: httpx.Response(503))
    assert _kind(lambda: client.download("p1", max_bytes=100)) == "unreachable"


def test_about_and_get_ask_for_what_indexing_needs():
    seen = {}

    def handler(req):
        seen[req.url.path] = dict(req.url.params)
        if req.url.path.endswith("/about"):
            return _json({"user": {"permissionId": "p", "emailAddress": "a@x", "displayName": "A"}})
        return _json({"id": "f1", "name": "n"})

    client, _, _ = _client(handler)
    assert client.about()["user"]["displayName"] == "A"
    assert client.get("f1")["name"] == "n"
    assert "permissionId" in seen[f"{API_PATH}/about"]["fields"]
    assert seen[f"{API_PATH}/files/f1"] == {"fields": FIELDS, "supportsAllDrives": "true"}
