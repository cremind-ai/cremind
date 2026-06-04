"""Event-driven skill execution package.

The :class:`EventManager` watches ``<skill_dir>/events/<event_type>/`` folders
for new ``*.md`` files; each file is treated as one event payload. For every
:class:`SkillEventSubscription` matching that (skill, event_type) pair, the
file content is enqueued to a per-conversation worker that re-runs the
reasoning agent with ``history_messages=[]`` and persists the result.

The :class:`FileWatcherManager` is the parallel system for user-registered
filesystem watches: it mounts a watchdog ``Observer`` per (profile, root,
recursive) key, applies per-subscription filters (target_kind, event_types,
extensions), and feeds matching events into the same per-conversation queue.
"""

from app.events.file_watcher_manager import (
    FileWatcherManager,
    get_file_watcher_manager,
)
from app.events.manager import EventManager, get_event_manager
from app.events.notifications_buffer import EventNotificationsBuffer, get_event_notifications
from app.events.notifications_bus import NotificationsStreamBus, get_notifications_stream_bus
from app.events.stream_bus import ConversationStreamBus, get_event_stream_bus

__all__ = [
    "ConversationStreamBus",
    "EventManager",
    "EventNotificationsBuffer",
    "FileWatcherManager",
    "NotificationsStreamBus",
    "get_event_manager",
    "get_event_notifications",
    "get_event_stream_bus",
    "get_file_watcher_manager",
    "get_notifications_stream_bus",
]
