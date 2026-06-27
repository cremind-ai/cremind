"""Declarative schema for the Settings → Config page.

Each :class:`Field` describes one user-tunable runtime setting: its primitive
type, the dotted TOML path of its default, and optional validation hints
(``min``/``max``/``step``, ``enum``). Fields are grouped into
:class:`ConfigGroup`s which become the sections in the UI.

Stored values live in the per-profile ``user_config`` SQLite table. Reads
follow the priority chain: SQLite override > ``settings.toml`` default. The
TOML is the single source of truth for defaults — a missing TOML entry for
a declared field is a hard error at startup.

Adding a new tunable knob is a 3-step process:
  1. Add an entry to ``CONFIG_SCHEMA`` here, in the appropriate group.
  2. Add the matching default under the same dotted path in ``settings.toml``.
  3. Replace the hardcoded literal at the consumption site with
     ``get_user_config("group.key", profile=...)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Literal

FieldType = Literal["number", "string", "boolean", "enum"]


@dataclass(frozen=True)
class Field:
    """Schema for one configurable value."""

    type: FieldType
    default_toml: str
    label: str | None = None
    description: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    enum: tuple[str, ...] | None = None

    def coerce(self, raw: Any) -> Any:
        """Convert a raw stored value (string from SQLite, or native from TOML) to the declared type."""
        if raw is None:
            return None
        if self.type == "boolean":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("true", "1", "yes", "on")
        if self.type == "number":
            if isinstance(raw, bool):
                return int(raw)
            if isinstance(raw, int):
                return raw
            if isinstance(raw, float):
                return raw
            text = str(raw).strip()
            try:
                if "." in text or "e" in text.lower():
                    return float(text)
                return int(text)
            except ValueError as exc:
                raise ValueError(f"Cannot parse {text!r} as number") from exc
        if self.type == "enum":
            text = str(raw)
            if self.enum and text not in self.enum:
                raise ValueError(f"Value {text!r} not in enum {self.enum}")
            return text
        return str(raw)

    def validate(self, value: Any) -> None:
        """Raise ``ValueError`` if ``value`` violates declared bounds or enum."""
        if self.type == "number":
            if self.min is not None and value < self.min:
                raise ValueError(f"Value {value} below minimum {self.min}")
            if self.max is not None and value > self.max:
                raise ValueError(f"Value {value} above maximum {self.max}")
        elif self.type == "enum":
            if self.enum and value not in self.enum:
                raise ValueError(f"Value {value!r} not in enum {self.enum}")


@dataclass(frozen=True)
class ConfigGroup:
    """A logical grouping of fields (one card/section in the UI)."""

    label: str
    description: str
    fields: dict[str, Field] = dataclass_field(default_factory=dict)


CONFIG_SCHEMA: dict[str, ConfigGroup] = {
    "agent": ConfigGroup(
        label="Reasoning Agent",
        description="Controls the agent loop's iteration limits and per-call LLM parameters.",
        fields={
            "max_steps": Field(
                type="number", default_toml="agent.max_steps",
                label="Max steps",
                description="Maximum tool-calling iterations before the agent stops a turn.",
                min=1, max=500,
            ),
            "max_llm_retries": Field(
                type="number", default_toml="agent.max_llm_retries",
                label="Max LLM retries",
                description="How many times the loop retries after an LLM error before giving up.",
                min=0, max=10,
            ),
            "reasoning_temperature": Field(
                type="number", default_toml="agent.reasoning_temperature",
                label="Reasoning temperature",
                description="Sampling temperature for the main reasoning LLM call.",
                min=0, max=2, step=0.1,
            ),
            "reasoning_max_tokens": Field(
                type="number", default_toml="agent.reasoning_max_tokens",
                label="Reasoning max tokens",
                description="Output token cap for the reasoning LLM call.",
                min=256, max=131072,
            ),
            "reasoning_retry": Field(
                type="number", default_toml="agent.reasoning_retry",
                label="Per-call retry count",
                description="How many times an individual reasoning LLM call retries on transient errors.",
                min=0, max=10,
            ),
            "steps_length": Field(
                type="number", default_toml="agent.steps_length",
                label="Steps history length",
                description="Maximum number of recent ReAct step entries kept in the prompt context. Older entries are dropped once this is exceeded.",
                min=5, max=500,
            ),
            "enable_prompt_cache": Field(
                type="boolean", default_toml="agent.enable_prompt_cache",
                label="Prompt caching",
                description="Reuse the cached system+tools prefix across reasoning steps to cut input tokens. Anthropic uses explicit cache markers; OpenAI-family providers cache automatically. Harmless on providers without cache support.",
            ),
            "replay_reasoning_steps": Field(
                type="boolean", default_toml="agent.replay_reasoning_steps",
                label="Replay reasoning steps",
                description="Send each prior turn's full tool-call/tool-result trace back into history (not just the final answer), so the model resumes the real transcript and the cached prefix covers the reasoning. Larger prompts — cheap on Anthropic (cached), but extra input tokens on providers without caching.",
            ),
        },
    ),
    "compaction": ConfigGroup(
        label="Conversation Compaction",
        description=(
            "Keeps long conversations within budget by folding the oldest turns "
            "into a running summary (via the low model group) while recent turns "
            "stay verbatim. Replaces fixed token-window truncation and is "
            "prompt-cache friendly — the summary at the front stays byte-stable "
            "between compactions."
        ),
        fields={
            "enabled": Field(
                type="boolean", default_toml="compaction.enabled",
                label="Enabled",
                description="When off, full history is sent (bounded only by the model's context window).",
            ),
            "compact_threshold_percent": Field(
                type="number", default_toml="compaction.compact_threshold_percent",
                label="Compaction threshold (% of context window)",
                description="Suggest folding the oldest turns once the model's reported context reaches this percentage of its context window. Lower it to compact earlier.",
                min=10, max=100, step=5,
            ),
            "keep_recent_tokens": Field(
                type="number", default_toml="compaction.keep_recent_tokens",
                label="Keep-recent target (tokens)",
                description="After a compaction, keep about this many tokens of recent turns verbatim. The gap below the threshold is the hysteresis band that keeps the cached summary stable across turns.",
                min=500, max=500000,
            ),
            "keep_recent_messages": Field(
                type="number", default_toml="compaction.keep_recent_messages",
                label="Keep-recent messages (floor)",
                description="Never fold below this many of the most recent messages, even if the tail is over the keep-recent target.",
                min=0, max=50,
            ),
            "temperature": Field(
                type="number", default_toml="compaction.temperature",
                label="Temperature",
                description="Sampling temperature for the summarization call; keep low for consistency.",
                min=0, max=2, step=0.1,
            ),
            "max_tokens": Field(
                type="number", default_toml="compaction.max_tokens",
                label="Max tokens",
                description="Output token cap for the running summary (also its hard size bound).",
                min=128, max=8192,
            ),
            "retry": Field(
                type="number", default_toml="compaction.retry",
                label="Retry count",
                description="Retries on transient summarization LLM errors.",
                min=0, max=10,
            ),
        },
    ),
    "tool_result": ConfigGroup(
        label="Tool Result Truncation",
        description="Limits applied to tool observations when re-sent to the reasoning LLM. The full result is always stored in the database and shown in the web UI.",
        fields={
            "enabled": Field(
                type="boolean", default_toml="tool_result.enabled",
                label="Enabled",
                description="When on, older tool observations are shortened to a head/tail excerpt before being included in the next reasoning prompt.",
            ),
            "max_tokens": Field(
                type="number", default_toml="tool_result.max_tokens",
                label="Per-observation token threshold",
                description="An older observation longer than this many tokens is replaced with a head excerpt + truncation marker + tail excerpt.",
                min=100, max=200000,
            ),
            "preserve_recent": Field(
                type="number", default_toml="tool_result.preserve_recent",
                label="Recent observations kept full",
                description="The N most recent observations always pass through at full length, regardless of size.",
                min=0, max=10,
            ),
            "head_tokens": Field(
                type="number", default_toml="tool_result.head_tokens",
                label="Head excerpt tokens",
                description="Tokens kept from the beginning of a truncated observation.",
                min=0, max=10000,
            ),
            "tail_tokens": Field(
                type="number", default_toml="tool_result.tail_tokens",
                label="Tail excerpt tokens",
                description="Tokens kept from the end of a truncated observation.",
                min=0, max=10000,
            ),
        },
    ),
    "skill_classifier": ConfigGroup(
        label="Skill Classifier",
        description="Lightweight LLM that decides whether a request maps to a registered skill.",
        fields={
            "temperature": Field(
                type="number", default_toml="skill_classifier.temperature",
                label="Temperature",
                description="Sampling temperature; keep low for deterministic classification.",
                min=0, max=2, step=0.1,
            ),
            "max_tokens": Field(
                type="number", default_toml="skill_classifier.max_tokens",
                label="Max tokens",
                description="Output token cap for the classifier call.",
                min=8, max=2048,
            ),
            "retry": Field(
                type="number", default_toml="skill_classifier.retry",
                label="Retry count",
                description="Retries on transient classifier LLM errors.",
                min=0, max=10,
            ),
        },
    ),
    "memory": ConfigGroup(
        label="Memory",
        description=(
            "Lets the agent recall durable, long-term facts about the user across "
            "conversations. Long-term memory is extracted together with the "
            "conversation summary at the compaction fold (requires Compaction "
            "enabled). When Vector Embedding is on, facts are stored in the vector "
            "store and retrieved by relevance; otherwise they live in a small "
            "size-capped queue. Off by default."
        ),
        fields={
            "enabled": Field(
                type="boolean", default_toml="memory.enabled",
                label="Enabled",
                description="Master switch for long-term memory. Requires Compaction enabled (memory is generated at the compaction fold). When off, the agent reads and writes no long-term memory.",
            ),
            "long_term_queue_size": Field(
                type="number", default_toml="memory.long_term_queue_size",
                label="Long-term queue size",
                description="Max long-term facts kept per profile in the DB queue (Vector-Embedding-OFF mode). Oldest is dropped on overflow. Ignored in vector mode (unlimited).",
                min=1, max=100,
            ),
            "long_term_max_tokens": Field(
                type="number", default_toml="memory.long_term_max_tokens",
                label="Long-term entry max tokens",
                description="Each long-term fact is clipped to at most this many tokens.",
                min=10, max=500,
            ),
            "long_term_retrieve_limit": Field(
                type="number", default_toml="memory.long_term_retrieve_limit",
                label="Long-term retrieval limit",
                description="Top-K long-term facts retrieved from the vector store for the prompt (Vector-Embedding-ON mode).",
                min=1, max=50,
            ),
        },
    ),
}


def lookup(key: str) -> tuple[str, str, Field]:
    """Resolve a dotted ``group.field`` key to ``(group_name, field_name, Field)``.

    Raises ``KeyError`` if the group or field is unknown.
    """
    if "." not in key:
        raise KeyError(f"Config key must be of the form 'group.field', got {key!r}")
    group_name, field_name = key.split(".", 1)
    group = CONFIG_SCHEMA.get(group_name)
    if group is None:
        raise KeyError(f"Unknown config group: {group_name!r}")
    field = group.fields.get(field_name)
    if field is None:
        raise KeyError(f"Unknown config field: {key!r}")
    return group_name, field_name, field


def all_keys() -> list[str]:
    """Every dotted ``group.field`` key declared in the schema."""
    return [
        f"{group_name}.{field_name}"
        for group_name, group in CONFIG_SCHEMA.items()
        for field_name in group.fields
    ]
