"""Moving gateway workers from Cremind Connect into Cremind's hardware host.

A computer that ran the older Cremind Connect keeps each gateway's worker in
Connect's data directory: its controller key, connector credentials, durable
delivery queue and device keys. Cremind takes the workers over as they are —
nothing is paired again and no key is made anew (the server's authority keys
are never touched). Only the workers of THIS Cremind move: a worker belongs
to it when its recorded authority id is this server's; every other worker in
Connect's directory is left alone and keeps running there.

Each worker goes through a journal (``migration/<worker_id>.json`` under the
runtime folder, rewritten atomically after every step)::

    detected   Connect's worker.json read; enabled; this server's
    validated  its controller key, credentials and database are readable
    disabled   Connect's copy says enabled: false, and Connect was asked to
               reload it (IPC ``remove_worker``, keeping the directory)
    stopped    Connect reports it stopped (or Connect is not running at all)
    copied     into ``workers/.migrating-<id>``: the files as they are, the
               database through SQLite's backup API (a consistent copy even
               with a WAL), the secrets owner-only
    verified   the copy's database passes ``integrity_check`` and its key and
               credentials read back
    adopted    Cremind records the worker on this host (execution kind, host);
               the copy's worker.json says so (host id, generation) and is
               enabled again (it was copied after Connect's was disabled)
    published  renamed to ``workers/<worker_id>`` in one step (Connect's id stays its name)
    started    handed to the supervisor

A failure before ``adopted`` rolls back: the copy is removed, Connect's worker
is enabled again and Connect is asked to reload it; the journal keeps why.
From ``adopted`` on the worker is Cremind's; a failure after it is retried at
the next start (the journal resumes). Connect's own copy stays in its
directory, disabled and marked ``migrated_to``: Cremind never deletes it.
Connect's startup registration is removed (and the service asked to stop)
only once none of its workers is enabled any more.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

log = logging.getLogger("app.tags.runtime.hosting")

JOURNAL_SCHEMA = "cremind/tag-migration@1"
DB_FILE = "companion.sqlite3"
SECRET_FILES = ("controller.key", "secrets.json")
COPY_FILES = ("worker.json", "controller.key", "secrets.json", "agent.json", "daemon-status.json", "config.toml",
              "ca.pem")
STOP_TIMEOUT_S = 20.0


class MigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Outcome:
    worker_id: str
    companion_id: str | None
    state: str
    """``complete`` | ``rolled_back`` | ``failed`` (after ``adopted``: retried at the next start)."""
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"worker_id": self.worker_id, "companion_id": self.companion_id, "state": self.state,
                "detail": self.detail}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Journal:
    path: Path
    doc: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def open(cls, runtime: Any, worker_id: str, source: Path) -> Journal:
        path = runtime.migration_dir / f"{worker_id}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        if doc.get("schema") != JOURNAL_SCHEMA or doc.get("state") in ("rolled_back", "complete", None):
            if doc.get("state") != "complete":
                doc = {"schema": JOURNAL_SCHEMA, "worker_id": worker_id, "source": str(source), "state": "in_progress",
                       "steps": [], "error": None, "started_at": _now()}
        return cls(path, doc)

    def done(self, step: str) -> bool:
        return any(s.get("step") == step for s in self.doc.get("steps") or [])

    def step(self, step: str, **fields: Any) -> None:
        self.doc.setdefault("steps", []).append({"step": step, "at": _now(), **fields})
        _write_json(self.path, self.doc)

    def finish(self, state: str, error: str | None = None) -> None:
        self.doc.update(state=state, error=error, finished_at=_now())
        _write_json(self.path, self.doc)


class ConnectService:
    """The running Cremind Connect, over its local IPC (same OS user, owner-only key)."""

    def __init__(self, paths: Any) -> None:
        self.paths = paths

    def _request(self, op: str, **fields: Any) -> dict[str, Any] | None:
        from app.tags.runtime.connect import ipc

        try:
            return ipc.request(self.paths, op, timeout=10.0, **fields)
        except ipc.IpcUnavailable:
            return None

    def disable(self, worker_id: str) -> None:
        """Stop the worker and re-read its (now disabled) spec; nothing when Connect is not running."""
        self._request("remove_worker", worker_id=worker_id)

    def enable(self, worker_id: str) -> None:
        self._request("add_worker", worker_id=worker_id)

    def running(self, worker_id: str) -> bool:
        """Whether Connect still runs this worker (False when Connect itself is not running)."""
        from app.tags.runtime.connect.instance import is_locked

        answer = self._request("status")
        if answer is None:
            return is_locked(self.paths.service_lock)  # a service that does not answer yet may still run it
        for worker in answer.get("workers") or []:
            if worker.get("worker_id") == worker_id:
                return worker.get("state") in ("running", "starting", "stopping")
        return False

    def retire(self) -> None:
        """No worker left: stop Connect and remove its startup registration."""
        from app.tags.runtime.connect import startup
        from app.tags.runtime.connect.plan import apply

        self._request("stop")
        with contextlib.suppress(Exception):
            apply(startup.stop_plan(self.paths))
        apply(startup.unregister_plan(self.paths))


def connect_paths() -> Any | None:
    """Cremind Connect's directories for this OS user, when it ever ran here."""
    try:
        from app.tags.runtime.connect.paths import default_paths

        paths = default_paths()
    except Exception:  # noqa: BLE001 - Connect never ran, or this OS has no place for it
        return None
    return paths if paths.workers_dir.is_dir() else None


