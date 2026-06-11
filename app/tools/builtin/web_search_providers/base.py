"""Shared types and the provider protocol for ``web_search``.

A provider is any object exposing::

    async def search(query: str, *, count: int, variables: dict) -> list[SearchResult]

It returns a list of :data:`SearchResult` dicts on success, or raises
:class:`ProviderError` (or any exception) on failure. The ``web_search`` tool
catches failures and, when the provider was selected via ``"auto"``, falls
through to the next provider in the resolved order.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, TypedDict


class SearchResult(TypedDict):
    """One web-search hit. ``title``/``snippet`` are untrusted web text;
    ``url`` is a machine field (left un-wrapped so it can be chained into
    ``web_fetch``)."""
    title: str
    url: str
    snippet: str


class ProviderError(Exception):
    """A provider failed in a way the caller can surface or fall through from.

    Carries a short machine ``code`` (e.g. ``"RateLimited"``, ``"BotChallenge"``,
    ``"AuthRequired"``, ``"Upstream"``, ``"Network"``) so the tool can build a
    consistent structured error.
    """

    def __init__(self, message: str, *, code: str = "ProviderError") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SearchProvider(Protocol):
    """Structural type every provider satisfies."""

    id: str

    async def search(
        self, query: str, *, count: int, variables: Dict[str, Any],
    ) -> List[SearchResult]:
        ...
