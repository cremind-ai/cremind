"""Per-profile tool configuration manager.

Wraps :class:`app.storage.tool_storage.ToolStorage` with helpers for the four
configuration scopes:

- ``arg``      Tool Arguments (JSON-Schema parameter values)
- ``variable`` Tool Variables (env-style secrets / required config)
- ``llm``      LLM Parameters (provider/model/full_reasoning, etc.)
- ``meta``     Free-form metadata (system_prompt, description override, ...)

Argument values are always JSON-serialised so non-string types round-trip.
"""

from __future__ import annotations

import json
from typing import Any

from app.storage.tool_storage import (
    SCOPE_ARG, SCOPE_LLM, SCOPE_META, SCOPE_VARIABLE,
    ToolStorage,
)


class ToolConfigManager:
    """Per-profile configuration accessor scoped by tool_id."""

    def __init__(self, tool_storage: ToolStorage):
        self._storage = tool_storage

    @property
    def storage(self) -> ToolStorage:
        return self._storage

    # ── arguments (JSON-serialised) ────────────────────────────────────

    def get_arguments(self, tool_id: str, profile: str) -> dict[str, Any]:
        raw = self._storage.get_scope(profile=profile, tool_id=tool_id, scope=SCOPE_ARG)
        out: dict[str, Any] = {}
        for key, value in raw.items():
            try:
                out[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                out[key] = value
        return out

    def set_arguments(self, tool_id: str, profile: str, arguments: dict[str, Any]) -> None:
        for key, value in arguments.items():
            stored = json.dumps(value) if not isinstance(value, str) else value
            self._storage.set_config(
                profile=profile, tool_id=tool_id, scope=SCOPE_ARG, key=key, value=stored,
            )

    # ── variables (env-style secrets) ──────────────────────────────────

    def get_variables(self, tool_id: str, profile: str, include_secrets: bool = False) -> dict[str, str]:
        return self._storage.get_scope(
            profile=profile, tool_id=tool_id, scope=SCOPE_VARIABLE,
            include_secrets=include_secrets,
        )

    def set_variable(
        self, tool_id: str, profile: str, key: str, value: str, is_secret: bool = False,
    ) -> None:
        self._storage.set_config(
            profile=profile, tool_id=tool_id, scope=SCOPE_VARIABLE,
            key=key, value=value, is_secret=is_secret,
        )

    def delete_variable(self, tool_id: str, profile: str, key: str) -> bool:
        return self._storage.delete_config(
            profile=profile, tool_id=tool_id, scope=SCOPE_VARIABLE, key=key,
        )

    # ── llm parameters ─────────────────────────────────────────────────

    def get_llm_params(self, tool_id: str, profile: str) -> dict[str, Any]:
        raw = self._storage.get_scope(profile=profile, tool_id=tool_id, scope=SCOPE_LLM)
        out: dict[str, Any] = {}
        for key, value in raw.items():
            if value in ("true", "false"):
                out[key] = value == "true"
            else:
                out[key] = value
        return out

    def set_llm_param(self, tool_id: str, profile: str, key: str, value: Any) -> None:
        if value is None or (not isinstance(value, bool) and value == ""):
            self._storage.delete_config(
                profile=profile, tool_id=tool_id, scope=SCOPE_LLM, key=key,
            )
            return
        if isinstance(value, bool):
            stored = "true" if value else "false"
        else:
            stored = str(value)
        self._storage.set_config(
            profile=profile, tool_id=tool_id, scope=SCOPE_LLM, key=key, value=stored,
        )

    # ── meta (system_prompt, description override, ...) ────────────────

    def get_meta(self, tool_id: str, profile: str) -> dict[str, str]:
        return self._storage.get_scope(profile=profile, tool_id=tool_id, scope=SCOPE_META)

    def set_meta(self, tool_id: str, profile: str, key: str, value: str) -> None:
        if value is None or value == "":
            self._storage.delete_config(
                profile=profile, tool_id=tool_id, scope=SCOPE_META, key=key,
            )
            return
        self._storage.set_config(
            profile=profile, tool_id=tool_id, scope=SCOPE_META, key=key, value=value,
        )

    # ── snapshot for the UI ────────────────────────────────────────────

    def snapshot(self, tool_id: str, profile: str) -> dict[str, Any]:
        """Return all config scopes for one tool, with secrets masked."""
        scopes = self._storage.get_all_scopes(
            profile=profile, tool_id=tool_id, include_secrets=False,
        )
        return {
            "arguments": self.get_arguments(tool_id, profile),
            "variables": scopes.get(SCOPE_VARIABLE, {}),
            "llm": self.get_llm_params(tool_id, profile),
            "meta": scopes.get(SCOPE_META, {}),
        }
