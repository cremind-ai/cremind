"""Errors the sync engine raises to its callers (the API, the CLI via the API).

Each carries the HTTP status and machine-readable code the API should send,
so the translation lives with the condition rather than in every route.
"""

from __future__ import annotations

from typing import Any


class EngineError(Exception):
    status = 409
    code = "InvalidState"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None, **extra: Any):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


class NotEnabled(EngineError):
    """The profile has not turned the feature on, or it is suspended."""

    code = "NotEnabled"


class UnknownAction(EngineError):
    status = 400
    code = "ValidationFailed"


class NotFound(EngineError):
    """A file or folder id that does not exist *for this profile* — the same
    answer as for another profile's id, so ids cannot be probed."""

    status = 404
    code = "NotFound"


class StoragePaused(EngineError):
    """Ingestion is paused at a hard storage level; only freeing space helps."""

    code = "StoragePaused"
