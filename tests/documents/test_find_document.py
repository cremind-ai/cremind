"""Name resolution behind the ``read_documentation_section`` leaf.

``DocumentSyncService.find_document`` turns an AGENT-SUPPLIED document name into
one of the rows this service itself published, and ``list_document_names`` is
the corpus it resolves against (and the "did you mean" list). These run against
a real service on ``tmp_path`` — no vector store is involved in either.

Two properties matter more than the convenience matching:

- the name is never used to build a path, so anything path-shaped resolves to
  nothing at all;
- scopes are the caller's ``[shared, profile]`` and nothing else, so one
  profile can never read — or even see the name of — another profile's doc,
  and a profile can never shadow a shared doc of the same name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.documents.sync import SHARED_SCOPE, DocumentSyncService


def _write(directory: Path, stem: str, description: str, body: str = "Body.\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.md"
    path.write_text(
        f'---\ndescription: "{description}"\n---\n\n{body}', encoding="utf-8",
    )
    return path


@pytest.fixture
def svc(tmp_path) -> DocumentSyncService:
    return DocumentSyncService(working_dir=tmp_path)


@pytest.fixture
def shared(tmp_path) -> Path:
    return tmp_path / "documents"


def _profile_dir(tmp_path: Path, profile: str) -> Path:
    return tmp_path / profile / "documents"


# ---------------------------------------------------------------------------
# convenience matching
# ---------------------------------------------------------------------------


def test_exact_stem_resolves(svc, shared):
    path = _write(shared, "[cli]cremind widgets", "Manage widgets")
    _write(shared, "[cli]cremind gadgets", "Manage gadgets")

    row = svc.find_document("[cli]cremind widgets", [SHARED_SCOPE, "admin"])

    assert row is not None
    assert row["name"] == "[cli]cremind widgets"
    assert row["scope"] == SHARED_SCOPE
    assert Path(row["file_path"]).resolve() == path.resolve()
    assert row["description"] == "Manage widgets"


@pytest.mark.parametrize(
    "asked",
    ["[CLI]Cremind Widgets", "[cli]CREMIND WIDGETS", "  [cli]cremind widgets  "],
)
def test_matching_is_case_insensitive_and_trimmed(svc, shared, asked):
    _write(shared, "[cli]cremind widgets", "Manage widgets")

    row = svc.find_document(asked, [SHARED_SCOPE, "admin"])

    assert row is not None and row["name"] == "[cli]cremind widgets"


@pytest.mark.parametrize("asked", ["cremind widgets", "Cremind Widgets", "[tool]cremind widgets"])
def test_the_bracketed_tag_is_optional(svc, shared, asked):
    # The agent often drops (or mangles) the "[cli]" filename tag.
    _write(shared, "[cli]cremind widgets", "Manage widgets")

    row = svc.find_document(asked, [SHARED_SCOPE, "admin"])

    assert row is not None and row["name"] == "[cli]cremind widgets"


@pytest.mark.parametrize(
    "asked", ["[cli]cremind widgets.md", "cremind widgets.MD", "[CLI]cremind widgets.Md"],
)
def test_a_trailing_md_extension_is_tolerated(svc, shared, asked):
    _write(shared, "[cli]cremind widgets", "Manage widgets")

    row = svc.find_document(asked, [SHARED_SCOPE, "admin"])

    assert row is not None and row["name"] == "[cli]cremind widgets"


def test_a_tagless_name_two_docs_share_is_ambiguous_but_the_tagged_name_is_not(svc, shared):
    # Without its tag, "cremind widgets" fits both documents, so neither is
    # picked silently; with the tag it names exactly one.
    _write(shared, "[cli]cremind widgets", "CLI reference")
    _write(shared, "cremind widgets", "Prose guide")

    both = svc.resolve_document("cremind widgets", [SHARED_SCOPE])
    assert sorted(r["name"] for r in both) == ["[cli]cremind widgets", "cremind widgets"]
    assert svc.find_document("cremind widgets", [SHARED_SCOPE]) is None
    assert svc.find_document("[cli]cremind widgets", [SHARED_SCOPE])["name"] == "[cli]cremind widgets"


def test_nested_docs_resolve_by_stem_only(svc, shared):
    _write(shared / "guides", "deep dive", "A nested guide")

    row = svc.find_document("deep dive", [SHARED_SCOPE])

    assert row is not None
    assert row["relpath"] == "guides/deep dive.md"
    # ...but never by a path, even the doc's own relpath.
    assert svc.find_document("guides/deep dive", [SHARED_SCOPE]) is None


def test_unknown_or_ineligible_names_resolve_to_none(svc, shared):
    _write(shared, "[cli]cremind widgets", "Manage widgets")
    # A file without frontmatter is not a document at all.
    shared.joinpath("draft.md").write_text("# no frontmatter\n", encoding="utf-8")

    scopes = [SHARED_SCOPE, "admin"]
    assert svc.find_document("cremind widget", scopes) is None
    assert svc.find_document("draft", scopes) is None


def test_an_empty_corpus_resolves_nothing(svc):
    assert svc.find_document("anything", [SHARED_SCOPE, "admin"]) is None
    assert svc.list_document_names([SHARED_SCOPE, "admin"]) == []


# ---------------------------------------------------------------------------
# the name is never a path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asked",
    [
        "",
        "   ",
        None,
        "a/b",
        "a\\b",
        "../x",
        "..",
        "..\\x",
        "documents/[cli]cremind widgets",
        "documents\\[cli]cremind widgets",
        "../admin/documents/secret",
        "..secret",
    ],
)
def test_path_shaped_names_are_rejected(svc, shared, tmp_path, asked):
    _write(shared, "[cli]cremind widgets", "Manage widgets")
    _write(_profile_dir(tmp_path, "admin"), "secret", "Admin only")
    # A file OUTSIDE every scope, reachable only by traversal.
    _write(tmp_path, "x", "Outside every scope")

    assert svc.find_document(asked, [SHARED_SCOPE, "bob"]) is None


def test_an_absolute_path_to_a_real_doc_is_rejected(svc, tmp_path):
    path = _write(_profile_dir(tmp_path, "admin"), "secret", "Admin only")

    assert svc.find_document(str(path), [SHARED_SCOPE, "admin"]) is None
    assert svc.find_document(path.as_posix(), [SHARED_SCOPE, "admin"]) is None


# ---------------------------------------------------------------------------
# profile isolation
# ---------------------------------------------------------------------------


def test_a_profile_doc_is_visible_only_to_its_own_profile(svc, shared, tmp_path):
    _write(shared, "shared guide", "Everyone can read this")
    admin_path = _write(_profile_dir(tmp_path, "admin"), "admin notes", "Admin only")

    row = svc.find_document("admin notes", [SHARED_SCOPE, "admin"])
    assert row is not None
    assert row["scope"] == "admin"
    assert Path(row["file_path"]).resolve() == admin_path.resolve()

    # Every matching pass (exact, case-insensitive, tag-insensitive) must miss.
    for asked in ("admin notes", "ADMIN NOTES", "[x]admin notes", "admin notes.md"):
        assert svc.find_document(asked, [SHARED_SCOPE, "bob"]) is None

    # Shared docs stay visible to both.
    assert svc.find_document("shared guide", [SHARED_SCOPE, "bob"])["scope"] == SHARED_SCOPE
    assert svc.find_document("shared guide", [SHARED_SCOPE, "admin"])["scope"] == SHARED_SCOPE


def test_list_document_names_never_lists_another_profiles_docs(svc, shared, tmp_path):
    _write(shared, "shared guide", "Everyone")
    _write(_profile_dir(tmp_path, "admin"), "admin notes", "Admin only")
    _write(_profile_dir(tmp_path, "bob"), "bob notes", "Bob only")

    bob_names = {row["name"] for row in svc.list_document_names([SHARED_SCOPE, "bob"])}
    admin_names = {row["name"] for row in svc.list_document_names([SHARED_SCOPE, "admin"])}

    assert bob_names == {"shared guide", "bob notes"}
    assert admin_names == {"shared guide", "admin notes"}


def test_the_same_name_in_shared_and_profile_scope_is_ambiguous(svc, shared, tmp_path):
    """Regression: the shared copy used to win silently, so an envelope built
    for the PROFILE's copy sent the section reader to a different document."""
    shared_path = _write(shared, "[cli]cremind widgets", "The real reference")
    admin_path = _write(_profile_dir(tmp_path, "admin"), "[cli]cremind widgets", "A profile's copy")

    for asked in ("[cli]cremind widgets", "[CLI]CREMIND WIDGETS", "cremind widgets"):
        assert svc.find_document(asked, [SHARED_SCOPE, "admin"]) is None, asked
        assert {r["scope"] for r in svc.resolve_document(asked, [SHARED_SCOPE, "admin"])} == {
            SHARED_SCOPE, "admin",
        }

    # Each copy is still reachable, exactly, by its qualified reference.
    shared_row = svc.find_document("shared/[cli]cremind widgets.md", [SHARED_SCOPE, "admin"])
    admin_row = svc.find_document("admin/[cli]cremind widgets.md", [SHARED_SCOPE, "admin"])
    assert Path(shared_row["file_path"]).resolve() == shared_path.resolve()
    assert Path(admin_row["file_path"]).resolve() == admin_path.resolve()


