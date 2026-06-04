from threading import RLock
from typing import Any, ClassVar, Dict, Optional


class ContextStorage:
    """Thread-safe in-memory storage for context-scoped data."""

    _instance: ClassVar[Optional["ContextStorage"]] = None
    _instance_lock: ClassVar[RLock] = RLock()
    _store: Dict[str, Dict[str, Any]]
    _lock: RLock

    def __new__(cls) -> "ContextStorage":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._store = {}
                cls._instance._lock = RLock()
        return cls._instance

    @classmethod
    def instance(cls) -> "ContextStorage":
        return cls()

    def set(self, context_id: str, key: str, value: Any) -> None:
        with self._lock:
            scoped = self._store.setdefault(context_id, {})
            scoped[key] = value

    def get(self, context_id: str, key: str, default: Any | None = None) -> Any:
        with self._lock:
            return self._store.get(context_id, {}).get(key, default)

    def clear(self, context_id: str, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._store.pop(context_id, None)
                return

            scoped = self._store.get(context_id)
            if scoped is None:
                return

            scoped.pop(key, None)
            if not scoped:
                self._store.pop(context_id, None)


def _storage() -> ContextStorage:
    return ContextStorage.instance()


def set_context(context_id: str, key: str, value: Any) -> None:
    """Persist a value for the given context identifier and key."""
    if not context_id:
        return
    _storage().set(context_id, key, value)


def get_context(context_id: str, key: str, default: Any | None = None) -> Any:
    """Retrieve a stored value for the given context identifier and key."""
    if not context_id:
        return default
    return _storage().get(context_id, key, default)


def clear_context(context_id: str, key: str | None = None) -> None:
    """Remove a stored value for the given context identifier and key."""
    if not context_id:
        return
    _storage().clear(context_id, key)
