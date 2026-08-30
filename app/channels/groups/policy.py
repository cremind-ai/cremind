"""A channel group's settings blob, and who the agent is allowed to answer.

Stored as JSON on ``channel_groups.settings`` so adding a knob needs no
migration. :func:`normalize_settings` is strict — it raises :class:`ValueError`
with a message the API turns into a 400 — because these values decide who the
agent talks to in a room full of real people. A malformed member policy that was
quietly dropped would look exactly like "the agent ignored my block", which is a
bad way to find out about a typo.

The shape::

    {
      "member_policy": {"mode": "everyone", "allow": [], "deny": []},
      "respond_mode": "mention_or_relevant",
      "max_agent_posts_per_minute": 20,
      "max_consecutive_bot_messages": 8
    }

Both lists are kept whichever mode is active, so switching to ``selected`` and
back does not lose the deny list somebody curated.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.channels.groups.constants import (
    DEFAULT_MAX_AGENT_POSTS_PER_MINUTE,
    DEFAULT_MAX_CONSECUTIVE_BOT_MESSAGES,
    POLICY_EVERYONE,
    POLICY_MODES,
    POLICY_SELECTED,
    RESPOND_MENTION_OR_RELEVANT,
    RESPOND_MODES,
)
from app.channels.groups.keys import candidate_ids, ids_overlap

_MAX_POSTS_CEILING = 600
_MAX_BOT_STREAK_CEILING = 1000


def default_settings() -> Dict[str, Any]:
    return {
        "member_policy": {"mode": POLICY_EVERYONE, "allow": [], "deny": []},
        "respond_mode": RESPOND_MENTION_OR_RELEVANT,
        "max_agent_posts_per_minute": DEFAULT_MAX_AGENT_POSTS_PER_MINUTE,
        "max_consecutive_bot_messages": DEFAULT_MAX_CONSECUTIVE_BOT_MESSAGES,
    }


def normalize_settings(raw: Any) -> Dict[str, Any]:
    """Validate and fill in a settings blob. Raises ``ValueError`` when unusable."""
    if raw is None:
        return default_settings()
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")

    out = default_settings()

    if raw.get("member_policy") is not None:
        policy = raw["member_policy"]
        if not isinstance(policy, dict):
            raise ValueError("member_policy must be an object")
        mode = str(policy.get("mode") or POLICY_EVERYONE).strip().lower()
        if mode not in POLICY_MODES:
            raise ValueError(
                f"member_policy.mode must be one of: {', '.join(POLICY_MODES)}"
            )
        out["member_policy"] = {
            "mode": mode,
            "allow": _id_list(policy.get("allow"), "member_policy.allow"),
            "deny": _id_list(policy.get("deny"), "member_policy.deny"),
        }

    if raw.get("respond_mode") is not None:
        mode = str(raw["respond_mode"]).strip().lower()
        if mode not in RESPOND_MODES:
            raise ValueError(
                f"respond_mode must be one of: {', '.join(RESPOND_MODES)}"
            )
        out["respond_mode"] = mode

    if raw.get("max_agent_posts_per_minute") is not None:
        out["max_agent_posts_per_minute"] = _positive_int(
            raw["max_agent_posts_per_minute"],
            "max_agent_posts_per_minute",
            _MAX_POSTS_CEILING,
        )

    if raw.get("max_consecutive_bot_messages") is not None:
        out["max_consecutive_bot_messages"] = _positive_int(
            raw["max_consecutive_bot_messages"],
            "max_consecutive_bot_messages",
            _MAX_BOT_STREAK_CEILING,
        )

    return out


def merge_settings(current: Any, patch: Any) -> Dict[str, Any]:
    """Apply a partial settings patch to a stored blob, then normalise.

    One level deep, and only for ``member_policy`` — a client toggling
    ``respond_mode`` should not have to resend the allow list, but a client that
    sends ``member_policy`` at all sends the whole object (the two lists are
    edited together in the UI, and merging them per-key makes "remove the last
    denied member" impossible to express).
    """
    base = normalize_settings(current)
    if patch is None:
        return base
    if not isinstance(patch, dict):
        raise ValueError("settings must be an object")
    merged = {**base, **{k: v for k, v in patch.items() if v is not None}}
    return normalize_settings(merged)


def _id_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    out: List[str] = []
    for entry in value:
        # Platform ids arrive as ints from some clients; every comparison
        # downstream is on strings, so settle it here.
        text = str(entry or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _positive_int(value: Any, field: str, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number") from None
    if number < 0:
        raise ValueError(f"{field} cannot be negative")
    if number > ceiling:
        raise ValueError(f"{field} must be {ceiling} or less")
    return number


# ── reads ─────────────────────────────────────────────────────────────────


def member_allowed(
    settings: Optional[Dict[str, Any]],
    sender_id: str,
    alt_ids: Optional[Sequence[str]] = None,
) -> bool:
    """Whether the agent may answer this account.

    Every id the sender is seen under is tried, because a platform can report
    one account under several and the operator pasted whichever one they had in
    front of them. An empty ``selected`` allow list answers nobody — which is
    what "only these people" means when the list is empty, and is recoverable in
    one click, unlike the alternative reading.
    """
    policy = (settings or {}).get("member_policy") or {}
    mode = str(policy.get("mode") or POLICY_EVERYONE).lower()
    ids = candidate_ids(sender_id, alt_ids)
    if not ids:
        return False
    if mode == POLICY_SELECTED:
        return ids_overlap(policy.get("allow") or (), ids)
    return not ids_overlap(policy.get("deny") or (), ids)


def responds_without_mention(settings: Optional[Dict[str, Any]]) -> bool:
    return str(
        (settings or {}).get("respond_mode") or RESPOND_MENTION_OR_RELEVANT
    ) != "mention_only"


def max_agent_posts_per_minute(settings: Optional[Dict[str, Any]]) -> int:
    value = (settings or {}).get("max_agent_posts_per_minute")
    return DEFAULT_MAX_AGENT_POSTS_PER_MINUTE if value is None else int(value)


def max_consecutive_bot_messages(settings: Optional[Dict[str, Any]]) -> int:
    value = (settings or {}).get("max_consecutive_bot_messages")
    return (
        DEFAULT_MAX_CONSECUTIVE_BOT_MESSAGES if value is None else int(value)
    )


def member_responds(
    settings: Optional[Dict[str, Any]], member: Dict[str, Any],
) -> bool:
    """The same question as :func:`member_allowed`, asked of a stored row.

    Used by the API to decorate the member list the UI renders toggles from, so
    the switch and the runtime gate can never disagree.
    """
    return member_allowed(
        settings, str(member.get("member_id") or ""), member.get("alt_ids") or (),
    )