def test_reference_for_is_the_bare_name_unless_that_name_is_shared(svc, shared, tmp_path):
    _write(shared, "[cli]cremind widgets", "The real reference")
    _write(shared, "unique guide", "Only one of these")
    _write(_profile_dir(tmp_path, "admin"), "[cli]cremind widgets", "A profile's copy")
    scopes = [SHARED_SCOPE, "admin"]

    assert svc.reference_for(scope=SHARED_SCOPE, relpath="unique guide.md", scopes=scopes) == "unique guide"
    assert (
        svc.reference_for(scope="admin", relpath="[cli]cremind widgets.md", scopes=scopes)
        == "admin/[cli]cremind widgets.md"
    )
    # And whatever it returns resolves back to exactly that document.
    for scope, relpath in ((SHARED_SCOPE, "unique guide.md"), ("admin", "[cli]cremind widgets.md")):
        ref = svc.reference_for(scope=scope, relpath=relpath, scopes=scopes)
        row = svc.find_document(ref, scopes)
        assert (row["scope"], row["relpath"]) == (scope, relpath)


def test_a_qualified_reference_cannot_reach_another_profile(svc, shared, tmp_path):
    _write(_profile_dir(tmp_path, "bob"), "secret", "Bob's own")

    assert svc.find_document("bob/secret.md", [SHARED_SCOPE, "admin"]) is None
    assert svc.find_document("bob/secret.md", [SHARED_SCOPE, "bob"]) is not None


