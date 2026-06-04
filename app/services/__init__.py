"""Per-service deployment-mode descriptors and provisioning.

The Setup Wizard lets the user pick a deployment mode (Docker / Native /
External) for each backing service that supports more than one. This
package answers two questions:

- :mod:`app.services.manifest` — what services exist, what modes does
  each support, and what defaults / recipes does each mode need? The
  wizard UI is driven by ``get_capabilities_payload()``.
- :mod:`app.services.provisioner` — how do we actually bring a service
  up in a given mode? ``provision(service_id, mode, config)`` is the
  single entry point used by the wizard's setup handler.

SQLite is intentionally absent — it has only one possible deployment
(a local file) so it never goes through this module.
"""

from app.services.manifest import (
    SERVICES,
    ServiceSpec,
    get_capabilities_payload,
    get_service,
)

__all__ = [
    "SERVICES",
    "ServiceSpec",
    "get_capabilities_payload",
    "get_service",
]
