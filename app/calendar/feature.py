"""Per-profile Calendar & Schedule feature flag.

The switch on the *Calendar & Schedule* page. When OFF (the default), the
``scheduler`` tool stays parser-only (today's behavior) and no schedule events
fire; when ON, the calendar/schedule action subtools are exposed and the
``ScheduleManager`` arms the profile's events.

Persisted as a ``meta``-scope key on the ``scheduler`` tool_id via the existing
:class:`app.tools.config_manager.ToolConfigManager`, so it travels with the rest
of the tool configuration (no new table). Read/write go through the active
database provider's :class:`ToolStorage`, exactly like every other tool setting.
"""

from __future__ import annotations

from app.tools.config_manager import ToolConfigManager
from app.storage.tool_storage import get_tool_storage

# slugify("Scheduler") == "scheduler" — the tool_id the registry assigns to the
# scheduler built-in group (see register_builtin_tools).
SCHEDULER_TOOL_ID = "scheduler"
FEATURE_KEY = "calendar_schedule_enabled"


def _manager() -> ToolConfigManager:
    return ToolConfigManager(get_tool_storage())


def is_enabled(profile: str) -> bool:
    """True iff the Calendar & Schedule feature is enabled for ``profile``.

    Default is **ON**: an unset flag reads as enabled (the feature ships on, using
    the internal/system calendar). Only an explicit ``"false"`` opts out.
    """
    if not profile:
        return False
    try:
        meta = _manager().get_meta(SCHEDULER_TOOL_ID, profile)
    except Exception:  # noqa: BLE001
        # Storage not ready / lookup failed: fall back to the default-on stance.
        return True
    return str(meta.get(FEATURE_KEY, "true")).lower() != "false"


def set_enabled(profile: str, value: bool) -> None:
    """Enable/disable the feature for ``profile`` (persisted)."""
    # ``set_meta`` deletes the key on an empty string, so persist the explicit
    # literal "false" rather than "" to keep an OFF state distinguishable from
    # "never configured" (both read as disabled, but the row makes intent clear).
    _manager().set_meta(SCHEDULER_TOOL_ID, profile, FEATURE_KEY, "true" if value else "false")
