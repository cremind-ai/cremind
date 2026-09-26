"""Embedding-text composition and its coupling to the content hash.

``cremind_documentation_search`` embeds a doc's *identity* (its filename, minus any
leading ``[tag]``) together with its ``description`` so the doc's own name
(e.g. ``profile``) carries weight in the vector instead of being drowned out
by boilerplate shared across descriptions. The content hash must cover
*exactly* the embedded string — otherwise ``full_reconcile`` would skip
re-embedding when only the embedded formula changed, leaving stale vectors.
These tests pin both properties.
"""

from __future__ import annotations

from app.cremind_documents.parser import parse_document
from app.cremind_documents.sync import (
    CremindDocumentSyncService,
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


def _write_doc(docdir, stem, description, body):
    docdir.mkdir(parents=True, exist_ok=True)
    path = docdir / f"{stem}.md"
    path.write_text(f'---\ndescription: "{description}"\n---\n\n{body}\n', encoding="utf-8")
    return path


def _uid(profile: str) -> str:
    return f"uid-{profile}"


def test_scan_scope_hash_covers_embedded_text(tmp_path):
    """The stored content_hash must hash the *embedded* text, not the bare
    description — and must differ from the old description-only formula, so
    existing points re-embed once on the next reconcile."""
    svc = CremindDocumentSyncService(
        working_dir=tmp_path, vector_store=None, embedding=None, profile_uid_resolver=_uid,
    )
    stem = "[cli]cremind profile"
    desc = "Create, list, and delete profiles."
    path = _write_doc(svc.shared_dir(), stem, desc, "Some body text.")

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
    every bundled doc into ``shared_dir()``, the legacy ``cli`` tree of an
    upgraded install is retired, and the docs are reachable from the default
    (shared + profile) search scope."""
    from app.documents.relocate import retire_legacy_trees

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

    svc = CremindDocumentSyncService(
        working_dir=working, vector_store=None, embedding=None, profile_uid_resolver=_uid,
    )
    svc.seed_shared_from_app(bundle)
    # The seed itself touches nothing outside its own directory any more...
    assert legacy.exists()
    # ...the retirement (which knows no profile is named ``cli``) does.
    retire_legacy_trees(working, ["admin"])

    # Legacy cli tree is gone; every bundled doc (incl. [cli]*) lands in shared.
    assert not (working / "cli").exists()
    shared_names = {p.name for p in svc.shared_dir().glob("*.md")}
    assert shared_names == {"document.md", "[cli]cremind profile.md"}
    assert svc.shared_dir() == working / "storage" / "cremind_documents" / "shared"

    # Degraded (no vector store) full-scan search over the DEFAULT scopes
    # surfaces the CLI-reference doc — proving fold-back, not isolation.
    hits = svc.search(query="create a profile", profile="admin")
    names = {h.get("name") for h in hits}
    assert "[cli]cremind profile" in names


def test_a_profile_named_cli_keeps_its_tree(tmp_path):
    """The old boot code deleted ``<SYS>/cli`` wholesale — the entire tree of
    a profile named ``cli``. The retirement leaves a profile's tree alone."""
    from app.documents.relocate import retire_legacy_trees

    working = tmp_path / "work"
    (working / "cli" / "documents").mkdir(parents=True)
    (working / "cli" / "documents" / "notes.md").write_text("x", encoding="utf-8")
    (working / "cli" / "PERSONA.md").write_text("me", encoding="utf-8")
    svc = CremindDocumentSyncService(working_dir=working, profile_uid_resolver=_uid)
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, "document", "General docs overview.")
    svc.seed_shared_from_app(bundle)

    retire_legacy_trees(working, ["admin", "cli"])

    assert (working / "cli" / "PERSONA.md").read_text(encoding="utf-8") == "me"
    assert (working / "cli" / "documents" / "notes.md").exists()


def test_seed_writes_only_into_the_dedicated_shared_dir(tmp_path):
    """The mirror deletes every file in its target that is not in the bundle,
    so its target must be a directory nothing else owns: never a profile's
    tree (a profile named ``documents`` owned the old ``<SYS>/documents``)."""
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, "document", "General docs overview.")
    working = tmp_path / "work"
    # A profile named "documents" and another profile's own manual pages.
    (working / "documents" / "skills").mkdir(parents=True)
    (working / "documents" / "skills" / "SKILL.md").write_text("mine", encoding="utf-8")
    svc = CremindDocumentSyncService(working_dir=working, profile_uid_resolver=_uid)
    own = svc.profile_dir("alice")
    own.mkdir(parents=True)
    (own / "alice.md").write_text("alice", encoding="utf-8")

    svc.seed_shared_from_app(bundle)

    assert (working / "documents" / "skills" / "SKILL.md").read_text(encoding="utf-8") == "mine"
    assert (own / "alice.md").exists()
    assert {p.name for p in svc.shared_dir().rglob("*") if p.is_file()} == {"document.md"}


def test_profile_dir_is_keyed_by_uuid_and_skips_unknown_profiles(tmp_path):
    """Two profiles get two uuid directories; a profile whose uuid cannot be
    resolved gets NO directory — never a name-keyed guess."""
    uids = {"alice": "11111111-aaaa", "bob": "22222222-bbbb"}
    svc = CremindDocumentSyncService(working_dir=tmp_path, profile_uid_resolver=uids.get)
    root = tmp_path / "storage" / "cremind_documents" / "profiles"
    assert svc.profile_dir("alice") == root / "11111111-aaaa"
    assert svc.profile_dir("bob") == root / "22222222-bbbb"
    assert svc.profile_dir("ghost") is None
    assert svc.scope_dir("ghost") is None
    assert svc._scan_scope("ghost") == {}
    assert not (tmp_path / "ghost").exists()


def test_forget_profile_drops_the_cached_uuid(tmp_path):
    """A profile deleted and re-created under the same name has a new uuid;
    the service must not keep pointing at the old directory."""
    uids = {"alice": "old-uid"}
    svc = CremindDocumentSyncService(working_dir=tmp_path, profile_uid_resolver=lambda p: uids.get(p))
    assert svc.profile_dir("alice").name == "old-uid"
    uids["alice"] = "new-uid"
    assert svc.profile_dir("alice").name == "old-uid"  # cached while it exists
    svc.forget_profile("alice")
    assert svc.profile_dir("alice").name == "new-uid"


def test_prune_scope_is_noop_without_vector_store(tmp_path):
    """``prune_scope`` (the one-shot legacy-``cli`` point cleanup) is a safe
    no-op when no vector store is configured."""
    svc = CremindDocumentSyncService(
        working_dir=tmp_path, vector_store=None, embedding=None, profile_uid_resolver=_uid,
    )
    svc.prune_scope("cli")  # must not raise