def candidates(connect: Any, authority_id: str) -> list[tuple[Path, Any]]:
    """Connect's enabled workers of the server whose authority id is ``authority_id``."""
    from app.tags.runtime.connect.workerdir import WorkerDirError, list_workers

    out = []
    for directory, spec in list_workers(connect.workers_dir):
        if isinstance(spec, WorkerDirError) or not spec.enabled:
            continue
        if str(spec.extra.get("authority_id") or "").lower() != authority_id.lower():
            continue  # another Cremind's worker: left alone
        out.append((directory, spec))
    return out


def _validate(directory: Path) -> None:
    from app.tags.runtime.connect.setup_flow import read_controller_key
    from app.tags.runtime.connector.client import parse_credential
    from app.tags.runtime.secrets import FileBackend, SecretStore

    try:
        read_controller_key(directory)
    except (OSError, ValueError, KeyError) as exc:
        raise MigrationError("controller_key", f"the controller key is unusable: {exc}") from None
    store = SecretStore(FileBackend(directory / "secrets.json"))
    for name in ("hardware", "content"):
        value = store.get_credential(name)
        if value is None:
            raise MigrationError("credentials", f"the {name} credential is missing")
        parse_credential(value)
    db = directory / DB_FILE
    if db.exists():
        _integrity(db)


def _integrity(db: Path) -> None:
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise MigrationError("database", f"{db.name} failed its integrity check ({result})")


def _copy(source: Path, target: Path) -> None:
    from app.tags.runtime.private_files import restrict_to_owner

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o700)
    for name in COPY_FILES:
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
            if name in SECRET_FILES:
                restrict_to_owner(target / name)
    db = source / DB_FILE
    if db.exists():
        src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(target / DB_FILE)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()


def _mark_source(directory: Path, spec: Any, *, enabled: bool, migrated: bool) -> None:
    from app.tags.runtime.connect.workerdir import write_worker

    extra = dict(spec.extra)
    if migrated:
        extra.update(migrated_to="cremind", migrated_at=_now())
    else:
        extra.pop("migrated_to", None)
        extra.pop("migrated_at", None)
    write_worker(directory, replace(spec, enabled=enabled, extra=extra))


