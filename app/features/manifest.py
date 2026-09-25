"""Feature → extras-group + import-probe mapping.

Each :class:`Feature` declares:

- ``extras``: the pyproject extras-group names that must be installed for
  this feature to work. Multiple features can share a group
  (``llm.openai`` and ``llm.openai_compatible`` both map to
  ``llm-openai``).
- ``probes``: ``importlib.util.find_spec`` targets used to detect whether
  the dep is already importable. We probe rather than parse pip metadata
  because the user may have installed the same wheels through another
  channel (e.g. a Docker image baked with ``cremind[all]``).
- ``post_install``: opaque step names handled by
  :mod:`app.features.installer` (e.g. ``"playwright_install_chromium"``).
- ``requires_restart``: True for features whose import has heavy native
  init that doesn't play well with same-process re-import — torch DLLs,
  playwright event loops. These features persist the wizard's choice but
  defer the ``apply_*`` step until the user restarts ``cremind serve``.
  The chat-channel SDKs are *not* in this bucket: their adapters import
  the SDK lazily inside methods (never at module load), so a runtime
  install followed by ``importlib.invalidate_caches()`` is importable
  in-process on the next connect — same rationale as ``claude_code`` /
  ``codex`` below.
- ``requirements``: verbatim PEP 508 strings, copied from the extras group,
  for the packages whose *version* matters and not just their presence
  (``tests/features/test_feature_requirements_drift.py`` keeps the copy in
  step with ``pyproject.toml``). A probe only proves a package imports, and
  the installer skips anything that imports, so a runtime venv that got the
  SDK under an older pin would keep it forever — nothing ever re-syncs a
  runtime venv. These strings let :func:`is_outdated` flag that install as
  "update required", and :func:`pip_requirements` hands them to pip
  verbatim so the update actually lands.

"Installed" and "outdated" are deliberately separate questions.
:func:`is_installed` stays probe-only, and every gate that blocks a tool
from working at all (tool enable, the Setup Wizard, channel connect) keeps
asking it: an outdated SDK still runs, so it must not suddenly read as
missing. Only the installer and the status surfaces ask
:func:`is_outdated`, and a version check that cannot be made (no dist
metadata, an unparseable version, ``packaging`` unavailable) fails open to
"not outdated" rather than nagging about an update nobody can verify.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
from dataclasses import dataclass, field

from app.upgrade.channel import Channel


@dataclass(frozen=True)
class Feature:
    key: str
    extras: tuple[str, ...]
    probes: tuple[str, ...]
    post_install: tuple[str, ...] = field(default_factory=tuple)
    requires_restart: bool = False
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class VersionCheck:
    """One :attr:`Feature.requirements` entry checked against the live venv.

    ``installed`` is the bare installed version (``"0.1.0b3"``), or ``None``
    when the distribution has no metadata here. ``satisfied`` is ``True``
    whenever the check could not be made — see the module docstring.
    """

    requirement: str
    dist: str
    installed: str | None
    satisfied: bool


FEATURES: dict[str, Feature] = {
    # ── Vector embedding (sentence-transformers + torch is the dominant cost) ──
    "embedding.me5": Feature(
        key="embedding.me5",
        extras=("embeddings-me5",),
        probes=("sentence_transformers", "pandas"),
        requires_restart=True,
    ),
    "embedding.gemma": Feature(
        key="embedding.gemma",
        extras=("embeddings-gemma",),
        probes=("sentence_transformers", "pandas"),
        requires_restart=True,
    ),

    # ── Vector store back-ends ───────────────────────────────────────────────
    "vectorstore.qdrant": Feature(
        key="vectorstore.qdrant",
        extras=("vectorstore-qdrant",),
        probes=("qdrant_client",),
    ),
    "vectorstore.chroma": Feature(
        key="vectorstore.chroma",
        extras=("vectorstore-chroma",),
        probes=("chromadb",),
    ),

    # ── Browser automation ───────────────────────────────────────────────────
    "browser": Feature(
        key="browser",
        extras=("browser",),
        probes=("playwright",),
        post_install=("playwright_install_chromium",),
        requires_restart=True,
    ),

    # ── Claude Code delegation (Claude Agent SDK bundles the CLI binary) ─────
    # No post_install: the platform wheel ships the Claude Code CLI binary, so
    # nothing extra to download. requires_restart=False: the claude_code tool
    # imports the SDK lazily inside run() (never at module load), and the
    # installer runs importlib.invalidate_caches() after pip, so a runtime pip
    # install is importable in-process on the very next call — no restart.
    "claude_code": Feature(
        key="claude_code",
        extras=("claude-code",),
        probes=("claude_agent_sdk",),
        requires_restart=False,
    ),

    # ── Codex delegation (OpenAI Codex SDK bundles the codex binary) ─────────
    # Mirrors claude_code: the openai-codex-cli-bin wheel ships the codex binary
    # (incl. win_amd64), so there is no post_install download, and the codex tool
    # imports openai_codex lazily inside run() (never at module load) — so, with
    # the installer's importlib.invalidate_caches() after pip, a runtime install
    # is importable in-process on the next call with no restart. That holds for
    # a FIRST install only: updating an SDK this process already imported leaves
    # the old module in memory, so the installer marks an update restart-pending
    # on its own. ``requirements`` mirrors the ``codex`` extra — 0.154 is the
    # first SDK + binary pair that can decode ChatGPT's live model catalog, so an
    # older install is reported as "update required".
    "codex": Feature(
        key="codex",
        extras=("codex",),
        probes=("openai_codex",),
        requires_restart=False,
        requirements=("openai-codex>=0.154.0,<0.155",),
    ),

    # ── Document ingestion + tabular processing ─────────────────────────────
    "documents": Feature(
        key="documents",
        extras=("documents",),
        probes=("markitdown", "pandas"),
    ),

    # ── User Document Search (indexing the user's own files) ────────────────
    # Rides on the ``documents`` extra (markitdown[all] brings pdfplumber,
    # pypdfium2, openpyxl, python-pptx, olefile, xlrd) plus its own: Pillow for
    # EXIF (only transitive until now) and pillow-heif so iPhone HEIC photos are
    # not silently metadata-only. (.cremindignore matching is built in; see
    # app/userdocs/discovery/ignore.py for why it is not pathspec.)
    # Hot-installable: every import happens inside the extractor subprocess.
    "userdocs": Feature(
        key="userdocs",
        extras=("documents", "userdocs"),
        probes=("markitdown", "pdfplumber", "PIL", "pillow_heif"),
    ),

    # ── Postgres back-end (alternative to bundled SQLite) ───────────────────
    "postgres": Feature(
        key="postgres",
        extras=("postgres",),
        probes=("asyncpg", "psycopg"),
    ),

    # ── LLM SDKs ─────────────────────────────────────────────────────────────
    "llm.anthropic": Feature(
        key="llm.anthropic",
        extras=("llm-anthropic",),
        probes=("anthropic",),
    ),
    "llm.openai": Feature(
        key="llm.openai",
        extras=("llm-openai",),
        probes=("openai", "tiktoken"),
    ),
    "llm.groq": Feature(
        key="llm.groq",
        extras=("llm-groq",),
        probes=("groq", "tiktoken"),
    ),
    # Umbrella for every OpenAI-compatible third-party (chutes, mistral,
    # moonshot, qwen, ...). They route through ``app.lib.llm.openai`` so
    # ``llm-openai`` is the only group required.
    "llm.openai_compatible": Feature(
        key="llm.openai_compatible",
        extras=("llm-openai",),
        probes=("openai",),
    ),

    # ── Google APIs (Places, Calendar, Vertex AI OAuth) ─────────────────────
    "google": Feature(
        key="google",
        extras=("google",),
        probes=("googleapiclient", "google.auth"),
    ),

    # ── External chat channels ──────────────────────────────────────────────
    # requires_restart=False: every channel adapter imports its SDK lazily
    # inside methods (``TelegramAdapter._build_bot``, etc.), never at module
    # load. The installer runs ``importlib.invalidate_caches()`` after pip, and
    # the registry installs the feature *before* starting the adapter, so a
    # runtime install is importable in-process on the very next connect — no
    # restart (same reasoning as claude_code / codex).
    "channel.telegram.bot": Feature(
        key="channel.telegram.bot",
        extras=("channel-telegram-bot",),
        probes=("telegram",),
        requires_restart=False,
    ),
    "channel.telegram.userbot": Feature(
        key="channel.telegram.userbot",
        extras=("channel-telegram-userbot",),
        probes=("telethon",),
        requires_restart=False,
    ),
    "channel.discord.bot": Feature(
        key="channel.discord.bot",
        extras=("channel-discord",),
        probes=("discord",),
        requires_restart=False,
    ),
    "channel.slack.bot": Feature(
        key="channel.slack.bot",
        extras=("channel-slack",),
        probes=("slack_bolt",),
        requires_restart=False,
    ),
    # Messenger (Graph API webhook) and Zalo (Bot API long-poll) ride the core
    # ``httpx`` client, and the Zalo personal channel is a Node sidecar — none
    # of them need a Python extras group, so they have no FEATURES entry.
}


# Map an LLM provider id (as stored in ``llm_config``) onto the feature
# key whose extras group covers it. Providers not in this dict route through
# the OpenAI SDK and are covered by ``llm.openai_compatible``.
LLM_PROVIDER_TO_FEATURE: dict[str, str] = {
    "anthropic": "llm.anthropic",
    "openai": "llm.openai",
    "groq": "llm.groq",
}


def channel_feature_key(channel_type: str, mode: str) -> str | None:
    """Feature key whose extras a channel adapter needs, or ``None``.

    Returns ``None`` for channels that ride the core ``httpx`` client
    (Messenger, Zalo bot/notification) or a Node.js sidecar (WhatsApp, Zalo
    userbot) — none of those need a Python extras group. Single source of
    truth for both the Setup Wizard preflight
    (:func:`app.api.config._features_required_by_setup_payload`) and the
    install-on-connect path in :meth:`ChannelRegistry.start_for_channel`.
    """
    ct = (channel_type or "").lower()
    md = (mode or "").lower()
    if ct == "telegram":
        return "channel.telegram.userbot" if md == "userbot" else "channel.telegram.bot"
    if ct == "discord":
        return "channel.discord.bot"
    if ct == "slack":
        return "channel.slack.bot"
    return None


def is_installed(feature_key: str) -> bool:
    """Return True if every probe for ``feature_key`` is importable.

    Uses :func:`importlib.util.find_spec` so we don't actually load the
    package — important on probes that have heavy side effects (torch
    triggers CUDA discovery on first import).
    """
    feature = FEATURES.get(feature_key)
    if feature is None:
        raise KeyError(f"Unknown feature: {feature_key!r}")
    for probe in feature.probes:
        try:
            spec = importlib.util.find_spec(probe)
        except (ImportError, ValueError):
            return False
        if spec is None:
            return False
    return True


def missing_features(feature_keys: list[str]) -> list[str]:
    """Return the subset of ``feature_keys`` whose deps are NOT installed.

    Order-preserving + de-duplicating so the caller can build a single pip
    spec without worrying about input shape.
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in feature_keys:
        if key in seen:
            continue
        seen.add(key)
        if key not in FEATURES:
            raise KeyError(f"Unknown feature: {key!r}")
        if not is_installed(key):
            out.append(key)
    return out


