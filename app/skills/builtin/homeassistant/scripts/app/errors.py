"""Shared exception types for the Home Assistant skill.

Kept in their own module so `homeassistant_api` and `auth` can both import them
without a circular dependency (the REST/WS clients call into auth for tokens).
"""
from __future__ import annotations


class HaError(RuntimeError):
    """Any failure talking to Home Assistant (HTTP, WebSocket, or protocol)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(HaError):
    """Authentication/authorization failure (missing token, refresh failed, etc.).

    Subclasses HaError so the listener's `except HaError` retry path also covers
    auth problems — a re-linked or replaced token then recovers without a restart.
    """
