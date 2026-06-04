"""Tests for the release-listing API used by the TUI installer."""

from __future__ import annotations

import io
import urllib.error
from typing import Any

import pytest

from app.upgrade import manifest
from app.upgrade.manifest import RateLimitExceeded, ReleaseSummary, list_releases


def _release_payload(tag: str, *, prerelease: bool = False, published_at: str = "") -> dict[str, Any]:
    return {
        "tag_name": tag,
        "name": tag,
        "html_url": f"https://example.invalid/r/{tag}",
        "body": "",
        "assets": [],
        "prerelease": prerelease,
        "published_at": published_at,
    }


def test_list_releases_dev_returns_empty() -> None:
    assert list_releases(channel="dev") == []


def test_list_releases_production_filters_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        _release_payload("v0.2.0", published_at="2026-01-01T00:00:00Z"),
        _release_payload("v0.2.2", published_at="2026-03-01T00:00:00Z"),
        _release_payload("v0.2.1rc3.dev1", prerelease=True),  # excluded
        _release_payload("v0.2.1", published_at="2026-02-01T00:00:00Z"),
    ]
    monkeypatch.setattr(manifest, "_http_get_json", lambda url, *, timeout: payload)

    summaries = list_releases(repo="x/y", channel="production", limit=10)

    assert [s.tag_name for s in summaries] == ["v0.2.2", "v0.2.1", "v0.2.0"]
    assert all(not s.prerelease for s in summaries)
    assert summaries[0].version == "0.2.2"
    assert isinstance(summaries[0], ReleaseSummary)


def test_resolve_release_finds_specific_dev_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The picker must be able to pin an OLDER RC even when a newer one exists.
    payload = [
        _release_payload("v0.2.9rc2.dev1", prerelease=True),  # newer
        _release_payload("v0.2.9rc1.dev1", prerelease=True),
        _release_payload("v0.2.9rc1.dev2", prerelease=True),  # target
    ]
    monkeypatch.setattr(manifest, "_http_get_json", lambda url, *, timeout: payload)

    info = manifest.resolve_release("0.2.9rc1.dev2", channel="test", repo="x/y")
    assert info.version == "0.2.9rc1.dev2"
    assert info.tag_name == "v0.2.9rc1.dev2"
    assert info.channel == "test"


def test_resolve_release_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [_release_payload("v0.2.9rc1.dev1", prerelease=True)]
    monkeypatch.setattr(manifest, "_http_get_json", lambda url, *, timeout: payload)
    with pytest.raises(LookupError):
        manifest.resolve_release("0.2.9rc9.dev9", channel="test", repo="x/y")


def test_resolve_release_rejects_non_test_channel() -> None:
    with pytest.raises(ValueError):
        manifest.resolve_release("0.2.9", channel="production")


def test_list_releases_test_keeps_only_rc_prereleases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        _release_payload("v0.2.2", prerelease=False),                # excluded: final
        _release_payload("v0.2.1rc1.dev1", prerelease=True),
        _release_payload("v0.2.1rc3.dev1", prerelease=True),
        _release_payload("v0.2.1rc2.dev1", prerelease=True),
        _release_payload("v0.2.0-test1", prerelease=True),           # excluded: non-RC tag shape
        _release_payload("v0.2.0-rc.1.dev.1", prerelease=True),      # excluded: legacy hyphenated form
    ]
    monkeypatch.setattr(manifest, "_http_get_json", lambda url, *, timeout: payload)

    summaries = list_releases(repo="x/y", channel="test", limit=10)

    assert [s.tag_name for s in summaries] == [
        "v0.2.1rc3.dev1",
        "v0.2.1rc2.dev1",
        "v0.2.1rc1.dev1",
    ]
    assert [s.version for s in summaries] == [
        "0.2.1rc3.dev1",
        "0.2.1rc2.dev1",
        "0.2.1rc1.dev1",
    ]
    assert all(s.prerelease for s in summaries)


def test_list_releases_caps_limit_and_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        _release_payload(f"v0.{i}.0") for i in range(50)
    ]
    seen_urls: list[str] = []

    def fake(url, *, timeout):
        seen_urls.append(url)
        return payload

    monkeypatch.setattr(manifest, "_http_get_json", fake)

    summaries = list_releases(repo="x/y", channel="production", limit=5)

    assert len(summaries) == 5
    assert "per_page=5" in seen_urls[0]
    # Sorted desc, so latest (highest minor) first
    assert summaries[0].tag_name == "v0.49.0"


def test_list_releases_raises_value_error_on_non_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        manifest, "_http_get_json", lambda url, *, timeout: {"message": "Not Found"}
    )
    with pytest.raises(ValueError, match="did not return a list"):
        list_releases(repo="x/y", channel="production")


def test_http_get_json_raises_rate_limit_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Build a fake HTTPError that mimics GitHub's rate-limit response.
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"}
    err = urllib.error.HTTPError(
        url="https://api.github.com/foo",
        code=403,
        msg="rate limit exceeded",
        hdrs=headers,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )

    def fake_urlopen(req, timeout):
        raise err

    monkeypatch.setattr(manifest.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RateLimitExceeded) as exc_info:
        manifest._http_get_json("https://api.github.com/foo", timeout=1.0)

    assert exc_info.value.reset_at == 1700000000


def test_http_get_json_passes_through_other_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"X-RateLimit-Remaining": "59"}
    err = urllib.error.HTTPError(
        url="https://api.github.com/foo",
        code=404,
        msg="Not Found",
        hdrs=headers,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )

    def fake_urlopen(req, timeout):
        raise err

    monkeypatch.setattr(manifest.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        manifest._http_get_json("https://api.github.com/foo", timeout=1.0)