def pip_spec(feature_keys: list[str], channel: Channel = "production") -> str:
    """Build the ``cremind[a,b,c]==<version>`` pin for one pip invocation.

    Pinning to the current installed version ensures pip doesn't try to
    upgrade core when the user only asked to add an extras group — that
    would risk pulling in a newer wheel with different transitive deps
    mid-session.

    Dev channel skips the version pin: the editable install rooted at
    ``/src`` (or the developer's checkout) already satisfies any
    ``cremind[extras]`` requirement, and pinning to a version that may
    not yet exist on PyPI (e.g. during a release-prep cycle) forces pip
    to consult an index it can't satisfy. Combined with
    ``_pip_install(upgrade=False)``, the unpinned spec lets pip keep the
    existing editable cremind and only resolve the extras' transitive
    deps.
    """
    from app.__version__ import __version__

    groups: list[str] = []
    seen: set[str] = set()
    for key in feature_keys:
        feat = FEATURES.get(key)
        if feat is None:
            raise KeyError(f"Unknown feature: {key!r}")
        for grp in feat.extras:
            if grp not in seen:
                seen.add(grp)
                groups.append(grp)
    if channel == "dev":
        if not groups:
            return "cremind"
        return f"cremind[{','.join(groups)}]"
    if not groups:
        return f"cremind=={__version__}"
    return f"cremind[{','.join(groups)}]=={__version__}"


