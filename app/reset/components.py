"""Canonical component vocabulary for the per-profile "clean data" feature.

This is the single source of truth shared by the REST API, the CLI, and (mirrored)
the UI + bundled docs. Each key is a snake_case token; the CLI flag is its kebab-case
form and the UI checkbox uses the identical snake_case key. Preset membership
(``working`` / ``factory``) is defined **only** here, so both clients and the docs
inherit any change for free — the server expands presets, clients just send the scope.
"""

from __future__ import annotations

# Canonical ordered list of every cleanable component (drives display order too).
COMPONENTS: tuple[str, ...] = (
    # Conversations, memory & uploads
    "conversations",
    "memory",
    "uploads",
    "plans",
    # Usage & event-run history
    "usage",
    "event_runs",
    # Automation & channels
    "processes",
    "schedules",
    "file_watchers",
    "skill_events",
    "channels",
    # Config & credentials
    "llm_config",
    "oauth_tokens",
    "tool_configs",
    "skills",
    "documents",
    "browser_login",
    "app_settings",
)

# "Working-data reset" — runtime data only; keeps all configuration/customization.
WORKING: frozenset[str] = frozenset({
    "conversations", "memory", "uploads", "plans",
    "usage", "event_runs",
    "processes", "schedules", "file_watchers", "skill_events",
})

# "Full factory reset" — everything, returning the profile to a fresh-provisioned
# baseline (no LLM configured, default persona/skills, only the auto 'main' channel).
FACTORY: frozenset[str] = frozenset(COMPONENTS)

VALID_SCOPES: tuple[str, ...] = ("custom", "working", "factory")


def expand_scope(scope: str, components: list[str] | None) -> set[str]:
    """Resolve a request scope into the concrete set of components to clean.

    - ``working`` / ``factory`` ignore ``components`` and return the preset set.
    - ``custom`` requires a non-empty ``components`` subset of :data:`COMPONENTS`.

    Raises :class:`ValueError` on an unknown scope, an unknown component name, or
    an empty custom selection — callers map that to an HTTP 400.
    """
    if scope == "working":
        return set(WORKING)
    if scope == "factory":
        return set(FACTORY)
    if scope == "custom":
        selected = set(components or [])
        if not selected:
            raise ValueError("custom scope requires at least one component")
        unknown = selected - set(COMPONENTS)
        if unknown:
            raise ValueError(f"unknown component(s): {', '.join(sorted(unknown))}")
        return selected
    raise ValueError(
        f"unknown scope '{scope}' (expected one of: {', '.join(VALID_SCOPES)})"
    )
