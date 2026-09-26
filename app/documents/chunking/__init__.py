"""Chunking for Documentation search: blocks in, embeddable chunks out.

- :mod:`.chunker` — structure-aware, content-defined cuts (why: an edit must
  leave the rest of the file's chunk hashes untouched).
- :mod:`.legal` — the article/clause overlay for statutes and contracts,
  cross-references and legal metadata.
- :mod:`.cards` — file/folder cards, image captions, OCR chunks.
- :mod:`.diff` — the hash-multiset diff that decides what gets embedded.

Pure functions over :mod:`app.documents.types`; no I/O, no DB, no model.
"""

from app.documents.chunking.cards import (
    make_caption_chunk,
    make_file_card,
    make_folder_card,
    make_ocr_chunks,
)
from app.documents.chunking.chunker import CHUNKER_VERSION, LEGAL_ONLY_BUMPS, chunk_blocks
from app.documents.chunking.diff import diff_chunks
from app.documents.chunking.legal import (
    detect_legal_meta,
    extract_refs,
    looks_legal,
    section_key_for,
)

__all__ = [
    "CHUNKER_VERSION",
    "LEGAL_ONLY_BUMPS",
    "chunk_blocks",
    "detect_legal_meta",
    "diff_chunks",
    "extract_refs",
    "looks_legal",
    "make_caption_chunk",
    "make_file_card",
    "make_folder_card",
    "make_ocr_chunks",
    "section_key_for",
]
