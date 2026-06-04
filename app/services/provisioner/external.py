"""External-mode provisioning — the no-op path.

External means "a service is already running somewhere; Cremind just
talks to it". The wizard collected host/port/credentials in the form;
this module's only job is to validate that the required keys are
present and pass them back to the setup handler in the
:class:`ProvisionedService` envelope.

No process is started here. The wizard's setup handler can still do a
TCP-level reachability probe before completing setup (e.g. the
Postgres connection check that already lives in :func:`handle_setup`).
"""

from __future__ import annotations

from typing import Any

from app.services.manifest import ServiceSpec
from app.services.provisioner.base import ProvisionedService, ProvisionError


async def provision(spec: ServiceSpec, config: dict[str, Any]) -> ProvisionedService:
    if spec.external is None:
        raise ProvisionError(
            f"{spec.display_name} has no External recipe — this is a bug "
            f"in app/services/manifest.py"
        )

    host = (config.get("host") or "").strip() or spec.external.default_host
    port = config.get("port")
    if port is None or port == "":
        port = spec.external.default_port
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ProvisionError(
            f"{spec.display_name}: port must be an integer, got {port!r}"
        )

    # Build a connection dict the storage adapters can consume directly.
    # We don't strip credentials here — the wizard's setup handler is
    # responsible for marking secrets when persisting.
    connection: dict[str, Any] = {"host": host, "port": port}
    for key in ("database", "user", "password", "sslmode", "api_key", "https", "ssl"):
        if key in config and config[key] not in (None, ""):
            connection[key] = config[key]

    return ProvisionedService(service_id=spec.id, mode="external", connection=connection)