# ── version checks ──────────────────────────────────────────────────────────

# The distribution name a PEP 508 string starts with. Only consulted when
# ``packaging`` can't parse the string (or can't be imported), so the report
# still names the package it could not check.
_DIST_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _require_feature(feature_key: str) -> Feature:
    feature = FEATURES.get(feature_key)
    if feature is None:
        raise KeyError(f"Unknown feature: {feature_key!r}")
    return feature


def _installed_version(dist: str) -> str | None:
    """Installed version of ``dist`` from its metadata, or ``None``.

    Called through the ``importlib.metadata`` module attribute (not a bound
    import) so tests can patch ``importlib.metadata.version``. Any failure
    reads as "no metadata": a half-written dist-info must not break
    ``GET /api/features``.
    """
    try:
        return importlib.metadata.version(dist)
    except Exception:  # noqa: BLE001 — PackageNotFoundError, corrupt metadata
        return None


def _check_requirement(requirement: str) -> VersionCheck:
    match = _DIST_NAME_RE.match(requirement)
    fallback_dist = match.group(1) if match else requirement.strip()
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.version import InvalidVersion, Version
    except ImportError:
        # Core depends on ``packaging``, but a hand-assembled venv may still
        # lack it; without a parser there is nothing to compare, so fail open.
        return VersionCheck(requirement, fallback_dist, _installed_version(fallback_dist), True)

    try:
        req = Requirement(requirement)
    except InvalidRequirement:
        return VersionCheck(requirement, fallback_dist, _installed_version(fallback_dist), True)

    installed = _installed_version(req.name)
    if installed is None:
        return VersionCheck(requirement, req.name, None, True)
    try:
        # A requirement whose marker excludes this platform doesn't apply here.
        if req.marker is not None and not req.marker.evaluate():
            return VersionCheck(requirement, req.name, installed, True)
    except Exception:  # noqa: BLE001 — undefined marker variables: can't judge, fail open
        return VersionCheck(requirement, req.name, installed, True)
    try:
        # ``prereleases=True``: an installed pre-release must be judged by the
        # range alone. By default a range that names no pre-release rejects
        # every one, so a beta that sits inside the range would be reported
        # outdated and "updated" on every install.
        satisfied = req.specifier.contains(Version(installed), prereleases=True)
    except InvalidVersion:
        satisfied = True
    return VersionCheck(requirement, req.name, installed, satisfied)


