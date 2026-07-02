"""Embedding-text composition and its coupling to the content hash.

``documentation_search`` embeds a doc's *identity* (its filename, minus any
leading ``[tag]``) together with its ``description`` so the doc's own name
(e.g. ``profile``) carries weight in the vector instead of being drowned out
by boilerplate shared across descriptions. The content hash must cover
*exactly* the embedded string — otherwise ``full_reconcile`` would skip
re-embedding when only the embedded formula changed, leaving stale vectors.
These tests pin both properties.
"""

from __future__ import annotations

from app.documents.parser import parse_document
from app.documents.sync import (
    DocumentSyncService,
    _clean_name,
    _embedding_text,
    _hash_text,
)


def test_clean_name_strips_leading_tag():
    assert _clean_name("[cli]cremind profile") == "cremind profile"
    assert _clean_name("[cli] cremind conv") == "cremind conv"
    assert _clean_name("document") == "document"
    # No usable identity left -> empty, so _embedding_text falls back to desc.
    assert _clean_name("[cli]") == ""


def test_embedding_text_leads_with_identity():
    et = _embedding_text("[cli]cremind profile", "Create, list, delete profiles.")
    assert et.startswith("cremind profile")
    assert "Create, list, delete profiles." in et
    # With no usable name, embed the description alone (never a bare separator).
    assert _embedding_text("[cli]", "Just a desc.") == "Just a desc."


def test_embedding_text_discriminates_by_name():
    # Same description, different doc name -> different embedded text, so the
    # doc identity actually contributes to the vector.
    a = _embedding_text("[cli]cremind profile", "shared desc")
    b = _embedding_text("[cli]cremind conv", "shared desc")
    assert a != b


def _write_doc(root, stem, description, body):
    docdir = root / "documents"
    docdir.mkdir(parents=True, exist_ok=True)
    path = docdir / f"{stem}.md"
    path.write_text(f'---\ndescription: "{description}"\n---\n\n{body}\n', encoding="utf-8")
    return path


def test_scan_scope_hash_covers_embedded_text(tmp_path):
    """The stored content_hash must hash the *embedded* text, not the bare
    description — and must differ from the old description-only formula, so
    existing points re-embed once on the next reconcile."""
    svc = DocumentSyncService(working_dir=tmp_path, vector_store=None, embedding=None)
    stem = "[cli]cremind profile"
    desc = "Create, list, and delete profiles."
    path = _write_doc(tmp_path, stem, desc, "Some body text.")

    state = svc._scan_scope("shared")
    assert len(state) == 1
    (payload,) = state.values()

    parsed = parse_document(path)
    expected_embed = _embedding_text(stem, desc)

    # The transient carries exactly what full_reconcile will embed.
    assert payload["_embed_text"] == expected_embed
    assert payload["_embed_text"].startswith("cremind profile")

    # content_hash covers the embedded text (identity-aware), not bare desc.
    assert payload["content_hash"] == _hash_text(expected_embed + "\0" + parsed.body)

    # Regression guard: the new formula differs from the old description-only
    # one, guaranteeing every existing point re-embeds after this change.
    old_formula = _hash_text(desc + "\0" + parsed.body)
    assert payload["content_hash"] != old_formula


def _write_bundle(bundle_dir, stem, description):
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / f"{stem}.md").write_text(
        f'---\ndescription: "{description}"\n---\n\nbody for {stem}\n',
        encoding="utf-8",
    )


def test_seed_folds_cli_docs_into_shared_and_retires_legacy_scope(tmp_path):
    """The retired ``cli`` built-in tool once routed ``[cli]*`` docs into a
    disjoint ``cli`` scope. They now fold into the shared corpus: seed mirrors
    every bundled doc into ``shared_dir()``, removes any legacy ``cli`` tree
    from an upgraded install, and the docs are reachable from the default
    (shared + profile) search scope."""
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, "document", "General docs overview.")
    _write_bundle(bundle, "[cli]cremind profile", "Create, list, delete profiles via the CLI.")

    working = tmp_path / "work"
    # Simulate an upgraded install still carrying a legacy cli scope on disk.
    legacy = working / "cli" / "documents"
    legacy.mkdir(parents=True)
    (legacy / "[cli]cremind profile.md").write_text(
        '---\ndescription: "stale"\n---\n\nstale body\n', encoding="utf-8",
    )

    svc = DocumentSyncService(working_dir=working, vector_store=None, embedding=None)
    svc.seed_shared_from_app(bundle)

    # Legacy cli tree is gone; every bundled doc (incl. [cli]*) lands in shared.
    assert not (working / "cli").exists()
    shared_names = {p.name for p in svc.shared_dir().glob("*.md")}
    assert shared_names == {"document.md", "[cli]cremind profile.md"}

    # Degraded (no vector store) full-scan search over the DEFAULT scopes
    # surfaces the CLI-reference doc — proving fold-back, not isolation.
    hits = svc.search(query="create a profile", profile="admin")
    names = {h.get("name") for h in hits}
    assert "[cli]cremind profile" in names


def test_prune_scope_is_noop_without_vector_store(tmp_path):
    """``prune_scope`` (the one-shot legacy-``cli`` point cleanup) is a safe
    no-op when no vector store is configured."""
    svc = DocumentSyncService(working_dir=tmp_path, vector_store=None, embedding=None)
    svc.prune_scope("cli")  # must not raise
