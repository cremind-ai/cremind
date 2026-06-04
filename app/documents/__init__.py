"""Documentation subsystem for the Documentation Search built-in tool.

Two on-disk roots are watched:

- ``<CREMIND_SYSTEM_DIR>/documents``                     -- shared docs
- ``<CREMIND_SYSTEM_DIR>/<profile>/documents``           -- per-profile docs

Markdown files in either tree must declare a YAML-frontmatter ``description``
field. Only the description is embedded into the Qdrant collection used by
the ``documentation_search`` built-in tool; the body is read from disk on
demand at query time.
"""

from __future__ import annotations

from typing import Optional

from app.documents.sync import DocumentSyncService

_service: Optional[DocumentSyncService] = None


def set_service(service: Optional[DocumentSyncService]) -> None:
    """Register (or clear) the global :class:`DocumentSyncService` singleton."""
    global _service
    _service = service


def get_service() -> Optional[DocumentSyncService]:
    """Return the singleton :class:`DocumentSyncService`, or None if not initialized."""
    return _service


__all__ = ["DocumentSyncService", "set_service", "get_service"]
