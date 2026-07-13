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
  playwright event loops, telegram pollers. These features persist the
  wizard's choice but defer the ``apply_*`` step until the user restarts
  ``cremind serve``.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field

from app.upgrade.channel import Channel


@dataclass(frozen=True)
class Feature:
    key: str
    extras: tuple[str, ...]
    probes: tuple[str, ...]
    post_install: tuple[str, ...] = field(default_factory=tuple)
    requires_restart: bool = False


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

    # ── Document ingestion + tabular processing ─────────────────────────────
    "documents": Feature(
        key="documents",
        extras=("documents",),
        probes=("markitdown", "pandas"),
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
    # Umbrella for every OpenAI-compatible third-party (chutes, deepseek,
    # mistral, moonshot, ...). They route through ``app.lib.llm.openai`` so
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
    "channel.telegram.bot": Feature(
        key="channel.telegram.bot",
        extras=("channel-telegram-bot",),
        probes=("telegram",),
        requires_restart=True,
    ),
    "channel.telegram.userbot": Feature(
        key="channel.telegram.userbot",
        extras=("channel-telegram-userbot",),
        probes=("telethon",),
        requires_restart=True,
    ),
    "channel.discord.bot": Feature(
        key="channel.discord.bot",
        extras=("channel-discord",),
        probes=("discord",),
        requires_restart=True,
    ),
    "channel.slack.bot": Feature(
        key="channel.slack.bot",
        extras=("channel-slack",),
        probes=("slack_bolt",),
        requires_restart=True,
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
