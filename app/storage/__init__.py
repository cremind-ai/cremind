"""Storage singletons.

Each ``get_*_storage`` returns a process-wide instance bound to the active
:class:`app.databases.DatabaseProvider`. Storage classes obtain their engines
from the provider on demand, so swapping the provider (e.g. when the setup
wizard switches from SQLite to Postgres) only requires clearing the cached
instances and re-fetching them.
"""

from app.databases import DatabaseProvider, get_database_provider
from app.storage.autostart_storage import AutostartStorage
from app.storage.conversation_storage import ConversationStorage
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.storage.event_subscription_storage import EventSubscriptionStorage
from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage
from app.storage.schedule_event_storage import ScheduleEventSubscriptionStorage
from app.storage.event_run_storage import EventRunStorage, get_event_run_storage
from app.storage.memory_storage import MemoryStorage
from app.storage.usage_storage import UsageStorage
from app.storage.tool_storage import ToolStorage, get_tool_storage

_instance: ConversationStorage | None = None
_dynamic_config_instance: DynamicConfigStorage | None = None
_autostart_instance: AutostartStorage | None = None
_event_subscription_instance: EventSubscriptionStorage | None = None
_file_watcher_instance: FileWatcherSubscriptionStorage | None = None
_schedule_event_instance: ScheduleEventSubscriptionStorage | None = None
_memory_instance: MemoryStorage | None = None
_usage_instance: UsageStorage | None = None


def get_conversation_storage(provider: DatabaseProvider | None = None) -> ConversationStorage:
    global _instance
    if _instance is None:
        _instance = ConversationStorage(provider)
    return _instance


def get_memory_storage(provider: DatabaseProvider | None = None) -> MemoryStorage:
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = MemoryStorage(provider)
    return _memory_instance


def get_usage_storage(provider: DatabaseProvider | None = None) -> UsageStorage:
    global _usage_instance
    if _usage_instance is None:
        _usage_instance = UsageStorage(provider)
    return _usage_instance


def get_dynamic_config_storage(provider: DatabaseProvider | None = None) -> DynamicConfigStorage:
    global _dynamic_config_instance
    if _dynamic_config_instance is None:
        _dynamic_config_instance = DynamicConfigStorage(provider)
    return _dynamic_config_instance


def get_autostart_storage(provider: DatabaseProvider | None = None) -> AutostartStorage:
    global _autostart_instance
    if _autostart_instance is None:
        _autostart_instance = AutostartStorage(provider)
    return _autostart_instance


def get_event_subscription_storage(provider: DatabaseProvider | None = None) -> EventSubscriptionStorage:
    global _event_subscription_instance
    if _event_subscription_instance is None:
        _event_subscription_instance = EventSubscriptionStorage(provider)
    return _event_subscription_instance


def get_file_watcher_storage(provider: DatabaseProvider | None = None) -> FileWatcherSubscriptionStorage:
    global _file_watcher_instance
    if _file_watcher_instance is None:
        _file_watcher_instance = FileWatcherSubscriptionStorage(provider)
    return _file_watcher_instance


def get_schedule_event_storage(provider: DatabaseProvider | None = None) -> ScheduleEventSubscriptionStorage:
    global _schedule_event_instance
    if _schedule_event_instance is None:
        _schedule_event_instance = ScheduleEventSubscriptionStorage(provider)
    return _schedule_event_instance


def invalidate_storage_singletons() -> None:
    """Drop every cached storage instance.

    Called by the setup wizard right after writing a new bootstrap.toml so
    the next ``get_*_storage()`` call rebuilds against the freshly-installed
    provider. Safe to call any time — re-resolution is lazy.
    """
    global _instance, _dynamic_config_instance, _autostart_instance
    global _event_subscription_instance, _file_watcher_instance, _memory_instance
    global _schedule_event_instance, _usage_instance
    _instance = None
    _dynamic_config_instance = None
    _autostart_instance = None
    _event_subscription_instance = None
    _file_watcher_instance = None
    _schedule_event_instance = None
    _memory_instance = None
    _usage_instance = None

    # Reach into the storage modules that hold their own singletons to drop
    # them too — otherwise they'd hold engines pointing at the old DB.
    from app.storage.tool_storage import _reset_tool_storage_singleton
    _reset_tool_storage_singleton()
    import app.storage.event_run_storage as _ers
    _ers._instance = None
    try:
        from app.utils.client_storage import _reset_auth_client_storage_singleton
        _reset_auth_client_storage_singleton()
    except ImportError:
        pass


__all__ = [
    "AutostartStorage",
    "ConversationStorage",
    "DynamicConfigStorage",
    "EventSubscriptionStorage",
    "FileWatcherSubscriptionStorage",
    "ScheduleEventSubscriptionStorage",
    "EventRunStorage",
    "MemoryStorage",
    "UsageStorage",
    "ToolStorage",
    "get_autostart_storage",
    "get_conversation_storage",
    "get_dynamic_config_storage",
    "get_event_subscription_storage",
    "get_file_watcher_storage",
    "get_schedule_event_storage",
    "get_event_run_storage",
    "get_memory_storage",
    "get_usage_storage",
    "get_tool_storage",
    "invalidate_storage_singletons",
]
