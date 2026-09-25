"""Documentation subsystem for the Cremind Documentation Search built-in tool.

Two on-disk roots are watched:

- ``<CREMIND_SYSTEM_DIR>/documents``                     -- shared docs
- ``<CREMIND_SYSTEM_DIR>/<profile>/documents``           -- per-profile docs

Markdown files in either tree must declare a YAML-frontmatter ``description``
field. Only the description is embedded into the Qdrant collection used by
the ``cremind_documentation_search`` built-in tool; the body is read from disk on
demand at query time.
"""

from __future__ import annotations

from typing import Optional

from app.cremind_documents.sync import CremindDocumentSyncService

_service: Optional[CremindDocumentSyncService] = None


def set_service(service: Optional[CremindDocumentSyncService]) -> None:
    """Register (or clear) the global :class:`CremindDocumentSyncService` singleton."""
    global _service
    _service = service


def get_service() -> Optional[CremindDocumentSyncService]:
    """Return the singleton :class:`CremindDocumentSyncService`, or None if not initialized."""
    return _service


__all__ = ["CremindDocumentSyncService", "set_service", "get_service"]
