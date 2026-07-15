"""Dependency bundle passed into the clean engine.

Mirrors :class:`app.blueprint.apply.Deps` — the route factory injects the shared
storage/registry singletons so the engine stays pure and testable. Components that
already have their own process-wide singleton (event/watcher/schedule/tool/autostart
storages) are resolved via ``app.storage.get_*`` inside the engine, so they are not
threaded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class Deps:
    """Everything :func:`app.reset.engine.run_clean` needs that isn't a singleton."""

    conversation_storage: Any                     # ConversationStorage (async)
    config_storage: Any                           # DynamicConfigStorage (sync)
    registry: Any = None                          # ToolRegistry | None
    drop_profile_embeddings: Optional[Callable[[str], None]] = None
    document_service: Any = None                  # DocumentSyncService | None
