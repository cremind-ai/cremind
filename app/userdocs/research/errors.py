"""Errors the research job API raises to its callers (the tool leaf, REST)."""

from __future__ import annotations

from typing import Any


class ResearchError(Exception):
    code = "ResearchError"
    status = 409

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


class ResearchBusy(ResearchError):
    """The profile already has a job running (one at a time). ``job_id`` and
    ``question`` name it, so the caller can continue or cancel it."""

    code = "ResearchBusy"
    status = 409

    def __init__(self, job_id: str, question: str) -> None:
        super().__init__(
            f"A research job is already running: {question!r} ({job_id}). Continue it with "
            f"continue_job={job_id!r}, or cancel it first.",
            job_id=job_id, question=question,
        )


class JobNotFound(ResearchError):
    code = "JobNotFound"
    status = 404


class ResearchUnavailable(ResearchError):
    """User Document Search is not usable for this profile right now (off,
    not allowed by the admin, no index yet). ``status_code`` is the reason
    code open_engine gave."""

    code = "UserDocumentsUnavailable"
    status = 503


class InvalidRequest(ResearchError):
    code = "InvalidRequest"
    status = 400


__all__ = ["InvalidRequest", "JobNotFound", "ResearchBusy", "ResearchError", "ResearchUnavailable"]
