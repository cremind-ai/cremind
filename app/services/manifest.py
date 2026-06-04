"""Static descriptor of every provisionable backing service.

Each :class:`ServiceSpec` answers three questions:

- Which deployment modes (Docker / Native / External) does this service
  support? — drives the radio in the Setup Wizard.
- For Docker mode, which compose service and profile activate it? —
  read by :mod:`app.services.provisioner.docker` when the wizard picks
  this mode.
- For Native mode, how does Cremind run the service locally (in-process
  Python library vs. spawned subprocess)? — read by
  :mod:`app.services.provisioner.native`.
- For External mode, what are the host / port defaults to seed the
  wizard form with? — read by the UI via
  ``GET /api/services/capabilities``.

SQLite is deliberately absent: it has only one possible deployment
(a local file managed by the SQLite Python module) and the wizard
treats it as a "no deployment radio" choice in the DB step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["docker", "native", "external"]


@dataclass(frozen=True)
class DockerRecipe:
    """How the service is activated inside the bundled compose stack.

    The compose template ships every supported service pre-defined and
    gated behind a ``profiles:`` entry. The Docker provisioner activates
    a service by adding ``profile`` to ``COMPOSE_PROFILES`` in ``.env``
    and running ``docker compose up -d <compose_service_name>``.

    ``in_network_host`` is the address the Cremind container uses to
    reach this service over the user-defined compose network (the
    service's compose name). Host-side callers use ``localhost`` with
    the published port instead.
    """

    compose_service_name: str
    compose_profile: str
    in_network_host: str
    in_network_port: int


@dataclass(frozen=True)
class NativeRecipe:
    """How the service runs locally without Docker.

    ``in_process`` services are Python libraries imported directly by
    the Cremind process (e.g. the ``chromadb`` persistent client) — no
    subprocess, no port, just a data directory under ``<working_dir>``.

    ``subprocess`` services are managed binaries Cremind downloads to
    ``<working_dir>/bin/`` and spawns. The descriptor carries the
    minimum the provisioner needs to fetch + launch them; the actual
    download/start logic lives in :mod:`app.services.provisioner.native`.
    Phase 1 ships only ``in_process``; Qdrant subprocess support is
    deferred to phase 2.
    """

    kind: Literal["in_process", "subprocess"]
    # Subpath under ``<working_dir>/storage/`` where this service
    # persists its data. The wizard may override this in the payload.
    default_data_subpath: str
    # ``subprocess`` only — left None for ``in_process`` services.
    default_port: int | None = None


@dataclass(frozen=True)
class ExternalRecipe:
    """Defaults for the host/port fields when the user picks External."""

    default_host: str
    default_port: int


@dataclass(frozen=True)
class ServiceSpec:
    """A backing service that supports multiple deployment modes."""

    id: str
    display_name: str
    category: Literal["database", "vectorstore"]
    supported_modes: tuple[Mode, ...]
    docker: DockerRecipe | None = None
    native: NativeRecipe | None = None
    external: ExternalRecipe | None = None

    def supports(self, mode: Mode) -> bool:
        return mode in self.supported_modes


SERVICES: dict[str, ServiceSpec] = {
    # ── PostgreSQL ─────────────────────────────────────────────────────────
    # Docker via the bundled ``postgres:16`` image; external via host/port.
    # Native is intentionally not supported — installing a cross-platform
    # Postgres server from the wizard is far more complex than the rest of
    # this module justifies, and most users running native already have a
    # Postgres they can point to via External.
    "postgres": ServiceSpec(
        id="postgres",
        display_name="PostgreSQL",
        category="database",
        supported_modes=("docker", "external"),
        docker=DockerRecipe(
            compose_service_name="postgres",
            compose_profile="with-postgres",
            in_network_host="postgres",
            in_network_port=5432,
        ),
        external=ExternalRecipe(default_host="localhost", default_port=5432),
    ),
    # ── ChromaDB ───────────────────────────────────────────────────────────
    # Docker via the bundled ``chromadb/chroma`` image; Native runs the
    # ``chromadb`` Python library in-process with a persistent client (the
    # old "persistent" mode in the wizard, renamed to Native). External
    # points at a Chroma HTTP server elsewhere.
    "chroma": ServiceSpec(
        id="chroma",
        display_name="ChromaDB",
        category="vectorstore",
        supported_modes=("docker", "native", "external"),
        docker=DockerRecipe(
            compose_service_name="chroma",
            compose_profile="with-chroma",
            in_network_host="chroma",
            in_network_port=8000,
        ),
        native=NativeRecipe(kind="in_process", default_data_subpath="chroma"),
        external=ExternalRecipe(default_host="localhost", default_port=8000),
    ),
    # ── Qdrant ─────────────────────────────────────────────────────────────
    # Docker via the bundled ``qdrant/qdrant`` image; External via host/port.
    # Native (binary download + supervisor) is phase 2.
    "qdrant": ServiceSpec(
        id="qdrant",
        display_name="Qdrant",
        category="vectorstore",
        supported_modes=("docker", "external"),
        docker=DockerRecipe(
            compose_service_name="qdrant",
            compose_profile="with-qdrant",
            in_network_host="qdrant",
            in_network_port=6333,
        ),
        external=ExternalRecipe(default_host="localhost", default_port=6333),
    ),
}


def get_service(service_id: str) -> ServiceSpec:
    """Look up a service by id. Raises :class:`KeyError` for unknown ids."""
    spec = SERVICES.get(service_id)
    if spec is None:
        raise KeyError(f"Unknown service: {service_id!r}")
    return spec


def get_capabilities_payload() -> dict:
    """JSON-serializable snapshot of every service for the wizard UI.

    The shape is stable: each entry includes the supported modes and the
    defaults for any mode the service supports. ``None`` for a mode the
    service doesn't support so the UI can render a fall-through without
    having to recompute the mode list.
    """
    out: dict[str, dict] = {}
    for spec in SERVICES.values():
        out[spec.id] = {
            "id": spec.id,
            "display_name": spec.display_name,
            "category": spec.category,
            "supported_modes": list(spec.supported_modes),
            "docker_defaults": (
                {
                    "in_network_host": spec.docker.in_network_host,
                    "in_network_port": spec.docker.in_network_port,
                }
                if spec.docker
                else None
            ),
            "native_defaults": (
                {
                    "kind": spec.native.kind,
                    "data_subpath": spec.native.default_data_subpath,
                    "port": spec.native.default_port,
                }
                if spec.native
                else None
            ),
            "external_defaults": (
                {
                    "host": spec.external.default_host,
                    "port": spec.external.default_port,
                }
                if spec.external
                else None
            ),
        }
    return out
