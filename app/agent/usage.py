"""Per-invocation token attribution for a single agent turn.

Today the reasoning agent sums every LLM call's four-way token usage into one
aggregate blob per turn, so the contribution of the main Reasoning Agent vs. each
child sub-agent/tool is lost. :class:`UsageRecord` is the small structured unit
that lets us attribute each call to its source (reasoning step vs. a specific
tool/sub-agent) along with the provider/model that produced it. The reasoning
agent appends one record at the *same* sites it already increments its running
totals, so the sum of the records equals the existing aggregate by construction
(:func:`reconcile` asserts this — no double counting, no loss).

These records ride the terminal stream chunk to the runner, which freezes their
cost and persists them to ``usage_records`` keyed by the turn's conversation +
message ids.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

# Cost is computed later from raw tokens; this module stays pricing-agnostic.
_TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


@dataclass(frozen=True)
class UsageRecord:
    """One LLM invocation's attributed four-way token usage within a turn."""

    source_kind: str                 # reasoning | tool | subagent | intrinsic
    tool_id: Optional[str]           # None for source_kind == "reasoning"
    label: Optional[str]             # model_label for reasoning; tool.name otherwise
    provider: Optional[str]
    model: Optional[str]
    model_group: Optional[str]
    step_index: int
    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def reconcile(records: list, totals: dict) -> bool:
    """Return True iff the records sum exactly to the turn's aggregate totals.

    Accepts ``UsageRecord`` instances or their serialized dicts (the runner sees
    dicts off the stream chunk). A safety check, not a hard gate — the caller
    logs a warning on mismatch rather than failing the turn over an accounting
    discrepancy.
    """
    def field_of(rec, field: str) -> int:
        value = rec.get(field) if isinstance(rec, dict) else getattr(rec, field, 0)
        return int(value or 0)

    for field in _TOKEN_FIELDS:
        summed = sum(field_of(r, field) for r in records)
        if summed != int(totals.get(field) or 0):
            return False
    return True
