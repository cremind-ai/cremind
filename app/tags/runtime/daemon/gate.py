"""The send gate: nothing cached leaves this worker before it knows it is current.

A worker keeps work across restarts (its durable queue: composed screens,
commands it had claimed). Sending any of it after a restart, a long outage
or a server restore could put stale content on a tag or change a device that
Cremind has meanwhile given to someone else. So **sending screens** and
**running commands** wait until the gate is open, and it opens only after a
full pass of:

1. **identity** — the hardware credential's ``whoami`` answered (the
   credential is valid and names this companion);
2. **content** — every content credential synced with Cremind's *current*
   stream since the gate closed (``sync``: outstanding jobs rebuilt, jobs
   Cremind ended dropped, tags taken away released);
3. **checks** added by the worker's agent (a protocol v2 private worker):
   the authorization **lease** renewed (the worker is still the current one,
   not paused into removal) and **reconciliation** with Cremind's view of its
   devices (``GET state``: generations, revocations).

A credential that is refused for good (401/403) does not hold the gate: its
own loops stop and it has nothing to send. Any other failure is retried with
back-off while the gate stays closed.

The gate closes again when Cremind's stream changes under the worker (a
restore) and when a private worker's lease lapses (Cremind unreachable for
longer than the lease); the next pass reopens it. Composing screens is local
and goes on while the gate is closed; outbox reports (receipts, previews,
results) are not held.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..connector.client import Backoff, ConnectorError

if TYPE_CHECKING:
    from .service import DaemonService

log = logging.getLogger(__name__)

POLL_S = 0.1
Check = Callable[[], Awaitable[None]]


class SendGate:
    """See the module docstring."""

    def __init__(self, svc: DaemonService) -> None:
        self.svc = svc
        self._open = asyncio.Event()
        self._wanted = asyncio.Event()
        self._wanted.set()
        self.checks: list[tuple[str, Check]] = []
        self.reason = "starting"
        self.error: str | None = None
        self.opened = 0

    def add_check(self, name: str, check: Check) -> None:
        """Run ``check`` on every pass (it raises to keep the gate closed)."""
        self.checks.append((name, check))

    @property
    def is_open(self) -> bool:
        return self._open.is_set()

    async def wait(self) -> None:
        await self._open.wait()

    def close(self, reason: str) -> None:
        """Hold sends until a fresh pass (identity, content sync, checks) succeeds."""
        if self._open.is_set():
            log.info("gate: closed (%s); nothing is sent until the worker is current again", reason)
        self._open.clear()
        self.reason = reason
        self._wanted.set()

    def as_json(self) -> dict[str, Any]:
        return {"open": self.is_open, "reason": None if self.is_open else self.reason, "error": self.error,
                "opened": self.opened}

    async def run(self) -> None:
        settings = self.svc.settings
        backoff = Backoff(1.0, settings.connector_retry_max_s)
        while True:
            await self._wanted.wait()
            self._wanted.clear()
            try:
                await self._pass()
            except Exception as exc:  # noqa: BLE001 - any failed pass keeps the gate closed, and is retried
                self.error = str(exc) if isinstance(exc, ConnectorError) else f"{type(exc).__name__}: {exc}"
                delay = backoff.next()
                log.info("gate: still closed (%s); trying again in %.1fs", self.error, delay)
                await asyncio.sleep(delay)
                self._wanted.set()
                continue
            backoff.reset()
            if self._wanted.is_set():
                continue  # closed again while the pass ran: run another one
            self.error = None
            self._open.set()
            self.opened += 1
            log.info("gate: open (%s)", "worker current" if self.opened == 1 else f"after {self.reason}")
            self.svc.wake_scheduler()

    async def _pass(self) -> None:
        svc = self.svc
        hardware = svc.hardware
        if hardware is not None:
            self.reason = "waiting for the worker's identity"
            while not hardware.identified.is_set() and hardware.state != "stopped":
                await asyncio.sleep(POLL_S)
        marks = {cid: worker.syncs for cid, worker in svc.content_workers.items()}
        for cid in marks:
            svc.request_sync(cid, forced=True)
        self.reason = "waiting for the content sync"
        while any(worker.syncs <= marks[cid] and worker.state != "stopped"
                  for cid, worker in svc.content_workers.items()):
            await asyncio.sleep(POLL_S)
        for name, check in self.checks:
            self.reason = f"waiting for the {name}"
            await check()


__all__ = ["SendGate"]
