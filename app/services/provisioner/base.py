"""Provisioner dispatch + common types.

The wizard calls :func:`provision` once per service it wants to bring
up. The actual mode-specific work is delegated to :mod:`.docker`,
:mod:`.native`, :mod:`.external`. Keeping the dispatcher here (rather
than in ``__init__``) avoids the import cycle that would otherwise form
when the mode modules import :class:`ProvisionedService`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.manifest import Mode, get_service


class ProvisionError(RuntimeError):
    """The requested mode could not be brought up on this host.

    The message is shown to the user verbatim in the wizard, so it
    should be specific: ``"Docker socket /var/run/docker.sock is not
    accessible — re-run the installer in Docker mode or pick External"``
    rather than ``"failed to start"``.
    """


@dataclass(frozen=True)
class ProvisionedService:
    """The post-provision view the wizard's setup handler persists.

    ``connection`` is opaque to this layer: it's a service-specific
    dict that the corresponding storage adapter (e.g. the Postgres
    database provider, the Qdrant vectorstore adapter) consumes
    directly. Keeping it opaque means new services can plug in
    without touching this module.
    """

    service_id: str
    mode: Mode
    connection: dict[str, Any]


def docker_available() -> bool:
    """Best-effort check: can this process drive ``docker compose``?

    Used by the capabilities endpoint to mask the Docker option in the
    UI when there's no point offering it (native installs that don't
    have a compose file, Docker installs where the socket wasn't
    mounted, …). Returns ``False`` on any error so a flaky probe never
    blocks the wizard.
    """
    from app.services.provisioner.docker import is_supported as _docker_supported

    try:
        return _docker_supported()
    except Exception:  # noqa: BLE001
        return False


async def provision(
    service_id: str,
    mode: Mode,
    config: dict[str, Any],
) -> ProvisionedService:
    """Bring ``service_id`` up in ``mode`` and return its connection.

    ``config`` carries the per-mode form values the wizard collected
    (host/port/credentials for External, persist path for Native
    Chroma, …). Unknown keys are ignored; missing keys fall back to
    the defaults in :class:`ServiceSpec`.

    Raises :class:`ProvisionError` if the mode is not supported by
    this service or the underlying recipe can't run on this host.
    """
    spec = get_service(service_id)
    if not spec.supports(mode):
        raise ProvisionError(
            f"{spec.display_name} does not support {mode!r} deployment mode "
            f"(supported: {', '.join(spec.supported_modes)})"
        )

    if mode == "docker":
        from app.services.provisioner import docker as _docker

        return await _docker.provision(spec, config)
    if mode == "native":
        from app.services.provisioner import native as _native

        return await _native.provision(spec, config)
    if mode == "external":
        from app.services.provisioner import external as _external

        return await _external.provision(spec, config)
    # Belt-and-suspenders: ``Mode`` is a Literal so static analysis
    # already enforces this, but a runtime guard catches typos in
    # caller-supplied dicts.
    raise ProvisionError(f"Unknown deployment mode: {mode!r}")