def _rollback(journal: Journal, source: Path, spec: Any, copy: Path, service: Any, why: str) -> Outcome:
    shutil.rmtree(copy, ignore_errors=True)
    if journal.done("disabled"):
        with contextlib.suppress(Exception):
            _mark_source(source, spec, enabled=True, migrated=False)
        with contextlib.suppress(Exception):
            service.enable(spec.worker_id)
    journal.finish("rolled_back", why)
    log.warning("migration: %s stays with Cremind Connect (%s)", spec.worker_id, why)
    return Outcome(spec.worker_id, spec.companion_id, "rolled_back", why)


async def migrate_worker(runtime: Any, source: Path, spec: Any, *, host_id: str,
                         adopt: Callable[[str], Awaitable[dict[str, Any]]], service: Any,
                         start: Callable[[str], None] | None = None,
                         sleep: Callable[[float], Awaitable[None]] | None = None,
                         stop_timeout_s: float = STOP_TIMEOUT_S) -> Outcome:
    """One worker through the journal (see the module docstring)."""
    import asyncio

    from app.tags.runtime.connect.workerdir import WorkerSpec, write_worker

    pause = sleep or asyncio.sleep
    journal = Journal.open(runtime, spec.worker_id, source)
    if journal.doc.get("state") == "complete":
        # Moved already, and Connect's copy was enabled again since: Cremind drives it; Connect must not.
        await asyncio.to_thread(_mark_source, source, spec, enabled=False, migrated=True)
        await asyncio.to_thread(service.disable, spec.worker_id)
        return Outcome(spec.worker_id, journal.doc.get("companion_id"), "complete", "already moved")
    companion_id = spec.companion_id
    copy = runtime.workers_dir / f".migrating-{spec.worker_id}"
    # Connect's worker id stays the directory name (worker.json must match it); the companion id is inside.
    final = runtime.workers_dir / spec.worker_id
    try:
        if not journal.done("detected"):
            journal.step("detected", companion_id=companion_id, server=spec.server_origin, profile=spec.profile)
        if not journal.done("adopted"):
            await asyncio.to_thread(_validate, source)
            journal.step("validated")
            await asyncio.to_thread(_mark_source, source, spec, enabled=False, migrated=False)
            await asyncio.to_thread(service.disable, spec.worker_id)
            journal.step("disabled")
            deadline = time.monotonic() + stop_timeout_s
            while await asyncio.to_thread(service.running, spec.worker_id):
                if time.monotonic() > deadline:
                    raise MigrationError("still_running", "Cremind Connect did not stop the worker in time")
                await pause(0.5)
            journal.step("stopped")
            await asyncio.to_thread(_copy, source, copy)
            journal.step("copied")
            await asyncio.to_thread(_validate, copy)
            journal.step("verified")
    except MigrationError as exc:
        return _rollback(journal, source, spec, copy, service, str(exc))
    except Exception as exc:  # noqa: BLE001 - a failure before adoption leaves Connect as it was
        log.exception("migration: %s failed before adoption", spec.worker_id)
        return _rollback(journal, source, spec, copy, service, f"{type(exc).__name__}: {exc}")

    def apply_adoption(generation: int, profile_id: str) -> None:
        # The copy was taken after Connect's worker was disabled: enabled again here, where Cremind runs it.
        copied = WorkerSpec.from_json(json.loads((copy / "worker.json").read_text(encoding="utf-8")))
        write_worker(copy, replace(copied, enabled=True, extra={
            **copied.extra, "host_id": host_id, "generation": generation,
            "profile_id": profile_id or str(copied.extra.get("profile_id") or ""), "migrated_from": "cremind-connect"}))

    try:
        if not journal.done("adopted"):
            try:
                adopted = await adopt(companion_id)
            except Exception as exc:  # noqa: BLE001 - refused or unreachable: Connect keeps it
                return _rollback(journal, source, spec, copy, service, f"Cremind did not take it over: {exc}")
            generation = int(adopted.get("generation") or 0)
            profile_id = str((adopted.get("profile") or {}).get("id") or "")
            await asyncio.to_thread(apply_adoption, generation, profile_id)
            journal.step("adopted", generation=generation, profile_id=profile_id)
        if not journal.done("published"):
            if not copy.exists() and final.exists():
                pass  # published before a crash (the journal just did not say so); the supervisor may run it already
            else:
                if not copy.exists():
                    # A crash lost the copy after Cremind adopted the worker: make it again from Connect's
                    # (disabled, untouched) directory.
                    adopted_step = next(s for s in journal.doc["steps"] if s.get("step") == "adopted")
                    await asyncio.to_thread(_copy, source, copy)
                    await asyncio.to_thread(_validate, copy)
                    await asyncio.to_thread(apply_adoption, int(adopted_step.get("generation") or 0),
                                            str(adopted_step.get("profile_id") or ""))
                if final.exists():
                    old = final.with_name(f".replaced-{spec.worker_id}-{int(time.time())}")
                    os.replace(final, old)
                    shutil.rmtree(old, ignore_errors=True)
                os.replace(copy, final)
            await asyncio.to_thread(_mark_source, source, spec, enabled=False, migrated=True)
            journal.step("published", directory=str(final))
        if start is not None:
            start(spec.worker_id)
        journal.step("started")
        journal.doc["companion_id"] = companion_id
        journal.finish("complete")
        log.info("migration: gateway worker %s moved from Cremind Connect into Cremind", companion_id)
        return Outcome(spec.worker_id, companion_id, "complete")
    except Exception as exc:  # noqa: BLE001 - adopted: retried from the journal at the next start
        log.exception("migration: %s failed after adoption; retried at the next start", spec.worker_id)
        journal.finish("in_progress", f"{type(exc).__name__}: {exc}")
        return Outcome(spec.worker_id, companion_id, "failed", str(exc))


