"""Per-profile general application configuration accessor.

Reads follow this priority chain:

1. SQLite ``user_config`` row for the profile (UI/CLI-driven override).
2. TOML default at the dotted path declared in :mod:`app.config.config_schema`.

The TOML at ``app/config/settings.toml`` is the single source of truth for
defaults. A schema field with no matching TOML entry is a configuration bug
and is rejected at import time by the startup validator below.

Returned values are coerced to the type declared in the schema, so consumers
can rely on ``int``/``float``/``bool``/``str`` instead of stringly-typed
SQLite output.

Resolver helpers (e.g. :func:`resolve_agent_config`) batch-read every key in
a group into a frozen dataclass — the right shape for stable references in
long-lived objects like :class:`ReasoningAgent`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config.config_schema import CONFIG_SCHEMA, Field, all_keys, lookup
from app.config.settings import get_dynamic

_SETTINGS_TOML = Path(__file__).resolve().parent / "settings.toml"
with _SETTINGS_TOML.open("rb") as _f:
    _TOML_DEFAULTS: dict = tomllib.load(_f)


def _toml_default(field: Field) -> Any:
    """Resolve the TOML value at ``field.default_toml``. Raises ``KeyError`` if missing."""
    node: Any = _TOML_DEFAULTS
    for part in field.default_toml.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(
                f"settings.toml is missing required default '{field.default_toml}'"
            )
        node = node[part]
    return node


def _validate_toml_defaults() -> None:
    missing: list[str] = []
    for key in all_keys():
        _, _, field = lookup(key)
        try:
            _toml_default(field)
        except KeyError as exc:
            missing.append(str(exc).strip("'"))
    if missing:
        raise RuntimeError(
            "Schema/TOML mismatch — settings.toml is missing defaults for:\n  "
            + "\n  ".join(missing)
        )


_validate_toml_defaults()


def get_user_config(key: str, profile: str) -> Any:
    """Return the effective value for ``key`` on ``profile``.

    Raises ``KeyError`` if ``key`` is not declared in ``CONFIG_SCHEMA``.
    """
    _, _, field = lookup(key)
    raw = get_dynamic("user_config", key, profile=profile)
    if raw is None:
        raw = _toml_default(field)
    return field.coerce(raw)


def resolve_default(key: str) -> Any:
    """Return the TOML default for ``key`` (no per-profile lookup)."""
    _, _, field = lookup(key)
    return field.coerce(_toml_default(field))


def resolve_group(group_name: str, profile: str) -> dict[str, Any]:
    """Read every field in ``group_name`` for ``profile`` in one pass."""
    group = CONFIG_SCHEMA[group_name]
    return {
        field_name: get_user_config(f"{group_name}.{field_name}", profile)
        for field_name in group.fields
    }


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Snapshot of agent + history config for one ReAct run."""

    max_steps: int
    max_llm_retries: int
    reasoning_temperature: float
    reasoning_max_tokens: int
    reasoning_retry: int
    steps_length: int
    history_max_tokens_total: int
    history_max_tokens_per_message: int
    tool_result_enabled: bool
    tool_result_max_tokens: int
    tool_result_preserve_recent: int
    tool_result_head_tokens: int
    tool_result_tail_tokens: int


def resolve_agent_config(profile: str) -> AgentRuntimeConfig:
    """Build an :class:`AgentRuntimeConfig` for ``profile``."""
    agent = resolve_group("agent", profile)
    history = resolve_group("history", profile)
    tool_result = resolve_group("tool_result", profile)
    return AgentRuntimeConfig(
        max_steps=int(agent["max_steps"]),
        max_llm_retries=int(agent["max_llm_retries"]),
        reasoning_temperature=float(agent["reasoning_temperature"]),
        reasoning_max_tokens=int(agent["reasoning_max_tokens"]),
        reasoning_retry=int(agent["reasoning_retry"]),
        steps_length=int(agent["steps_length"]),
        history_max_tokens_total=int(history["max_tokens_total"]),
        history_max_tokens_per_message=int(history["max_tokens_per_message"]),
        tool_result_enabled=bool(tool_result["enabled"]),
        tool_result_max_tokens=int(tool_result["max_tokens"]),
        tool_result_preserve_recent=int(tool_result["preserve_recent"]),
        tool_result_head_tokens=int(tool_result["head_tokens"]),
        tool_result_tail_tokens=int(tool_result["tail_tokens"]),
    )


@dataclass(frozen=True)
class SkillClassifierConfig:
    temperature: float
    max_tokens: int
    retry: int


def resolve_skill_classifier_config(profile: str) -> SkillClassifierConfig:
    values = resolve_group("skill_classifier", profile)
    return SkillClassifierConfig(
        temperature=float(values["temperature"]),
        max_tokens=int(values["max_tokens"]),
        retry=int(values["retry"]),
    )


@dataclass(frozen=True)
class SummarizerConfig:
    temperature: float
    max_tokens: int
    retry: int


def resolve_summarizer_config(profile: str) -> SummarizerConfig:
    values = resolve_group("summarizer", profile)
    return SummarizerConfig(
        temperature=float(values["temperature"]),
        max_tokens=int(values["max_tokens"]),
        retry=int(values["retry"]),
    )
