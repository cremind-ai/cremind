"""Degraded-mode (no vector search) full-scan fallback.

When the embedding model / vector store is unavailable, ``search`` falls back to
``_list_all_for_scopes`` and the LLM judge becomes the *only* discriminator, so
it must see the whole bounded system-doc set. A prior bug capped the fallback at
the vector ``top_k`` (10) after an alphabetical sort, so ``[cli]cremind profile``
(~#12) and the rest of the tail were silently hidden from the judge — the real
cause of "no relevant result found" for profile/setup/tools/usage/... queries.
These tests pin that the fallback now surfaces the full system-doc set and only
truncates (loudly) for a pathologically large per-profile corpus.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.documents.sync import DocumentSyncService, FALLBACK_MAX_CANDIDATES
from app.utils.logger import logger

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"


def _svc_with_system_docs(tmp_path):
    """A degraded-mode service (no vector store / embedding) seeded with the
    real bundled system docs under its shared ``documents/`` dir."""
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    count = 0
    for p in BUNDLED.glob("*.md"):
        shutil.copy2(p, docs / p.name)
        count += 1
    svc = DocumentSyncService(working_dir=tmp_path, vector_store=None, embedding=None)
    return svc, count


def test_degraded_fallback_includes_tail_sorted_docs(tmp_path):
    svc, count = _svc_with_system_docs(tmp_path)
    # limit=10 mirrors the tool's default top_k; the fallback must ignore it and
    # surface every system doc, not just the first 10 alphabetically.
    hits = svc.search(query="how to create a profile", profile="admin", limit=10)
    names = {h["name"] for h in hits}

    # The exact doc that was cut off before the fix (sorted ~#12 of 18).
    assert "[cli]cremind profile" in names
    # The rest of the alphabetical tail must be reachable too.
    for tail in ("[cli]cremind setup", "[cli]cremind tools", "[cli]cremind usage"):
        assert tail in names
    # Every eligible system doc reaches the judge.
    assert len(hits) == count


def test_fallback_caps_and_warns_on_oversized_profile_corpus(tmp_path):
    svc, sys_count = _svc_with_system_docs(tmp_path)

    # Give the profile scope more docs than the cap so truncation must kick in.
    pdir = tmp_path / "admin" / "documents"
    pdir.mkdir(parents=True)
    overflow = FALLBACK_MAX_CANDIDATES + 5
    for i in range(overflow):
        (pdir / f"doc{i:03d}.md").write_text(
            f'---\ndescription: "profile scratch doc {i}"\n---\n\nbody {i}\n',
            encoding="utf-8",
        )

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        hits = svc.search(query="anything", profile="admin", limit=10)
    finally:
        logger.remove(sink_id)

    # Capped, and the truncation is logged (never silent).
    assert len(hits) == FALLBACK_MAX_CANDIDATES
    assert any("truncated" in m for m in messages)
    # Shared/system docs are appended first, so they survive truncation.
    names = {h["name"] for h in hits}
    assert "[cli]cremind profile" in names
