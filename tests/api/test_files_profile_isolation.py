"""One profile's exports are not another profile's to read.

The file routes have always been a path-traversal sandbox rather than an
authorization boundary: every authenticated profile may read anything under the
System Directory. That was survivable while the directory held working data and
credential stores were denied by name.

It stopped being survivable when ``cremind profile wizard finish`` and
``cremind config export`` started writing configuration files there. Those files
embed a live JWT, they live at a fully predictable path
(``<system dir>/<profile>/exports/cremind-<target>-config.md``), and the
download chip in chat resolves through the very route that would serve them to
anyone — so hiding the directory by name, the way credential stores are hidden,
would break the feature instead of protecting it.

Hence a narrow rule: ``<system dir>/<p>/exports/`` belongs to ``p``. Admin is
not exempt, because its own exports are in its own slice and the exemption would
only hand the most prompt-injectable profile a reader for everyone else's
tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import files as files_api


@pytest.fixture
def system_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(files_api.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(files_api, "get_user_working_directory", lambda: str(tmp_path / "work"))
    (tmp_path / "admin" / "exports").mkdir(parents=True)
    (tmp_path / "admin" / "exports" / "cremind-javis-config.md").write_text("token", encoding="utf-8")
    (tmp_path / "admin" / "uploads_tmp").mkdir(parents=True)
    (tmp_path / "admin" / "uploads_tmp" / "note.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "shared").mkdir()
    (tmp_path / "work").mkdir()
    return tmp_path


def _export(system_dir: Path) -> str:
    return os.path.realpath(system_dir / "admin" / "exports" / "cremind-javis-config.md")


def test_another_profile_cannot_reach_an_export(system_dir: Path) -> None:
    assert files_api._is_inside_allowed(_export(system_dir), None, "bob") is False


def test_the_owner_can_reach_its_own_export(system_dir: Path) -> None:
    """The download chip in chat goes through this route — denying the owner
    would break the hand-off this whole feature exists for."""
    assert files_api._is_inside_allowed(_export(system_dir), None, "admin") is True


def test_admin_is_not_exempt_from_another_profiles_export(system_dir: Path) -> None:
    (system_dir / "bob" / "exports").mkdir(parents=True)
    target = os.path.realpath(system_dir / "bob" / "exports" / "cremind-bob-config.md")
    assert files_api._is_inside_allowed(target, None, "admin") is False


def test_a_route_that_forgets_to_pass_a_profile_fails_closed(system_dir: Path) -> None:
    """The default has to deny: a forgotten argument should surface as a 403 in
    a test, not as someone else's token on the wire."""
    assert files_api._is_inside_allowed(_export(system_dir)) is False


def test_the_rule_is_narrow_and_leaves_the_rest_of_the_subtree_alone(system_dir: Path) -> None:
    """Deliberately not the whole ``<system dir>/<profile>/`` slice: a group
    chat renders every member agent's file tree out of it, and widening this
    needs its own audit."""
    other = os.path.realpath(system_dir / "admin" / "uploads_tmp" / "note.txt")
    assert files_api._is_inside_allowed(other, None, "bob") is True
    shared = os.path.realpath(system_dir / "shared")
    assert files_api._is_inside_allowed(shared, None, "bob") is True


def test_a_wizard_draft_is_denied_to_everyone_including_its_owner(system_dir: Path) -> None:
    """Drafts hold the API key the user pasted, until ``finish`` posts it.
    Nothing legitimately browses them — the CLI reads them straight off disk."""
    draft = system_dir / "admin" / "cli-wizards"
    draft.mkdir(parents=True)
    (draft / "javis.json").write_text("{}", encoding="utf-8")
    target = os.path.realpath(draft / "javis.json")
    assert files_api._is_inside_allowed(target, None, "admin") is False
    assert files_api._is_inside_allowed(target, None, "bob") is False
    # The legacy ``/api/files/{path}`` route resolves separately; it must agree.
    assert files_api._safe_resolve("admin/cli-wizards/javis.json", "admin") is None


def test_the_legacy_path_route_applies_the_same_rule(system_dir: Path) -> None:
    assert files_api._safe_resolve("admin/exports/cremind-javis-config.md", "bob") is None
    assert files_api._safe_resolve("admin/exports/cremind-javis-config.md", "admin") is not None


