"""Who the agent may message without asking first.

Messaging a channel client is irreversible — it lands on a real person's phone —
so :mod:`app.channels.direct_send` shows the operator a preview and waits for
approval before it delivers. That is the right default and the wrong law: an
unattended automation has nobody to approve it, so a scheduled "message today's
new customers" job parks itself pending forever instead of running.

This module decides, per recipient, whether approval is still needed. Two levels,
most specific first:

1. **The client's own override** (``channel_senders.send_confirmation``) —
   ``"skip"`` to let the agent message them directly, ``"required"`` to keep
   asking even when the profile setting is off, ``NULL`` to inherit.
2. **The profile setting** (``channels.confirm_before_send``, default on).

One case ignores both and always asks: a recipient nobody has messaged before.
They have no client record, so there is nothing to have exempted them, and a
never-contacted phone number is the likeliest thing to be wrong in a
copy-pasted list — the one send you cannot take back is to a stranger.
"""

from __future__ import annotations

from typing import Any

# Values of ``channel_senders.send_confirmation``. ``NULL`` (absent) inherits.
CONFIRM_REQUIRED = "required"
CONFIRM_SKIP = "skip"
CONFIRM_VALUES = (CONFIRM_REQUIRED, CONFIRM_SKIP)


def confirm_before_send_default(profile: str) -> bool:
    """The profile's default: does the agent ask before messaging a client?

    Never raises and falls back to ``True`` — if the setting cannot be read, the
    safe answer is to ask.
    """
    if not profile:
        return True
    from app.config.user_config import confirm_before_send_enabled

    return confirm_before_send_enabled(profile)


def normalize_override(value: Any) -> str | None:
    """Coerce a stored/API override value to ``"required"``, ``"skip"`` or ``None``.

    ``None`` and the empty string both mean "inherit the profile setting", so
    clearing an override and never setting one are indistinguishable — which is
    what the caller wants.
    """
    text = str(value or "").strip().lower()
    if text in CONFIRM_VALUES:
        return text
    return None


def requires_confirmation(
    *, profile_default: bool, sender: dict[str, Any] | None, cold: bool,
) -> bool:
    """Whether this one recipient still needs the operator's approval.

    ``sender`` is the resolved client row (``None`` for someone with no record);
    ``cold`` marks a recipient this send would be contacting for the first time.
    """
    if cold or sender is None:
        return True
    override = normalize_override(sender.get("send_confirmation"))
    if override == CONFIRM_SKIP:
        return False
    if override == CONFIRM_REQUIRED:
        return True
    return profile_default


def describe(sender: dict[str, Any] | None, *, cold: bool) -> str:
    """Short reason a recipient needs approval, for the preview the agent shows."""
    if cold or sender is None:
        return "has never messaged this channel"
    if normalize_override(sender.get("send_confirmation")) == CONFIRM_REQUIRED:
        return "set to always ask"
    return "profile requires confirmation"
