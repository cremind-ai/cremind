"""Native-mode provisioning — run a service locally without Docker.

Two kinds of native services are described in
:mod:`app.services.manifest`:

- ``in_process``: a Python library imported directly by the Cremind
  process (e.g. the ``chromadb`` persistent client). Provisioning
  reduces to "make sure the data directory exists"; the actual library
  is loaded by the storage adapter when it first needs the service.
- ``subprocess``: a managed binary Cremind downloads to
  ``<working_dir>/bin/`` and spawns. Phase 1 ships no subprocess
  services — Qdrant's binary-download path is deferred to phase 2 — so
  hitting that branch raises :class:`ProvisionError` with a "not yet
  implemented" message.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.config.settings import BaseConfig
from app.events.setup_progress_bus import publish_setup_event
from app.services.manifest import ServiceSpec
from app.services.provisioner.base import ProvisionedService, ProvisionError


def _emit(step: str, message: str, level: str = "info") -> None:
    try:
        publish_setup_event({
            "step": step,
            "message": message,
            "level": level,
            "ts": time.time(),
        })
    except Exception:  # noqa: BLE001
        pass


def _step_for_service(service_id: str) -> str:
    return "database" if service_id == "postgres" else "vectorstore"


async def provision(spec: ServiceSpec, config: dict[str, Any]) -> ProvisionedService:
    if spec.native is None:
        raise ProvisionError(
            f"{spec.display_name} has no Native recipe — this is a bug "
            f"in app/services/manifest.py"
        )

    if spec.native.kind == "in_process":
        return await _provision_in_process(spec, config)
    if spec.native.kind == "subprocess":
        raise ProvisionError(
            f"Native (subprocess) mode for {spec.display_name} is not yet "
            "implemented. Use Docker or External, or wait for the next Cremind release."
        )
    raise ProvisionError(
        f"Unknown native recipe kind: {spec.native.kind!r}"
    )


async def _provision_in_process(
    spec: ServiceSpec, config: dict[str, Any]
) -> ProvisionedService:
    """Resolve the persist path and ensure the directory exists.

    The path is either user-supplied (``config["persist_path"]``) or
    derived from the working dir: ``<working_dir>/storage/<subpath>``.
    The storage adapter (e.g. :mod:`app.vectorstores.chroma`) reads
    the resolved path from the persisted connection.
    """
    assert spec.native is not None
    step = _step_for_service(spec.id)
    user_path = (config.get("persist_path") or "").strip()
    if user_path:
        persist_path = os.path.expanduser(user_path)
    else:
        working_dir = os.path.expanduser(BaseConfig.CREMIND_SYSTEM_DIR)
        persist_path = os.path.join(working_dir, "storage", spec.native.default_data_subpath)

    _emit(
        step,
        f"Preparing {spec.display_name} data directory at {persist_path}…",
    )

    try:
        Path(persist_path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProvisionError(
            f"Could not create {spec.display_name} data directory "
            f"at {persist_path!r}: {exc}"
        )

    return ProvisionedService(
        service_id=spec.id,
        mode="native",
        connection={"persist_path": persist_path},
    )
