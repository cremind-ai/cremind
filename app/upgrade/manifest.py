"""Release-manifest lookup for the upgrader.

We treat the GitHub Releases API as the source of truth: each release
ships with a tag (the SemVer) and an optional ``release.json`` asset
that pins the compatibility matrix. Reading the tag is enough for the
common case; the asset adds defense-in-depth for releases that bump
``min_supported_upgrade_from``.

Why GitHub and not a self-hosted feed: it's free, signs artifacts via
Actions, integrates with electron-updater for the UI side, and gives
users a public changelog out of the box. Self-hosted would be a strict
upgrade only if and when the team needs staged rollouts.

Network failures are non-fatal here — callers display "couldn't check
for updates" and move on. We never fail the running server because the
internet is flaky.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.__version__ import (
    MIN_SUPPORTED_UPGRADE_FROM as _CURRENT_MIN_FROM,
    __version__ as _CURRENT_VERSION,
)
from app.upgrade.channel import (
    Channel,
    get_channel,
    is_rc_tag,
    parse_pep440,
    tag_to_pep440,
)


# ``CREMIND_UPGRADE_REPO`` overrides the default for staging / forks.
DEFAULT_REPO = os.environ.get("CREMIND_UPGRADE_REPO", "cremind-ai/cremind")
LATEST_URL_TMPL = "https://api.github.com/repos/{repo}/releases/latest"
LIST_URL_TMPL = "https://api.github.com/repos/{repo}/releases"


class RateLimitExceeded(Exception):
    """Raised when GitHub's unauthenticated rate limit is hit (60/hour/IP).

    ``reset_at`` is the epoch second when the limit resets, parsed from the
    ``X-RateLimit-Reset`` response header. ``None`` if the header was
    missing or malformed.
    """

    def __init__(self, reset_at: int | None) -> None:
        self.reset_at = reset_at
        if reset_at is not None:
            super().__init__(
                f"GitHub API rate limit exceeded; resets at epoch {reset_at}"
            )
        else:
            super().__init__("GitHub API rate limit exceeded")


@dataclass(frozen=True)
class ReleaseSummary:
    """One row in a release-picker list.

    ``version`` is the PEP 440 form (``rcN`` rewritten for test-channel
    tags so callers can pin via ``pip install cremind==<version>``).
    ``tag_name`` is the raw GitHub tag. ``published_at`` is the ISO-8601
    string straight from the API so the picker can render dates without
    timezone math.
    """

    tag_name: str
    name: str
    version: str
    published_at: str
    prerelease: bool
    html_url: str


@dataclass(frozen=True)
class ReleaseInfo:
    """Minimal manifest the upgrader needs to make a decision.

    ``version`` is the PEP 440 / SemVer string callers compare against
    ``__version__`` — for a test prerelease this is the ``rcN`` form,
    not the raw tag. ``asset_url`` is the optional ``release.json`` link
    if the release published one; when present, the upgrader fetches it
    and overlays the compatibility fields on top of the defaults.
    ``channel`` records which feed produced this record so the runner
    can build the right pip args (Test PyPI vs prod PyPI).
    """

    version: str
    tag_name: str
    name: str
    html_url: str
    body: str
    asset_url: str | None
    min_supported_upgrade_from: str
    channel: Channel


def fetch_latest(
    repo: str = DEFAULT_REPO,
    *,
    timeout: float = 10.0,
    channel: Channel | None = None,
) -> ReleaseInfo:
    """Fetch the latest release on the requested channel. Raises on failure.

    ``channel="production"`` (or ``None``, resolved via :func:`get_channel`)
    hits ``/releases/latest``. GitHub's documented behavior is that this
    endpoint excludes pre-releases, so prod hosts cannot accidentally see
    test tags — the regression-safety guarantee for existing installs.

    ``channel="test"`` lists ``/releases``, filters to entries marked
    ``prerelease=true`` whose tag matches the canonical RC shape
    ``v<X.Y.Z>[.W]rc<N>.dev<M>``, and returns the highest by PEP 440
    ordering. Page 1 (default 30 items) is enough in practice; if a
    test channel ever accumulates more than 30 unreleased entries,
    the cap is the right thing to hit anyway.

    ``channel="dev"`` short-circuits the GitHub lookup entirely and
    returns a synthesised "always available" release derived from the
    current install. Dev installs run a working copy, so there is no
    upstream release that could be newer than the running code; without
    the synth, the Update button would never light up on dev. See
    :func:`_synthesize_dev_release`.
    """
    ch: Channel = channel if channel is not None else get_channel()
    if ch == "test":
        return _fetch_latest_test(repo=repo, timeout=timeout)
    if ch == "dev":
        return _synthesize_dev_release()
    return _fetch_latest_prod(repo=repo, timeout=timeout)


def _synthesize_dev_release() -> ReleaseInfo:
    """Build a synthetic ReleaseInfo that always reports as newer than
    the running version.

    Used only on the ``dev`` channel to exercise the in-app updater UI
    end-to-end against a working-copy install. The version carries a
    PEP 440 *local-version* suffix (``+devforced``) — :func:`parse`
    treats that as strictly newer than the same version without one, so
    ``is_newer(synth.version, CURRENT_VERSION)`` is always True
    regardless of whether the current install is a final (``0.2.1``) or
    an rc (``0.2.1rc6``).

    The runner's matching dev-channel branch in
    :func:`app.upgrade.runner._apply_locked` skips the actual pip
    install — so clicking Update on dev exercises backup → migrate →
    health → restart without touching the editable install.
    """
    synth_version = f"{_CURRENT_VERSION}+devforced"
    return ReleaseInfo(
        version=synth_version,
        tag_name=f"v{synth_version}",
        name="Dev channel — forced available for in-app updater testing",
        html_url="",
        body=(
            "**Dev channel synthetic release.**\n\n"
            "This entry is generated locally so the in-app Update button is "
            "always testable on a working-copy install. Clicking Update Now "
            "will run the full upgrade flow (backup → migrate → restart) but "
            "skip the actual `pip install` step — your editable install is "
            "not modified."
        ),
        asset_url=None,
        # Never blocks: the synth must work from any install older than
        # itself (which is every dev install by construction).
        min_supported_upgrade_from="0.0.0",
        channel="dev",
    )


def _fetch_latest_prod(*, repo: str, timeout: float) -> ReleaseInfo:
    url = LATEST_URL_TMPL.format(repo=repo)
    data = _http_get_json(url, timeout=timeout)
    return _parse_release(data, channel="production")


def _fetch_latest_test(*, repo: str, timeout: float) -> ReleaseInfo:
    url = LIST_URL_TMPL.format(repo=repo)
    payload = _http_get_json(url, timeout=timeout)
    if not isinstance(payload, list):
        raise ValueError("GitHub /releases did not return a list payload")

    candidates: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if not entry.get("prerelease"):
            continue
        tag = entry.get("tag_name") or ""
        if is_rc_tag(tag):
            candidates.append(entry)

    if not candidates:
        raise LookupError("No RC prereleases found on GitHub for this repo.")

    candidates.sort(key=lambda e: parse_pep440(tag_to_pep440(e["tag_name"])))
    return _parse_release(candidates[-1], channel="test")


def resolve_release(
    target_version: str,
    *,
    channel: Channel = "test",
    repo: str = DEFAULT_REPO,
    timeout: float = 10.0,
) -> ReleaseInfo:
    """Return the ``ReleaseInfo`` for a *specific* test-channel version.

    Powers the Updates-page version picker: unlike :func:`fetch_latest`, which
    always takes the highest, this finds the release whose PEP 440 version
    equals ``target_version`` (e.g. ``0.2.9rc1.dev1`` even when
    ``0.2.9rc2.dev1`` is newer) and parses it through :func:`_parse_release`
    so the target's own ``min_supported_upgrade_from`` is honored.

    Only the ``test`` channel is supported — production installs upgrade to the
    latest final and have no per-version picker, and dev has no upstream list.
    """
    if channel != "test":
        raise ValueError(f"resolve_release is test-channel only, got {channel!r}")

    url = LIST_URL_TMPL.format(repo=repo)
    payload = _http_get_json(url, timeout=timeout)
    if not isinstance(payload, list):
        raise ValueError("GitHub /releases did not return a list payload")

    for entry in payload:
        if not isinstance(entry, dict) or not entry.get("prerelease"):
            continue
        tag = entry.get("tag_name") or ""
        if is_rc_tag(tag) and tag_to_pep440(tag) == target_version:
            return _parse_release(entry, channel="test")

    raise LookupError(
        f"No test release {target_version!r} found on GitHub for this repo."
    )


def list_releases(
    repo: str = DEFAULT_REPO,
    *,
    channel: Channel | None = None,
    limit: int = 30,
    timeout: float = 10.0,
) -> list[ReleaseSummary]:
    """Return the most recent ``limit`` releases for ``channel``, newest-first.

    Per channel:
      - ``production`` returns final releases (``prerelease=False``) sorted
        by PEP 440 desc.
      - ``test`` returns release candidates whose tag matches the
        canonical ``v<X.Y.Z>[.W]rc<N>.dev<M>`` shape (``prerelease=True``
        AND :func:`is_rc_tag`), sorted by PEP 440 desc with the leading
        ``v`` stripped to give the PEP 440 form.
      - ``dev`` returns ``[]`` — there is no upstream release list when
        running the working copy, callers should skip the picker.

    Caller-visible errors:
      - :class:`RateLimitExceeded` when GitHub's unauthenticated quota
        (60/hr/IP) is exhausted. The installer surfaces an "use
        ``--version <spec>`` to skip" hint with the reset time.
      - :class:`urllib.error.URLError` for network failures.

    ``limit`` is capped at GitHub's ``per_page`` (100) for safety; one
    page is enough in practice — the installer caps at 30.
    """
    ch: Channel = channel if channel is not None else get_channel()
    if ch == "dev":
        return []

    capped = max(1, min(limit, 100))
    url = f"{LIST_URL_TMPL.format(repo=repo)}?per_page={capped}"
    payload = _http_get_json(url, timeout=timeout)
    if not isinstance(payload, list):
        raise ValueError("GitHub /releases did not return a list payload")

    summaries: list[ReleaseSummary] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        tag = entry.get("tag_name") or ""
        prerelease = bool(entry.get("prerelease"))

        if ch == "production":
            if prerelease:
                continue
            version = _strip_v(tag)
            if not _looks_like_semver(version):
                continue
        else:  # test
            if not prerelease or not is_rc_tag(tag):
                continue
            version = tag_to_pep440(tag)

        summaries.append(
            ReleaseSummary(
                tag_name=tag,
                name=entry.get("name") or tag,
                version=version,
                published_at=entry.get("published_at") or "",
                prerelease=prerelease,
                html_url=entry.get("html_url") or "",
            )
        )

    summaries.sort(key=lambda r: parse_pep440(r.version), reverse=True)
    return summaries[:limit]


def _http_get_json(url: str, *, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub serves the same data without a token but the
            # rate limit is 60/hour/IP. A user-agent is required.
            "User-Agent": f"cremind-upgrader/{_CURRENT_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # GitHub returns 403 + ``X-RateLimit-Remaining: 0`` when the
        # unauthenticated quota is exhausted. Surface this as a typed
        # error so the installer can recommend ``--version <spec>`` to
        # skip the picker instead of dumping a generic HTTPError.
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            reset_raw = exc.headers.get("X-RateLimit-Reset")
            try:
                reset_at: int | None = int(reset_raw) if reset_raw else None
            except (TypeError, ValueError):
                reset_at = None
            raise RateLimitExceeded(reset_at) from exc
        raise


def _parse_release(payload: dict[str, Any], *, channel: Channel) -> ReleaseInfo:
    tag = payload.get("tag_name") or ""
    if channel == "test":
        # Test tags carry the canonical ``vX.Y.ZrcN.devM`` shape (PEP
        # 440 form with a leading ``v``); ``tag_to_pep440`` just strips
        # the ``v``. The wheel on Test PyPI is named with that exact
        # PEP 440 form, so the round-trip is trivial.
        if not is_rc_tag(tag):
            raise ValueError(f"Test channel got non-RC tag: {tag!r}")
        version = tag_to_pep440(tag)
    else:
        version = _strip_v(tag)
        if not _looks_like_semver(version):
            raise ValueError(f"GitHub release tag is not SemVer: {tag!r}")

    # Walk the assets list for ``release.json``. The manifest is optional
    # — when missing, the upgrader uses the conservative defaults
    # described in app/__version__.py.
    asset_url: str | None = None
    for asset in payload.get("assets") or []:
        if asset.get("name") == "release.json":
            asset_url = asset.get("browser_download_url")
            break

    min_from = _CURRENT_MIN_FROM
    if asset_url:
        try:
            extra = _fetch_release_manifest(asset_url)
            if isinstance(extra.get("min_supported_upgrade_from"), str):
                min_from = extra["min_supported_upgrade_from"]
        except Exception:  # noqa: BLE001
            # The manifest is best-effort. Fall back to defaults rather
            # than refusing to upgrade because of a single missing asset.
            pass

    return ReleaseInfo(
        version=version,
        tag_name=tag,
        name=payload.get("name") or tag,
        html_url=payload.get("html_url") or "",
        body=payload.get("body") or "",
        asset_url=asset_url,
        min_supported_upgrade_from=min_from,
        channel=channel,
    )


def _fetch_release_manifest(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"cremind-upgrader/{_CURRENT_VERSION}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── version helpers ───────────────────────────────────────────────────────

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[\w.]+)?(?:\+[\w.]+)?$")


def _strip_v(s: str) -> str:
    return s[1:] if s.startswith("v") else s


def _looks_like_semver(s: str) -> bool:
    return bool(_SEMVER.match(s))


def parse(s: str) -> tuple[int, int, int, int, int, str]:
    """Parse a version string into a sortable PEP 440 tuple.

    Accepts the prod form (``0.2.1``), the test-channel PEP 440 rc
    form (``0.2.1rc3``), and the dev-channel synth form with a local
    suffix (``0.2.1+devforced``). A leading ``v`` is tolerated. Returns
    the same shape as :func:`channel.parse_pep440`, which orders rc
    releases before the corresponding final and treats a populated local
    segment as strictly newer than no local segment.
    """
    return parse_pep440(_strip_v(s))


def is_newer(latest: str, current: str) -> bool:
    """``True`` iff ``latest`` is strictly newer than ``current``."""
    return parse(latest) > parse(current)


def is_at_or_above(version: str, floor: str) -> bool:
    """``True`` iff ``version`` >= ``floor`` under PEP 440 ordering."""
    return parse(version) >= parse(floor)


__all__ = [
    "DEFAULT_REPO",
    "RateLimitExceeded",
    "ReleaseInfo",
    "ReleaseSummary",
    "fetch_latest",
    "is_at_or_above",
    "is_newer",
    "list_releases",
    "parse",
]
