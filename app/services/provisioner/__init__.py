"""Bring backing services up in the deployment mode the wizard picked.

One entry point: :func:`provision`. It dispatches to the right
mode-specific module (:mod:`.docker`, :mod:`.native`, :mod:`.external`)
based on the descriptor in :mod:`app.services.manifest`. Each module
returns a :class:`ProvisionedService` carrying:

- ``connection``: the host / port / credentials the rest of Cremind
  uses to talk to the service. The wizard's setup handler writes this
  into ``server_config`` / ``bootstrap.toml`` after a successful
  provision.
- ``effective_mode``: what actually happened (e.g. Docker mode that
  fell back to External when the user already had a healthy service
  on the expected port — TBD; phase 1 reports exactly the requested
  mode).

When a mode is not reachable on this host (Docker socket missing,
compose file not on disk for Native installs, …) :func:`provision`
raises :class:`ProvisionError` with a human-readable explanation that
the wizard surfaces verbatim.
"""

from app.services.provisioner.base import (
    ProvisionedService,
    ProvisionError,
    docker_available,
    provision,
)

__all__ = [
    "ProvisionedService",
    "ProvisionError",
    "docker_available",
    "provision",
]