async def migrate(runtime: Any, connect: Any, *, authority_id: str, host_id: str,
                  adopt: Callable[[str], Awaitable[dict[str, Any]]], service: Any | None = None,
                  start: Callable[[str], None] | None = None, **kwargs: Any) -> list[Outcome]:
    """Every worker of this server in Connect's directory; then retire Connect if nothing is left there."""
    import asyncio

    from app.tags.runtime.connect.workerdir import WorkerDirError, list_workers

    service = service or ConnectService(connect)
    outcomes = []
    for source, spec in await asyncio.to_thread(candidates, connect, authority_id):
        outcomes.append(await migrate_worker(runtime, source, spec, host_id=host_id, adopt=adopt, service=service,
                                             start=start, **kwargs))
    remaining = [s for _, s in await asyncio.to_thread(list_workers, connect.workers_dir)
                 if not isinstance(s, WorkerDirError) and s.enabled]
    if outcomes and not remaining:
        try:
            await asyncio.to_thread(service.retire)
            log.info("migration: Cremind Connect has no worker left; its startup registration was removed")
        except Exception:  # noqa: BLE001 - Connect stays registered; it has nothing to run
            log.warning("migration: Cremind Connect's startup registration could not be removed", exc_info=True)
    return outcomes


def summary(runtime: Any) -> dict[str, int]:
    """``{moved, failed, rolled_back}`` from the journals (the host's status report)."""
    counts = {"moved": 0, "failed": 0, "rolled_back": 0}
    with contextlib.suppress(FileNotFoundError):
        for path in runtime.migration_dir.glob("*.json"):
            with contextlib.suppress(OSError, ValueError):
                state = json.loads(path.read_text(encoding="utf-8")).get("state")
                key = {"complete": "moved", "rolled_back": "rolled_back"}.get(state, "failed")
                counts[key] += 1
    return counts


__all__ = ["ConnectService", "Journal", "MigrationError", "Outcome", "candidates", "connect_paths", "migrate",
           "migrate_worker", "summary"]
