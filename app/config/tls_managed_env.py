"""The HTTPS environment a container install carries in its own volume.

A container's environment is fixed when the container is *created*. Restarting
re-runs the entrypoint with exactly the variables it was created with, which is
why enabling HTTPS on Docker has always ended in a runbook asking the operator
to hand-edit the Compose ``.env`` and recreate the container: the settings the
switch needs had nowhere to live that a restart would read.

This gives them somewhere. ``managed-env`` sits beside ``transition.json`` in
the system directory — which on Docker is the ``cremind-data`` volume, so it
survives both a restart and a replacement — and is loaded into the environment
before :mod:`app.config.settings` binds anything from it. A switch then applies
on an ordinary process restart, which the container already performs for itself
(the entrypoint's ``wait -n`` plus Compose's ``restart: unless-stopped``).

Three things make this safe to do without a capability handshake:

* **The build that writes it is the build that reads it.** A restart reuses the
  same image, so a server that can honour this file is by definition the server
  that will come back. Nothing has to negotiate with an older entrypoint.
* **It is loaded only where the container environment is genuinely immutable**
  (``INSTALL_MODE=docker``). A native install's ``.env`` keeps being the
  installer shim's business, and Kubernetes keeps its Helm runbook — there
  ``cremind.ssl`` moves the Service, the probes and the proxy sidecar together,
  which no pod can do to itself.
* **It carries only the six keys the HTTPS switch owns** and is written by the
  same atomic, rollback-recorded path as a native switch, so reverting it is
  the same operation that has always reverted one.

It also retires itself. Once the deployment's own environment already carries
the same values — because the operator recreated the container from a mirrored
Compose ``.env``, or set them by hand — the file has nothing left to say and is
deleted on the next boot, so it can never become an invisible override of a
value someone is trying to change.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: The keys an HTTPS switch owns. Deliberately a copy of
#: ``tls_transition._NATIVE_ENV_KEYS`` rather than an import: this module is
#: loaded from ``settings.py`` during its own import, and ``tls_transition``
#: imports ``BaseConfig`` from it. ``tests/config/test_tls_managed_env.py``
#: pins the two equal, so the duplication cannot drift.
MANAGED_KEYS = (
    "CREMIND_SSL", "APP_URL", "CREMIND_UI_PORT", "CORS_ALLOWED_ORIGINS",
    "CREMIND_SSL_AUTO_HOSTS", "CREMIND_ATLASSIAN_REDIRECT_URI",
)

#: ``KEY=value`` with no quoting and no shell semantics. The file is only ever
#: written by :func:`app.config.tls_transition.persist_native`, and it is only
#: ever *read* by this parser — never sourced by a shell — so a value cannot be
#: executed however it was produced.
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def install_mode() -> str:
    return (os.environ.get("INSTALL_MODE") or "").strip().lower()


def is_container_install() -> bool:
    """Whether this deployment's environment is fixed at container creation.

    Docker only. Kubernetes is excluded on purpose even though its PVC would
    hold the file perfectly well: enabling HTTPS there is a chart change, not
    an environment change, so honouring this file would let the pod believe in
    a switch its Service and probes never made.
    """
    return install_mode() == "docker"


def system_dir() -> str:
    """The system directory, resolved without importing ``settings``.

    Mirrors ``settings.get_system_directory``; this module is imported *by*
    that one, so it cannot ask it.
    """
    raw = os.environ.get("CREMIND_SYSTEM_DIR") or ""
    if not raw:
        return os.path.normpath(os.path.expanduser("~/.cremind"))
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    return os.path.normpath(raw)


def managed_env_path(directory: str | None = None) -> Path:
    return Path(directory or system_dir()) / "tls" / "managed-env"


def read(path: Path | None = None) -> dict[str, str]:
    """The file's contents, ignoring anything outside :data:`MANAGED_KEYS`.

    ``utf-8-sig`` because the same tolerance is what lets a file written on
    Windows be read back; an unreadable or malformed file yields nothing rather
    than raising, since the caller is a boot path.
    """
    try:
        text = (path or managed_env_path()).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match and match.group(1) in MANAGED_KEYS:
            value = match.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[match.group(1)] = value
    return values


def is_redundant(values: dict[str, str]) -> bool:
    """Whether the deployment's own environment already says all of this.

    When it does, the file is no longer carrying the switch — the container was
    recreated with the values baked in — and keeping it would silently outrank
    whatever the operator edits next.
    """
    return bool(values) and all(
        os.environ.get(key) == value for key, value in values.items()
    )


def load_into_environ() -> dict[str, str]:
    """Apply the file to ``os.environ``, or delete it once it is redundant.

    Returns what was applied, for the boot log. Called from ``settings.py``
    before :class:`BaseConfig` binds any of these values, because that class
    reads ``os.environ`` once at class-definition time.

    Overrides rather than defers, which is the opposite of every other dotenv
    load in the process and is the entire point: the values it is overriding
    are the ones baked into the container at creation, and the switch exists to
    replace them.
    """
    if not is_container_install():
        return {}
    path = managed_env_path()
    values = read(path)
    if not values:
        return {}
    if is_redundant(values):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return {}
    os.environ.update(values)
    return values
