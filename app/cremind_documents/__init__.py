"""Documentation subsystem for the Cremind Documentation Search built-in tool.

Two on-disk roots are watched (see :mod:`app.cremind_documents.paths`):

- ``<CREMIND_SYSTEM_DIR>/storage/cremind_documents/shared``          -- shared docs
- ``<CREMIND_SYSTEM_DIR>/storage/cremind_documents/profiles/<uuid>`` -- per-profile docs

Markdown files in either tree must declare a YAML-frontmatter ``description``
field. Only the description is embedded into the collection used by the
``cremind_documentation_search`` built-in tool; the body is read from disk on
demand at query time. Installs from before this layout are moved into it at
boot by :mod:`app.documents.relocate`.
"""

from __future__ import annotations

from typing import Optional

from app.cremind_documents.sync import CremindDocumentSyncService, remove_profile_documents

_service: Optional[CremindDocumentSyncService] = None


def set_service(service: Optional[CremindDocumentSyncService]) -> None:
    """Register (or clear) the global :class:`CremindDocumentSyncService` singleton."""
    global _service
    _service = service


def get_service() -> Optional[CremindDocumentSyncService]:
    """Return the singleton :class:`CremindDocumentSyncService`, or None if not initialized."""
    return _service


__all__ = ["CremindDocumentSyncService", "get_service", "remove_profile_documents", "set_service"]