def version_checks(feature_key: str) -> list[VersionCheck]:
    """Check each :attr:`Feature.requirements` entry against the live venv.

    Returns one :class:`VersionCheck` per requirement, in declaration order
    (an empty list for a feature that declares none). Raises ``KeyError`` for
    an unknown feature. This reads dist metadata only — it says nothing about
    whether the feature imports; :func:`is_outdated` combines the two.
    """
    feature = _require_feature(feature_key)
    return [_check_requirement(req) for req in feature.requirements]


def is_outdated(feature_key: str) -> bool:
    """Return True if ``feature_key`` is installed but below its required range.

    A feature that isn't installed is *missing*, not outdated — installing it
    already brings the right version. The probe only runs once a requirement
    is actually unsatisfied, so the common case (no requirements, or all
    satisfied) never touches ``find_spec``.
    """
    checks = version_checks(feature_key)
    if all(check.satisfied for check in checks):
        return False
    return is_installed(feature_key)


def outdated_features(feature_keys: list[str]) -> list[str]:
    """Return the subset of ``feature_keys`` that :func:`is_outdated` flags.

    Order-preserving + de-duplicating, and raises ``KeyError`` on an unknown
    key, exactly like :func:`missing_features`.
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in feature_keys:
        if key in seen:
            continue
        seen.add(key)
        if key not in FEATURES:
            raise KeyError(f"Unknown feature: {key!r}")
        if is_outdated(key):
            out.append(key)
    return out


def version_report(feature_key: str) -> dict:
    """Version facts for one feature, as the status endpoints publish them.

    ``{"outdated": bool, "required": [<PEP 508 string>, ...],
    "installed_versions": {<dist>: <version or None>}}``. System facts only —
    package names and versions, nothing profile-scoped.
    """
    feature = _require_feature(feature_key)
    checks = version_checks(feature_key)
    outdated = any(not check.satisfied for check in checks) and is_installed(feature_key)
    return {
        "outdated": outdated,
        "required": list(feature.requirements),
        "installed_versions": {check.dist: check.installed for check in checks},
    }


def pip_requirements(feature_keys: list[str], channel: Channel = "production") -> list[str]:
    """Every argument ``pip install`` needs to install or update ``feature_keys``.

    Production and test: :func:`pip_spec` for all the keys, followed by each
    feature's :attr:`Feature.requirements` (de-duplicated). The cremind pin
    alone would already make pip upgrade a dependency that falls outside the
    range in cremind's metadata, but naming the range outright means the
    update never hinges on which cremind metadata pip happens to read, and
    the streamed ``pip install`` line shows exactly what is being updated.

    Dev: features that declare requirements are left OUT of the
    ``cremind[...]`` spec and installed through their requirement strings
    alone. The editable cremind's installed metadata is only as fresh as the
    last ``uv sync`` (a dev venv can still record an older version with the
    old pin), so pairing ``cremind[codex]`` with the new range asks pip for
    two contradictory ranges — unresolvable, or "resolved" by fetching a PyPI
    cremind over the checkout. ``pip_spec([], "dev")`` is plain ``cremind``,
    which the editable install already satisfies.
    """
    for key in feature_keys:
        _require_feature(key)

    requirements: list[str] = []
    seen: set[str] = set()
    for key in feature_keys:
        for req in FEATURES[key].requirements:
            if req not in seen:
                seen.add(req)
                requirements.append(req)

    if channel == "dev":
        spec = pip_spec([k for k in feature_keys if not FEATURES[k].requirements], channel="dev")
    else:
        spec = pip_spec(feature_keys, channel=channel)
    return [spec, *requirements]
