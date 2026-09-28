"""The hardware runtime's log records, in Cremind's log.

:mod:`app.tags.runtime` logs through the standard library (it also runs in
the desktop hardware host and the developer tools). Inside the backend its
records go to Cremind's loguru sinks (``logs/app.log``, the Developer page),
prefixed ``[tags:runtime]`` and tagged with the worker a record came from.
"""

from __future__ import annotations

import contextvars
import logging

WORKER = contextvars.ContextVar[str | None]("tags_worker", default=None)
"""The worker a runtime task belongs to (set around each worker's tasks)."""

_LOGGER = "app.tags.runtime"
_HANDLER_FLAG = "_cremind_tags_bridge"


class _LoguruBridge(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        from app.utils.logger import logger

        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not lose the record
            message = str(record.msg)
        worker = WORKER.get()
        where = f"[tags:runtime{':' + worker[:8] if worker else ''}]"
        level = record.levelname if record.levelname in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO"
        logger.opt(exception=record.exc_info if record.exc_info else None, depth=6).log(level, f"{where} {message}")


def install(level: int = logging.INFO) -> None:
    """Route ``app.tags.runtime`` records to Cremind's log (idempotent)."""
    log = logging.getLogger(_LOGGER)
    if not any(getattr(h, _HANDLER_FLAG, False) for h in log.handlers):
        handler = _LoguruBridge()
        setattr(handler, _HANDLER_FLAG, True)
        log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False


__all__ = ["WORKER", "install"]
