"""Release-listing shim used by the TUI installer.

The TUI runs inside an ephemeral ``uv run --with prompt_toolkit --with
rich`` env that does **not** have the cremind wheel installed — only the
repo checkout on ``PYTHONPATH`` (the bootstrap arranges this). When
``app.upgrade.manifest`` imports cleanly we delegate straight to it so
there is one source of truth for the GitHub queries. When it doesn't
(e.g. an older checkout where the symbols are missing), we fall back to
a small embedded implementation with the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

try:
    from app.upgrade.manifest import (
        RateLimitExceeded,
        ReleaseSummary,
        list_releases,
    )

    _USING_UPSTREAM = True
except ImportError:  # pragma: no cover - only triggers in mismatched checkouts
    _USING_UPSTREAM = False

    import json
    import os
    import urllib.error
    import urllib.request
    from typing import Any, Literal

    # Version/tag-format knowledge always comes from channel.py — it's
    # stdlib-only, so it imports even when the heavier ``manifest`` deps
    # don't. This keeps the fallback from drifting (it used to carry its
    # own ``rcN``-only regexes that missed the ``.devM`` form).
    from app.upgrade.channel import is_rc_tag, matches_channel, parse_pep440, tag_to_pep440

    Channel = Literal["production", "test", "dev"]
    _DEFAULT_REPO = os.environ.get("CREMIND_UPGRADE_REPO", "cremind-ai/cremind")

    class RateLimitExceeded(Exception):  # type: ignore[no-redef]
        def __init__(self, reset_at: int | None) -> None:
            self.reset_at = reset_at
            super().__init__("GitHub API rate limit exceeded")

    @dataclass(frozen=True)
    class ReleaseSummary:  # type: ignore[no-redef]
        tag_name: str
        name: str
        version: str
        published_at: str
        prerelease: bool
        html_url: str

    def list_releases(  # type: ignore[no-redef]
        repo: str = _DEFAULT_REPO,
        *,
        channel: Channel | None = None,
        limit: int = 30,
        timeout: float = 10.0,
    ) -> list[ReleaseSummary]:
        if channel == "dev":
            return []
        capped = max(1, min(limit, 100))
        url = f"https://api.github.com/repos/{repo}/releases?per_page={capped}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "cremind-installer/embedded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload: Any = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
                reset_raw = exc.headers.get("X-RateLimit-Reset")
                try:
                    reset_at = int(reset_raw) if reset_raw else None
                except (TypeError, ValueError):
                    reset_at = None
                raise RateLimitExceeded(reset_at) from exc
            raise

        out: list[ReleaseSummary] = []
        for entry in payload or []:
            if not isinstance(entry, dict):
                continue
            tag = entry.get("tag_name") or ""
            prerelease = bool(entry.get("prerelease"))
            if channel == "production":
                if prerelease:
                    continue
                version = tag[1:] if tag.startswith("v") else tag
                if not matches_channel(version, "production"):
                    continue
            else:
                if not (prerelease and is_rc_tag(tag)):
                    continue
                version = tag_to_pep440(tag)
            out.append(
                ReleaseSummary(
                    tag_name=tag,
                    name=entry.get("name") or tag,
                    version=version,
                    published_at=entry.get("published_at") or "",
                    prerelease=prerelease,
                    html_url=entry.get("html_url") or "",
                )
            )
        out.sort(key=lambda r: parse_pep440(r.version), reverse=True)
        return out[:limit]


if TYPE_CHECKING:  # narrow types for tooling
    pass


__all__ = ["RateLimitExceeded", "ReleaseSummary", "list_releases"]
