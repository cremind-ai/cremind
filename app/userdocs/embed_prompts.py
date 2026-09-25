"""The prompt prefixes User Document Search embeds with, and their version.

e5 and EmbeddingGemma were trained with instruction prefixes ("query: " /
"passage: " and task prompts respectively) and retrieve noticeably better with
them. The collections that predate this feature (documentation, memory, tool
tables) were built without prefixes, so only the userdocs path applies them:
``LocalEmbeddings.embed_passages`` / ``embed_search_query`` read
``PASSAGE_PREFIX`` / ``QUERY_PREFIX`` off the loaded provider.

Why a scheme number: a vector embedded with one prefix is not comparable with
a query embedded with another, and nothing in a stored vector says which one
built it. ``PROMPT_SCHEME`` is therefore part of every userdocs collection name
(``..._p<scheme>``, see :func:`app.userdocs.vectors.collection_name`). Changing
any prefix below means bumping it: the next start then sees no collection for
the new name and re-embeds from stored text into a fresh one, instead of
mixing two schemes in one collection.

The table mirrors the providers' class attributes so code that only needs the
text (estimates, diagnostics) does not have to import sentence-transformers;
a test keeps the two in step.
"""

from __future__ import annotations

PROMPT_SCHEME = 1

# provider key → (query prefix, passage prefix)
_PREFIXES: dict[str, tuple[str, str]] = {
    "me5": ("query: ", "passage: "),
    "gemma": ("task: search result | query: ", "title: none | text: "),
}


def query_prefix(provider_key: str) -> str:
    """The prefix a search query gets for ``provider_key`` ("" when unknown)."""
    return _PREFIXES.get((provider_key or "").lower(), ("", ""))[0]


def passage_prefix(provider_key: str) -> str:
    """The prefix an indexed passage gets for ``provider_key`` ("" when unknown)."""
    return _PREFIXES.get((provider_key or "").lower(), ("", ""))[1]
