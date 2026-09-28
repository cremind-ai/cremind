"""Where the backend's hardware runtime keeps its state: ``<SYS>/.tag-runtime/``.

::

    <SYS>/.tag-runtime/
        host.json           this installation's hardware host id (never reused)
        host.lock           held by the one process that runs the hardware runtime
        host-lock.json      which process holds it (pid, host id)
        workers/<companion_id>/   one gateway worker each (worker.json, controller.key,
                            secrets.json, companion.sqlite3, agent.json, daemon-status.json)
        assets/fonts/<pack_id>/   verified font asset bundles (read-only), shared by every worker
        locks/gateway-<device_id>.lock   held while a worker drives that gateway
        migration/          journals of moves from Cremind Connect
        remote/             this computer as a desktop gateway computer of a Cremind server
                            elsewhere: installation.key (owner-only) + installation.json,
                            enrollment.json (server, host id, profile) + enrollment.key
                            (owner-only: its host credential), ca.pem

A dot directory next to ``.tag-authority``: profile names cannot take it
(``[a-z0-9_-]+``), every file API refuses it
(:data:`app.utils.credential_paths.CREDENTIAL_DIR_NAMES`), Documentation
search never indexes it, and ordinary backups leave it out
(:mod:`app.backup.rules`) — its controller keys and connector credentials
belong to this computer. A worker directory has the layout of a Cremind
Connect worker, so a Connect worker moves in unchanged.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DIR_NAME = ".tag-runtime"
HOST_FILE = "host.json"
HOST_LOCK = "host.lock"
HOST_LOCK_INFO = "host-lock.json"
_DEVICE_ID = re.compile(r"^[0-9a-f]{32}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def root() -> Path:
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / DIR_NAME


@dataclass(frozen=True)
class RuntimePaths:
    base: Path

    @property
    def host_file(self) -> Path:
        return self.base / HOST_FILE

    @property
    def host_lock(self) -> Path:
        return self.base / HOST_LOCK

    @property
    def host_lock_info(self) -> Path:
        """Who holds :attr:`host_lock` (pid, host id). Named explicitly: the lock's default,
        ``host.lock`` → ``host.json``, is :attr:`host_file`, which the lock would overwrite and then
        delete on release (a new host id at every start)."""
        return self.base / HOST_LOCK_INFO

    @property
    def workers_dir(self) -> Path:
        return self.base / "workers"

    @property
    def assets_dir(self) -> Path:
        return self.base / "assets"

    @property
    def locks_dir(self) -> Path:
        return self.base / "locks"

    @property
    def migration_dir(self) -> Path:
        return self.base / "migration"

    @property
    def remote_dir(self) -> Path:
        return self.base / "remote"

    # The installation identity of a desktop gateway computer (the shape Cremind Connect's installation module
    # reads: ``data_dir``, ``installation_key``, ``installation_json``).

    @property
    def data_dir(self) -> Path:
        return self.remote_dir

    @property
    def installation_key(self) -> Path:
        return self.remote_dir / "installation.key"

    @property
    def installation_json(self) -> Path:
        return self.remote_dir / "installation.json"

    @property
    def enrollment_file(self) -> Path:
        return self.remote_dir / "enrollment.json"

    @property
    def enrollment_key(self) -> Path:
        return self.remote_dir / "enrollment.key"

    def worker_dir(self, worker_id: str) -> Path:
        if not _WORKER_ID.match(worker_id or ""):
            raise ValueError(f"not a worker id: {worker_id!r}")
        return self.workers_dir / worker_id

    def gateway_lock(self, device_id: str) -> Path:
        if not _DEVICE_ID.match(device_id or ""):
            raise ValueError(f"not a device id: {device_id!r}")
        return self.locks_dir / f"gateway-{device_id}.lock"

    def ensure(self) -> RuntimePaths:
        """Create the directories, owner-only where the OS supports it."""
        for directory in (self.base, self.workers_dir, self.assets_dir, self.locks_dir):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        return self


def default_paths() -> RuntimePaths:
    return RuntimePaths(root())


def host_identity(paths: RuntimePaths) -> dict[str, Any]:
    """``host.json``: ``{host_id, created_at}`` — created once, never regenerated over an existing file."""
    path = paths.host_file
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("host_id"), str) and doc["host_id"]:
            return doc
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{path} is unreadable ({exc}); move it aside only if no worker depends on it") from None
    paths.ensure()
    import time

    doc = {"host_id": str(uuid.uuid4()), "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
    try:
        os.link(tmp, path)  # never replaces a host.json another process wrote meanwhile
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        return host_identity(paths)
    except OSError:
        os.replace(tmp, path)
    else:
        tmp.unlink(missing_ok=True)
    return doc


__all__ = ["DIR_NAME", "RuntimePaths", "default_paths", "host_identity", "root"]
