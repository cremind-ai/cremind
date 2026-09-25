"""The vector side of a Documentation search index: names, payloads, filters.

The vector store holds only what similarity search needs: one point per chunk
row (point id = the index DB's ``chunks.id``) and a small integer payload the
store can filter on. Text, locators and everything else stay in the per-profile
index DB, and every hit is joined back through it — so the payload is kept to
integers, which Qdrant can index cheaply and Chroma stores without coercion.

Payload keys (one letter each, because the payload is repeated per point):

- ``f`` file id (absent on folder cards)   - ``g`` folder id
- ``s`` source code (:data:`SOURCE_CODES`) - ``k`` kind code (:data:`KIND_CODES`)
- ``t`` chunk-type code (:data:`CTYPE_CODES`)
- ``d`` effective day, days since 1970-01-01 (absent when the file has no date)

The codes are persisted in every existing collection: **never renumber** a
code, only append new ones.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from app.documents import types as t
from app.documents.embed_prompts import PROMPT_SCHEME

# ── Payload keys ───────────────────────────────────────────────────────────
P_FILE = "f"
P_FOLDER = "g"
P_SOURCE = "s"
P_KIND = "k"
P_CTYPE = "t"
P_DAY = "d"

# Every key gets an integer payload index in Qdrant (Chroma indexes all
# metadata on its own). Passed as ``ensure_collection(payload_indexes=...)``.
PAYLOAD_INDEXES: dict[str, str] = {
    key: "integer" for key in (P_FILE, P_FOLDER, P_SOURCE, P_KIND, P_CTYPE, P_DAY)
}

# ── Stable codes (never renumber; append only) ─────────────────────────────
# Mirrors app.documents.settings.SOURCE_KINDS; kept literal so this module
# stays free of config imports (a test pins the two together).
SOURCE_CODES: dict[str, int] = {"local": 0, "drive": 1}

# 0 is reserved for "no kind" (folder cards).
KIND_NONE = 0
KIND_CODES: dict[str, int] = {
    t.KIND_TEXT: 1,
    t.KIND_MARKDOWN: 2,
    t.KIND_CODE: 3,
    t.KIND_CSV: 4,
    t.KIND_JSON: 5,
    t.KIND_XML: 6,
    t.KIND_HTML: 7,
    t.KIND_PDF: 8,
    t.KIND_DOCX: 9,
    t.KIND_XLSX: 10,
    t.KIND_PPTX: 11,
    t.KIND_DOC: 12,
    t.KIND_XLS: 13,
    t.KIND_PPT: 14,
    t.KIND_RTF: 15,
    t.KIND_ODT: 16,
    t.KIND_ODS: 17,
    t.KIND_ODP: 18,
    t.KIND_EPUB: 19,
    t.KIND_EML: 20,
    t.KIND_MSG: 21,
    t.KIND_IMAGE: 22,
    t.KIND_AUDIO: 23,
    t.KIND_VIDEO: 24,
    t.KIND_ARCHIVE: 25,
    t.KIND_EXECUTABLE: 26,
    t.KIND_DATABASE: 27,
    t.KIND_FONT: 28,
    t.KIND_BUNDLE: 29,
    t.KIND_ENCRYPTED: 30,
    t.KIND_OTHER: 31,
}

CTYPE_CODES: dict[str, int] = {
    t.CTYPE_BODY: 1,
    t.CTYPE_FILE_CARD: 2,
    t.CTYPE_FOLDER_CARD: 3,
    t.CTYPE_CAPTION: 4,
    t.CTYPE_OCR: 5,
}


def kind_code(kind: str | None) -> int:
    """Code for a file kind. ``None`` (a folder card) is :data:`KIND_NONE`;
    a kind this table does not know yet files under ``other`` rather than
    failing the write."""
    if kind is None:
        return KIND_NONE
    return KIND_CODES.get(kind, KIND_CODES[t.KIND_OTHER])


def ctype_code(ctype: str) -> int:
    """Code for a chunk type. Unknown types raise: the set is closed and
    written only by our own chunker, so an unknown one is a bug to surface."""
    try:
        return CTYPE_CODES[ctype]
    except KeyError:
        raise ValueError(f"unknown chunk type {ctype!r}") from None


def source_code(source: str) -> int:
    try:
        return SOURCE_CODES[source]
    except KeyError:
        raise ValueError(f"unknown source {source!r}") from None


def epoch_day(ts: float) -> int:
    """Days since 1970-01-01 (UTC) for a POSIX timestamp — the ``d`` scale."""
    return int(ts // 86400)


def make_payload(
    *,
    source: str,
    ctype: str,
    file_id: int | None = None,
    folder_id: int | None = None,
    kind: str | None = None,
    day: int | None = None,
) -> dict[str, int]:
    """The payload for one point. Unknown values are left out, not stored as
    null: Chroma drops null metadata anyway, and an absent ``d`` correctly
    fails every day-range filter."""
    payload: dict[str, int] = {
        P_SOURCE: source_code(source),
        P_KIND: kind_code(kind),
        P_CTYPE: ctype_code(ctype),
    }
    if file_id is not None:
        payload[P_FILE] = int(file_id)
    if folder_id is not None:
        payload[P_FOLDER] = int(folder_id)
    if day is not None:
        payload[P_DAY] = int(day)
    return payload


@dataclass
class VectorFilter:
    """Restrict a vector search. Fields combine with AND; a list matches any
    of its values; ``None`` means "no restriction on this field".

    An EMPTY list means "none of them" and matches nothing (the adapters
    return no hits without querying): a scope that resolved to zero files
    must not widen to all files.
    """

    file_ids: list[int] | None = None
    sources: list[int] | None = None     # SOURCE_CODES values: 0 local, 1 drive
    kinds: list[int] | None = None       # KIND_CODES values
    ctypes: list[int] | None = None      # CTYPE_CODES values
    day_range: tuple[int, int] | None = None  # (first, last) epoch days, inclusive

    @property
    def matches_nothing(self) -> bool:
        return any(
            v is not None and len(v) == 0
            for v in (self.file_ids, self.sources, self.kinds, self.ctypes)
        )

    def conditions(self) -> list[tuple[str, str, Any]]:
        """Backend-neutral clauses: ``(key, "in", [ints])`` or
        ``(key, "range", (lo, hi))``. Each adapter translates these, so the
        payload key names live only in this module."""
        out: list[tuple[str, str, Any]] = []
        for key, values in (
            (P_FILE, self.file_ids),
            (P_SOURCE, self.sources),
            (P_KIND, self.kinds),
            (P_CTYPE, self.ctypes),
        ):
            if values is not None:
                out.append((key, "in", [int(v) for v in values]))
        if self.day_range is not None:
            lo, hi = self.day_range
            out.append((P_DAY, "range", (int(lo), int(hi))))
        return out


# ── Collection names ───────────────────────────────────────────────────────
COLLECTION_PREFIX = "doc_"
# Collections created before the rename; migrated to the new prefix at boot
# (app/documents/vector_migrate.py) and recognised until then.
LEGACY_COLLECTION_PREFIX = "ud_"
# Chroma's historical limit (3–63 characters); Qdrant allows far more.
MAX_COLLECTION_NAME = 63
# When the model part must be cut, keep this many hex chars of its hash so
# two long model keys that share a prefix still get different collections.
_MODEL_HASH_HEX = 6
_MIN_MODEL_PART = 8


def profile_tag(profile_uid: str) -> str:
    """The 12-hex-char tag a profile's collections start with (after ``doc_``).

    Hashed rather than the raw uuid so the name stays short; 48 bits make a
    collision between two profiles on one install practically impossible."""
    return hashlib.blake2b(str(profile_uid).encode("utf-8"), digest_size=6).hexdigest()


def _slug(s: str, allowed: str) -> str:
    s = re.sub(f"[^{allowed}]+", "-", (s or "").lower())
    return re.sub(r"-{2,}", "-", s).strip("-_")


def collection_name(profile_uid: str, epoch: str, model_key: str, dim: int) -> str:
    """``doc_<uid tag>_<epoch>_<model key>_<dim>_p<PROMPT_SCHEME>``, ≤ 63 chars.

    Every part is identity: a restored index DB gets a new epoch, a model or
    dimension change gets a new name, and so does a prompt-scheme change, so
    no index ever adopts vectors it did not write. If the name would exceed
    :data:`MAX_COLLECTION_NAME`, only the model part is shortened, and it keeps
    a hash of the full key so the shortening cannot merge two models.
    """
    # Letters and digits only, so ``parse_collection_name`` can split it back out.
    epoch_part = re.sub(r"[^a-z0-9]+", "", (epoch or "").lower())
    if not epoch_part:
        raise ValueError("collection epoch must contain letters or digits")
    model_part = _slug(model_key, "a-z0-9_-") or "model"
    head = f"{COLLECTION_PREFIX}{profile_tag(profile_uid)}_{epoch_part}_"
    tail = f"_{int(dim)}_p{PROMPT_SCHEME}"
    room = MAX_COLLECTION_NAME - len(head) - len(tail)
    if room < _MIN_MODEL_PART:
        raise ValueError(f"collection epoch {epoch!r} is too long")
    if len(model_part) > room:
        digest = hashlib.blake2b(model_key.encode("utf-8"), digest_size=_MODEL_HASH_HEX // 2).hexdigest()
        model_part = model_part[: room - len(digest) - 1].rstrip("-_") + "-" + digest
    return f"{head}{model_part}{tail}"


def parse_collection_name(name: str) -> tuple[str, str] | None:
    """``(uid tag, epoch)`` of a documents collection name, or ``None`` when
    ``name`` is not one. For garbage collection: a ``doc_*`` collection whose
    tag or epoch no live index claims is an orphan. A pre-rename ``ud_*`` name
    parses too, so an unmigrated collection is still kept or collected."""
    m = re.fullmatch(r"(?:doc|ud)_([0-9a-f]{12})_([a-z0-9]+)_.+_\d+_p\d+", name or "")
    if not m:
        return None
    return m.group(1), m.group(2)