def test_a_traversal_out_of_a_private_directory_is_still_refused(system_dir: Path) -> None:
    """The rule reads resolved paths, so ``..`` cannot walk sideways into one
    and a symlink cannot walk out of one."""
    sneaky = os.path.join(
        str(system_dir), "shared", "..", "admin", "exports", "cremind-javis-config.md",
    )
    assert files_api._is_inside_allowed(os.path.realpath(sneaky), None, "bob") is False


# ── Documentation search's index ───────────────────────────────────────────


def _index(system_dir: Path) -> Path:
    d = system_dir / "storage" / "documents" / "0f6c2a9e-uid-of-alice"
    d.mkdir(parents=True)
    (d / "index.db").write_bytes(b"SQLite format 3\0 alice's extracted document text")
    return d


def test_no_profile_can_read_the_documents_index_through_the_file_routes(system_dir: Path) -> None:
    """Every profile's index (the text of its files) sits under one directory
    of the system folder, which the file routes otherwise serve. Nobody gets
    it there — not another profile, not its owner, not admin: the index is
    read only through the profile-scoped /api/documentation-search routes."""
    d = _index(system_dir)
    target = os.path.realpath(d / "index.db")
    for who in ("bob", "admin", "alice"):
        assert files_api._is_inside_allowed(target, None, who) is False
        assert files_api._is_inside_allowed(os.path.realpath(d), None, who) is False
    assert files_api._safe_resolve("storage/documents/0f6c2a9e-uid-of-alice/index.db", "admin") is None
    assert files_api._safe_resolve("storage/documents", "bob") is None
    sneaky = os.path.join(str(system_dir), "shared", "..", "storage", "documents", d.name, "index.db")
    assert files_api._is_inside_allowed(os.path.realpath(sneaky), None, "bob") is False