def test_a_name_containing_dots_is_found(svc, shared):
    """Regression: any name containing '..' was rejected as a path, so a doc
    titled with an ellipsis could be searched but never read."""
    _write(shared, "Getting started... with Cremind", "An ellipsis in the name")

    row = svc.find_document("Getting started... with Cremind", [SHARED_SCOPE])

    assert row is not None and row["name"] == "Getting started... with Cremind"


def test_ambiguous_matches_come_back_in_the_scope_order_given(svc, shared, tmp_path):
    _write(shared, "guide", "shared copy")
    _write(_profile_dir(tmp_path, "admin"), "guide", "admin copy")

    assert [r["scope"] for r in svc.resolve_document("guide", [SHARED_SCOPE, "admin"])] == [
        SHARED_SCOPE, "admin",
    ]
    assert [r["scope"] for r in svc.resolve_document("guide", ["admin", SHARED_SCOPE])] == [
        "admin", SHARED_SCOPE,
    ]
    assert svc.find_document("guide", [SHARED_SCOPE, "admin"]) is None


# ---------------------------------------------------------------------------
# list_document_names row shape and order
# ---------------------------------------------------------------------------


def test_list_document_names_rows_shared_first_then_profile(svc, shared, tmp_path):
    _write(shared, "zeta", "Shared Z")
    _write(shared, "[cli]cremind alpha", "Shared A")
    _write(shared / "sub", "nested", "Shared nested")
    shared.joinpath("not-a-doc.md").write_text("no frontmatter\n", encoding="utf-8")
    shared.joinpath("notes.txt").write_text('---\ndescription: "x"\n---\n', encoding="utf-8")
    _write(_profile_dir(tmp_path, "admin"), "aardvark", "Admin A")
    _write(_profile_dir(tmp_path, "admin"), "mine", "Admin M")

    rows = svc.list_document_names([SHARED_SCOPE, "admin"])

    assert [(r["scope"], r["relpath"]) for r in rows] == [
        # Shared rows first, even though "aardvark" sorts before all of them.
        (SHARED_SCOPE, "[cli]cremind alpha.md"),
        (SHARED_SCOPE, "sub/nested.md"),
        (SHARED_SCOPE, "zeta.md"),
        ("admin", "aardvark.md"),
        ("admin", "mine.md"),
    ]
    for row in rows:
        assert set(row) == {"name", "scope", "relpath", "file_path", "description"}
        assert row["name"] == Path(row["relpath"]).stem
        assert Path(row["file_path"]).is_file()
    by_name = {r["name"]: r for r in rows}
    assert by_name["[cli]cremind alpha"]["description"] == "Shared A"
    assert by_name["nested"]["description"] == "Shared nested"
    assert by_name["aardvark"]["description"] == "Admin A"
    assert Path(by_name["mine"]["file_path"]).resolve() == (
        _profile_dir(tmp_path, "admin") / "mine.md"
    ).resolve()


def test_list_document_names_skips_a_scope_with_no_directory(svc, shared):
    _write(shared, "only", "Only shared")

    rows = svc.list_document_names([SHARED_SCOPE, "ghost"])

    assert [r["name"] for r in rows] == ["only"]