def test_the_agents_file_tool_refuses_the_documents_index(system_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile whose working directory is the system folder (the built-in
    setup profile's default) must not reach another profile's index by a
    relative path."""
    from app.tools.builtin import system_file

    d = _index(system_dir)
    with pytest.raises(ValueError, match="internal index"):
        system_file._safe_resolve(str(system_dir), f"storage/documents/{d.name}/index.db")
    # The rest of the system folder is unaffected.
    assert system_file._safe_resolve(str(system_dir), "shared") == os.path.realpath(system_dir / "shared")


def test_the_pre_rename_index_root_is_protected_too(system_dir: Path) -> None:
    """An install not relocated yet (or a copy a conflict kept) still holds
    index files under ``storage/userdocs``."""
    from app.tools.builtin import system_file

    d = system_dir / "storage" / "userdocs" / "0f6c2a9e-uid-of-alice"
    d.mkdir(parents=True)
    (d / "index.db").write_bytes(b"SQLite format 3\0 alice's text")
    for who in ("bob", "admin", "alice"):
        assert files_api._is_inside_allowed(os.path.realpath(d / "index.db"), None, who) is False
    assert files_api._safe_resolve("storage/userdocs/0f6c2a9e-uid-of-alice/index.db", "admin") is None
    with pytest.raises(ValueError, match="internal index"):
        system_file._safe_resolve(str(system_dir), "storage/userdocs/0f6c2a9e-uid-of-alice/index.db")


# ── Cremind manual pages: owner only ────────────────────────────────────────

ALICE_UID, BOB_UID = "a1a1a1a1-0000-4000-8000-000000000001", "b2b2b2b2-0000-4000-8000-000000000002"


@pytest.fixture
def manuals(system_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two profiles' manual pages, and their rows' uuids."""
    from app.cremind_documents import paths as doc_paths

    uids = {"alice": ALICE_UID, "bob": BOB_UID}
    monkeypatch.setattr(doc_paths, "resolve_profile_uid", lambda p: uids.get(p))
    root = system_dir / "storage" / "cremind_documents" / "profiles"
    for uid, text in ((ALICE_UID, "alice's page"), (BOB_UID, "bob's page")):
        (root / uid).mkdir(parents=True)
        (root / uid / "page.md").write_text(text, encoding="utf-8")
    return root


def test_only_the_owner_reaches_its_manual_pages(manuals: Path) -> None:
    page = os.path.realpath(manuals / ALICE_UID / "page.md")
    assert files_api._is_inside_allowed(page, None, "alice") is True
    for who in ("bob", "admin", None):
        assert files_api._is_inside_allowed(page, None, who) is False, who
    rel = f"storage/cremind_documents/profiles/{ALICE_UID}/page.md"
    assert files_api._safe_resolve(rel, "alice") == page
    assert files_api._safe_resolve(rel, "bob") is None
    assert files_api._safe_resolve(rel, "admin") is None
    sneaky = os.path.join(str(manuals / BOB_UID), "..", ALICE_UID, "page.md")
    assert files_api._is_inside_allowed(os.path.realpath(sneaky), None, "bob") is False


def test_a_profile_with_no_row_reaches_no_manual_pages(manuals: Path) -> None:
    page = os.path.realpath(manuals / ALICE_UID / "page.md")
    assert files_api._is_inside_allowed(page, None, "ghost") is False


class _Req:
    def __init__(self, username: str, query=None, body=None):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.query_params = query or {}
        self.path_params = {}
        self._body = body or {}

    async def json(self):
        return self._body


def _call(handler, req) -> tuple[int, dict]:
    resp = asyncio.run(handler(req))
    return resp.status_code, json.loads(resp.body)


def test_the_listing_shows_a_profile_only_its_own_manual_directory(manuals: Path) -> None:
    status, body = _call(files_api._list_directory, _Req("bob", {"path": str(manuals)}))
    assert status == 200
    assert [e["name"] for e in body["entries"]] == [BOB_UID]

    status, body = _call(files_api._list_directory, _Req("bob", {"path": str(manuals / ALICE_UID)}))
    assert status == 403
    status, body = _call(files_api._list_directory, _Req("alice", {"path": str(manuals / ALICE_UID)}))
    assert status == 200 and [e["name"] for e in body["entries"]] == ["page.md"]


def test_the_listing_hides_both_index_roots(system_dir: Path) -> None:
    (system_dir / "storage" / "documents").mkdir(parents=True)
    (system_dir / "storage" / "userdocs").mkdir(parents=True)
    (system_dir / "storage" / "notes.txt").write_text("x", encoding="utf-8")
    status, body = _call(files_api._list_directory, _Req("admin", {"path": str(system_dir / "storage")}))
    assert status == 200
    assert {e["name"] for e in body["entries"]} == {"notes.txt"}


def test_a_parent_of_every_profiles_documents_cannot_be_moved_or_deleted(manuals: Path, system_dir: Path) -> None:
    for parent in (manuals, manuals.parent, system_dir / "storage"):
        status, _ = _call(files_api._delete_entry, _Req("alice", body={"path": str(parent)}))
        assert status == 403, parent
        status, _ = _call(files_api._move_entry, _Req("alice", body={
            "src": str(parent), "dest": str(system_dir / "work" / "stolen"),
        }))
        assert status == 403, parent
    assert (manuals / BOB_UID / "page.md").exists()
    # The owner still manages its own directory.
    status, _ = _call(files_api._delete_entry, _Req("alice", body={"path": str(manuals / ALICE_UID / "page.md")}))
    assert status == 200


def test_the_agents_file_tool_applies_the_owner_rule(manuals: Path, system_dir: Path) -> None:
    from app.tools.builtin import system_file

    rel = f"storage/cremind_documents/profiles/{ALICE_UID}/page.md"
    assert system_file._safe_resolve(str(system_dir), rel, profile="alice") == os.path.realpath(
        manuals / ALICE_UID / "page.md"
    )
    for who in ("bob", None):
        with pytest.raises(ValueError, match="another profile's Cremind manual"):
            system_file._safe_resolve(str(system_dir), rel, profile=who)
    # Working inside its own directory (change_working_directory 'documents').
    assert system_file._safe_resolve(str(manuals / BOB_UID), "page.md", profile="bob")


def test_the_agents_move_tool_refuses_a_parent_of_the_document_trees(manuals: Path, system_dir: Path) -> None:
    from app.tools.builtin import system_file

    for parent in ("storage", "storage/cremind_documents"):
        src, _dst, err = system_file._resolve_relocation(
            str(system_dir), parent, "work/stolen", False, {"_profile": "alice"},
        )
        assert src is None and err is not None, parent
        assert "other profiles' documents" in err.structured_content["message"]
    # Its own pages it may move (here: into its own directory's subfolder).
    src, dst, err = system_file._resolve_relocation(
        str(system_dir), f"storage/cremind_documents/profiles/{ALICE_UID}/page.md",
        f"storage/cremind_documents/profiles/{ALICE_UID}/renamed.md", False, {"_profile": "alice"},
    )
    assert err is None and src.endswith("page.md") and dst.endswith("renamed.md")


def _run_tool(tool_cls, system_dir: Path, profile: str | None, **args) -> dict:
    from app.tools.builtin import system_file

    tool = tool_cls(str(system_dir))
    arguments = {"_working_directory": str(system_dir), **args}
    if profile is not None:
        arguments["_profile"] = profile
    return asyncio.run(tool.run(arguments)).structured_content


def test_a_grep_rooted_at_the_manual_root_reads_only_the_callers_pages(manuals: Path, system_dir: Path) -> None:
    """``storage/cremind_documents/profiles`` itself belongs to nobody, so the
    path check lets a walk start there — the walk must then enter only the
    caller's own uuid directory."""
    from app.tools.builtin import system_file

    root = "storage/cremind_documents/profiles"
    for who, own, other in (("alice", ALICE_UID, BOB_UID), ("bob", BOB_UID, ALICE_UID)):
        res = _run_tool(system_file.GrepFilesTool, system_dir, who, pattern="page", path=root)
        assert res["files"] == [f"{root}/{own}/page.md"], (who, res)
        res = _run_tool(system_file.GrepFilesTool, system_dir, who, pattern="page", path=root,
                        output_mode="content")
        assert all(other not in m["path"] for m in res.get("matches", [])), res
    # No profile known: nobody's pages.
    res = _run_tool(system_file.GrepFilesTool, system_dir, None, pattern="page", path=root)
    assert res.get("files") == [], res


def test_a_name_search_rooted_at_the_manual_root_finds_only_the_callers_pages(manuals: Path, system_dir: Path) -> None:
    from app.tools.builtin import system_file

    res = _run_tool(system_file.SearchFilesTool, system_dir, "alice",
                    query="page", path="storage/cremind_documents/profiles")
    assert [e["path"] for e in res["results"]] == [f"storage/cremind_documents/profiles/{ALICE_UID}/page.md"]
    # A search for the uuid names themselves only ever sees the caller's.
    res = _run_tool(system_file.SearchFilesTool, system_dir, "alice",
                    query="0000", path="storage/cremind_documents/profiles", type="directory")
    assert [e["name"] for e in res["results"]] == [ALICE_UID]


def test_a_walk_through_the_manual_root_from_above_enters_only_the_callers_directory(
    manuals: Path, system_dir: Path,
) -> None:
    from app.tools.builtin import system_file

    (manuals / "stray.md").write_text("nobody's page", encoding="utf-8")
    walked = []
    prune = system_file._protected_tree_pruner(str(system_dir), "bob")
    for dirpath, dirnames, filenames in os.walk(str(system_dir)):
        prune(dirpath, dirnames, filenames)
        walked.extend(os.path.join(dirpath, f) for f in filenames)
    manual_files = [p for p in walked if "cremind_documents" in p]
    assert manual_files == [str(manuals / BOB_UID / "page.md")], manual_files


def test_list_files_shows_only_the_callers_manual_directory_and_no_index(manuals: Path, system_dir: Path) -> None:
    """The agent's ``list_files`` filters entries exactly like /api/files/list."""
    from app.tools.builtin import system_file

    for who, own in (("alice", ALICE_UID), ("bob", BOB_UID)):
        res = _run_tool(system_file.ListFilesTool, system_dir, who, path="storage/cremind_documents/profiles")
        assert [e["name"] for e in res["entries"]] == [own], (who, res)
    res = _run_tool(system_file.ListFilesTool, system_dir, None, path="storage/cremind_documents/profiles")
    assert res["entries"] == []

    (system_dir / "storage" / "documents").mkdir(parents=True)
    (system_dir / "storage" / "userdocs").mkdir(parents=True)
    res = _run_tool(system_file.ListFilesTool, system_dir, "alice", path="storage")
    assert {e["name"] for e in res["entries"]} == {"cremind_documents"}


# ── case-insensitive filesystems off Windows ────────────────────────────────
#
# macOS's default APFS and a Docker Desktop bind mount are case-insensitive,
# yet ``normcase`` is the identity off Windows and ``realpath`` keeps the case
# it was given: ``<SYS>/STORAGE/documents`` is the index store there, spelled
# differently. The tests simulate that POSIX ``normcase`` on any host and ask
# the filesystem (a stand-in for ``samestat``) whether the spelling is the same
# directory.


@pytest.fixture
def posix_case(monkeypatch: pytest.MonkeyPatch):
    from app.utils import credential_paths

    monkeypatch.setattr(credential_paths, "_normcase", lambda s: s)
    return credential_paths


def _variant(system_dir: Path, *parts: str) -> str:
    """``<SYS>/STORAGE/<parts…>`` as a resolved string (not realpath'd again,
    which on Windows would restore the on-disk case)."""
    return os.path.join(os.path.realpath(system_dir), "STORAGE", *parts)


def test_a_differently_cased_index_path_is_still_the_index(system_dir: Path, posix_case, monkeypatch) -> None:
    d = _index(system_dir)
    root = os.path.join(os.path.realpath(system_dir), "storage", "documents")
    target = _variant(system_dir, "documents", d.name, "index.db")

    # Case-insensitive filesystem: the same directory.
    monkeypatch.setattr(posix_case, "_same_file", lambda a, b: True)
    assert posix_case.parts_below(target, root) == [d.name, "index.db"]
    assert posix_case.is_documents_index_path(target, str(system_dir))
    assert posix_case.DocumentTreeGuard(str(system_dir)).hides(target)

    # Case-sensitive filesystem: another directory, not the index.
    monkeypatch.setattr(posix_case, "_same_file", lambda a, b: False)
    assert posix_case.parts_below(target, root) is None
    # An exact spelling never asks the disk.
    monkeypatch.setattr(posix_case, "_same_file", lambda a, b: pytest.fail("asked the disk"))
    assert posix_case.parts_below(os.path.join(root, d.name), root) == [d.name]


def test_a_differently_cased_path_to_another_profiles_pages_is_still_theirs(
    manuals: Path, system_dir: Path, posix_case, monkeypatch,
) -> None:
    monkeypatch.setattr(posix_case, "_same_file", lambda a, b: True)
    target = _variant(system_dir, "cremind_documents", "profiles", BOB_UID, "page.md")
    assert posix_case.same_uid(posix_case.authored_docs_owner(target, str(system_dir)), BOB_UID)
    assert files_api._is_other_profiles_authored_docs(target, "alice") is True
    assert files_api._is_other_profiles_authored_docs(target, "bob") is False
    # A differently cased uuid is the same directory too, and still bob's.
    upper = _variant(system_dir, "cremind_documents", "profiles", BOB_UID.upper(), "page.md")
    assert files_api._is_other_profiles_authored_docs(upper, "alice") is True
    # The per-entry filter of a listing or walk agrees.
    hides = files_api._document_trees_matcher("alice")
    assert hides(_variant(system_dir, "cremind_documents", "profiles", BOB_UID))
    assert not hides(_variant(system_dir, "cremind_documents", "profiles", ALICE_UID))
    # And moving a differently cased parent is refused like the real one.
    assert files_api._holds_protected_document_tree(os.path.join(os.path.realpath(system_dir), "STORAGE"))


def test_the_real_filesystem_check_on_this_host(system_dir: Path, posix_case) -> None:
    """``_same_file`` itself: on a case-insensitive host (Windows, macOS) the
    other spelling IS the directory; on a case-sensitive one it does not exist
    and is not."""
    d = _index(system_dir)
    real = os.path.join(os.path.realpath(system_dir), "storage", "documents")
    variant = _variant(system_dir, "documents")
    insensitive = os.path.exists(variant)
    assert posix_case._same_file(variant, real) is insensitive
    if insensitive:
        assert posix_case.is_documents_index_path(os.path.join(variant, d.name), str(system_dir))


def test_a_recursive_search_from_above_never_enters_the_document_trees(manuals: Path, system_dir: Path) -> None:
    """``search_files`` / ``grep_files`` rooted at the system folder must not
    walk into the index store or anyone's manual pages."""
    from app.tools.builtin import system_file

    (system_dir / "storage" / "documents" / "u").mkdir(parents=True)
    (system_dir / "storage" / "documents" / "u" / "page.md").write_text("index", encoding="utf-8")
    walked = []
    prune = system_file._protected_tree_pruner(str(system_dir))
    for dirpath, dirnames, filenames in os.walk(str(system_dir)):
        prune(dirpath, dirnames)
        walked.extend(os.path.join(dirpath, f) for f in filenames)
    assert not any("cremind_documents" in p or os.path.join("storage", "documents") in p for p in walked)
    assert any(p.endswith("cremind-javis-config.md") for p in walked)
