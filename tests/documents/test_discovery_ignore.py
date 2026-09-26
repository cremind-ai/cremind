"""The ignore matcher: what the agent can never read, and how the user shapes the rest.

The layer order is the security property under test: a secret file or a
credential directory must stay unreadable whatever a ``.cremindignore`` says,
while the conveniences (tooling trees, hidden files) must stay overridable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.documents.discovery import ignore as ig
from app.documents.discovery.ignore import INDEX, METADATA_ONLY, SKIP, IgnoreMatcher, compile_rules


def _m(root: Path, **kw) -> IgnoreMatcher:
    return IgnoreMatcher(str(root), excludes=kw.pop("excludes", []), locked_excludes=kw.pop("locked", []), **kw)


def _cls(m: IgnoreMatcher, root: Path, rel: str, *, is_dir: bool = False) -> str:
    return m.classify(rel, str(root.joinpath(*rel.split("/"))), is_dir=is_dir)


# ── layer 1: non-overridable ───────────────────────────────────────────────


@pytest.mark.parametrize("name", [".ssh", ".aws", ".kube", ".gnupg", ".docker", "coding-cli", "codex-home", "cli-wizards"])
def test_credential_dirs_are_pruned_even_with_hidden_included(tmp_path, name):
    m = _m(tmp_path, include_hidden=True)
    assert m.prune_dir(name, str(tmp_path / name))
    assert _cls(m, tmp_path, f"{name}/config") == SKIP
    assert _cls(m, tmp_path, f"deep/er/{name}/x.txt") == SKIP


@pytest.mark.parametrize("name", [
    ".env", ".env.local", "server.pem", "tls.key", "cert.p12", "a.pfx", "store.jks", "k.keystore",
    "putty.ppk", "vault.kdbx", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ecdsa", "id_dsa",
    ".netrc", ".pgpass", ".npmrc", ".pypirc", ".git-credentials", "credentials.json",
    "credentials-prod.json", "credentials.toml", "token.json", ".google_token.json", "wallet.dat",
    "ID_RSA", "Server.PEM",
])
def test_secret_files_are_metadata_only(tmp_path, name):
    m = _m(tmp_path, include_hidden=True)
    assert _cls(m, tmp_path, f"proj/{name}") == METADATA_ONLY


def test_hidden_secret_is_skipped_when_hidden_files_are_off(tmp_path):
    # Skipping is stricter than metadata-only; either way the content is unread.
    assert _cls(_m(tmp_path), tmp_path, ".env") == SKIP


def test_cremindignore_negation_cannot_lift_layer_one(tmp_path):
    (tmp_path / ".cremindignore").write_text("!*.pem\n!.ssh/\n!id_rsa\n!.env\n", encoding="utf-8")
    m = _m(tmp_path, include_hidden=True)
    assert _cls(m, tmp_path, "keys/server.pem") == METADATA_ONLY
    assert _cls(m, tmp_path, "id_rsa") == METADATA_ONLY
    assert _cls(m, tmp_path, ".env") == METADATA_ONLY
    assert m.prune_dir(".ssh", str(tmp_path / ".ssh"))


def test_negation_can_reinclude_a_hidden_secret_only_as_metadata(tmp_path):
    (tmp_path / ".cremindignore").write_text("!.env\n", encoding="utf-8")
    assert _cls(_m(tmp_path), tmp_path, ".env") == METADATA_ONLY


def test_locked_excludes_and_system_dir_are_pruned(tmp_path):
    sysdir = tmp_path / "cremind-system"
    locked = tmp_path / "Private" / "Vault"
    m = _m(tmp_path, locked=[str(locked)], system_dir=str(sysdir))
    assert m.prune_dir("cremind-system", str(sysdir))
    assert m.prune_dir("Private/Vault", str(locked))
    assert not m.prune_dir("Private", str(tmp_path / "Private"))
    assert _cls(m, tmp_path, "cremind-system/admin/tokens/x.token") == SKIP
    assert _cls(m, tmp_path, "Private/Vault/a.txt") == SKIP
    assert _cls(m, tmp_path, "Private/b.txt") == INDEX


def test_a_root_inside_a_locked_dir_indexes_nothing(tmp_path):
    root = tmp_path / "sys" / "inside"
    root.mkdir(parents=True)
    m = _m(root, system_dir=str(tmp_path / "sys"))
    assert _cls(m, root, "a.txt") == SKIP


def test_a_sanctioned_root_inside_the_system_dir_indexes(tmp_path):
    """validate_root approved it (the profile's own default workspace,
    ``<SYS>/workspaces/<profile>``): the system dir no longer locks the root,
    but every other rule still applies inside it."""
    root = tmp_path / "sys" / "workspaces" / "dog"
    root.mkdir(parents=True)
    m = _m(root, system_dir=str(tmp_path / "sys"), root_sanctioned=True, include_hidden=True)
    assert _cls(m, root, "a.txt") == INDEX
    assert _cls(m, root, "keys/id_rsa") == METADATA_ONLY
    assert m.prune_dir(".ssh", str(root / ".ssh"))


def test_the_sanctioned_exemption_is_for_the_system_dir_only(tmp_path):
    """Another locked exclude holding the root (another profile's folder)
    still locks it, and the system dir itself is never a sanctioned root."""
    root = tmp_path / "sys" / "workspaces" / "dog"
    root.mkdir(parents=True)
    other = _m(root, locked=[str(tmp_path / "sys" / "workspaces")], system_dir=str(tmp_path / "sys"),
               root_sanctioned=True)
    assert _cls(other, root, "a.txt") == SKIP
    sysroot = _m(tmp_path / "sys", system_dir=str(tmp_path / "sys"), root_sanctioned=True)
    assert _cls(sysroot, tmp_path / "sys", "a.txt") == SKIP


# ── layer 2: defaults ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox",
    ".idea", ".vscode", "dist", "build", "target", ".next", ".cache", ".gradle", "$RECYCLE.BIN",
    "System Volume Information", ".Trash", ".Trashes", ".Trash-1000",
])
def test_default_dirs_are_pruned(tmp_path, name):
    m = _m(tmp_path, include_hidden=True)
    assert m.prune_dir(f"a/{name}", str(tmp_path / "a" / name))


@pytest.mark.parametrize("name", [
    ".DS_Store", "Thumbs.db", "desktop.ini", "~$report.docx", ".~lock.report.odt#", "a.tmp", "a.temp",
    "a.swp", "a.swo", "big.iso.part", "movie.crdownload", "m.pyc", "m.o", "Main.class",
])
def test_default_junk_files_are_skipped(tmp_path, name):
    assert _cls(_m(tmp_path, include_hidden=True), tmp_path, f"docs/{name}") == SKIP


def test_hidden_entries_follow_include_hidden(tmp_path):
    assert _cls(_m(tmp_path), tmp_path, ".notes.md") == SKIP
    assert _m(tmp_path).prune_dir(".obsidian", str(tmp_path / ".obsidian"))
    assert _cls(_m(tmp_path, include_hidden=True), tmp_path, ".notes.md") == INDEX
    assert not _m(tmp_path, include_hidden=True).prune_dir(".obsidian", str(tmp_path / ".obsidian"))


def test_icloud_stub_is_not_treated_as_hidden(tmp_path):
    assert _cls(_m(tmp_path), tmp_path, "Docs/.Report.pdf.icloud") == INDEX


def test_home_only_dirs_are_pruned_only_at_the_top_of_a_home_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ig.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    home = _m(tmp_path)
    assert home.prune_dir("AppData", str(tmp_path / "AppData"))
    assert home.prune_dir("Library", str(tmp_path / "Library"))
    assert not home.prune_dir("Books/Library", str(tmp_path / "Books" / "Library"))
    other = _m(tmp_path / "Documents")
    assert not other.prune_dir("Library", str(tmp_path / "Documents" / "Library"))


def test_negation_reincludes_a_default(tmp_path):
    (tmp_path / ".cremindignore").write_text("!build/\n", encoding="utf-8")
    m = _m(tmp_path)
    assert not m.prune_dir("build", str(tmp_path / "build"))
    assert _cls(m, tmp_path, "build/plan.docx") == INDEX


def test_bundles_are_pruned_but_listed_as_metadata_only(tmp_path):
    m = _m(tmp_path)
    assert m.prune_dir("Apps/Tool.app", str(tmp_path / "Apps" / "Tool.app"))
    assert _cls(m, tmp_path, "Apps/Tool.app", is_dir=True) == METADATA_ONLY
    assert _cls(m, tmp_path, "Apps/Tool.app/Contents/Info.plist") == SKIP
    assert m.bundle_root("Apps/Tool.app/Contents/Info.plist") == "Apps/Tool.app"
    assert m.bundle_root("Apps/readme.txt") is None
    assert m.is_bundle("X.framework") and m.is_bundle("Y.BUNDLE") and not m.is_bundle(".app")


# ── layer 3: .cremindignore and settings excludes ──────────────────────────


def test_cremindignore_applies_to_its_own_subtree(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / ".cremindignore").write_text("*.log\n/drafts/\n", encoding="utf-8")
    m = _m(tmp_path)
    assert _cls(m, tmp_path, "a/x.log") == SKIP
    assert _cls(m, tmp_path, "a/deep/x.log") == SKIP
    assert _cls(m, tmp_path, "b/x.log") == INDEX
    assert m.prune_dir("a/drafts", str(tmp_path / "a" / "drafts"))
    assert not m.prune_dir("a/deep/drafts", str(tmp_path / "a" / "deep" / "drafts"))  # anchored
    assert not m.prune_dir("drafts", str(tmp_path / "drafts"))


def test_deeper_cremindignore_overrides_a_shallower_one(tmp_path):
    (tmp_path / ".cremindignore").write_text("*.csv\n", encoding="utf-8")
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / ".cremindignore").write_text("!*.csv\n", encoding="utf-8")
    m = _m(tmp_path)
    assert _cls(m, tmp_path, "data.csv") == SKIP
    assert _cls(m, tmp_path, "keep/data.csv") == INDEX


def test_only_markdown_idiom_works_per_directory(tmp_path):
    # "*" then "!*/" then "!*.md": keep every directory, only Markdown files.
    (tmp_path / ".cremindignore").write_text("*\n!*/\n!*.md\n", encoding="utf-8")
    m = _m(tmp_path)
    assert not m.prune_dir("a", str(tmp_path / "a"))
    assert not m.prune_dir("a/b", str(tmp_path / "a" / "b"))
    assert _cls(m, tmp_path, "a/b/notes.md") == INDEX
    assert _cls(m, tmp_path, "a/b/notes.txt") == SKIP


def test_file_under_an_excluded_dir_cannot_be_reincluded(tmp_path):
    (tmp_path / ".cremindignore").write_text("logs/\n!logs/keep.txt\n", encoding="utf-8")
    m = _m(tmp_path)
    assert m.prune_dir("logs", str(tmp_path / "logs"))
    assert _cls(m, tmp_path, "logs/keep.txt") == SKIP  # as git: the parent is excluded


def test_cremindignore_file_itself_is_skipped(tmp_path):
    assert _cls(_m(tmp_path, include_hidden=True), tmp_path, "a/.cremindignore") == SKIP


def test_invalidate_rereads_rules(tmp_path):
    m = _m(tmp_path)
    assert _cls(m, tmp_path, "x.log") == INDEX
    (tmp_path / ".cremindignore").write_text("*.log\n", encoding="utf-8")
    assert _cls(m, tmp_path, "x.log") == INDEX  # cached
    m.invalidate()
    assert _cls(m, tmp_path, "x.log") == SKIP


def test_settings_excludes_by_type_and_mode(tmp_path):
    m = _m(tmp_path, excludes=[
        {"pattern": "*.mp4", "type": "glob", "mode": "skip"},
        {"pattern": "Archive/", "type": "glob", "mode": "metadata_only"},
        {"pattern": "Old*", "type": "dir", "mode": "skip"},
        {"pattern": "raw", "type": "dir", "mode": "metadata_only"},
        {"pattern": "psd", "type": "ext", "mode": "metadata_only"},
        {"pattern": "iso", "type": "ext", "mode": "skip"},
        {"pattern": "!*.mp4", "type": "glob", "mode": "skip"},  # negations are ignored here
    ])
    assert _cls(m, tmp_path, "films/a.mp4") == SKIP
    assert _cls(m, tmp_path, "Archive/2019/report.docx") == METADATA_ONLY
    assert m.prune_dir("x/Old stuff", str(tmp_path / "x" / "Old stuff"))
    assert not m.prune_dir("x/raw", str(tmp_path / "x" / "raw"))
    assert _cls(m, tmp_path, "x/raw/p.cr2") == METADATA_ONLY
    assert _cls(m, tmp_path, "art/cover.PSD") == METADATA_ONLY
    assert _cls(m, tmp_path, "disk.iso") == SKIP
    assert _cls(m, tmp_path, "notes.txt") == INDEX


def test_settings_excludes_cannot_reinclude(tmp_path):
    m = _m(tmp_path, excludes=[{"pattern": "!node_modules/", "type": "glob", "mode": "skip"}])
    assert m.prune_dir("node_modules", str(tmp_path / "node_modules"))


def test_classify_accepts_backslashes_and_nfd(tmp_path):
    (tmp_path / ".cremindignore").write_text("Tiếng/\n", encoding="utf-8")  # NFC
    m = _m(tmp_path)
    nfd = "Tiếng"  # the same name, decomposed (macOS style)
    assert m.prune_dir(nfd, str(tmp_path / nfd))
    assert _cls(m, tmp_path, "a\\b.txt") == INDEX


# ── the gitignore translation ──────────────────────────────────────────────


@pytest.mark.parametrize("pattern,path,is_dir,expected", [
    ("foo", "foo", False, True),
    ("foo", "a/b/foo", False, True),
    ("foo", "foo/bar", False, False),       # an ancestor match is not a self match
    ("foo/", "foo", False, False),          # directory-only
    ("foo/", "a/foo/", True, True),
    ("/foo", "a/foo", False, False),        # anchored
    ("/foo", "foo", False, True),
    ("a/*.md", "a/x.md", False, True),
    ("a/*.md", "a/b/x.md", False, False),
    ("a/**/b", "a/b", False, True),
    ("a/**/b", "a/x/y/b", False, True),
    ("**/x", "p/q/x", False, True),
    ("x/**", "x/a/b", False, True),
    ("x/**", "x", True, False),
    ("*", "anything/at/all", False, True),
    ("*/", "a/x.txt", False, False),
    ("*/", "a/b/", True, True),
    ("?.txt", "a.txt", False, True),
    ("?.txt", "ab.txt", False, False),
    ("[!a]x", "bx", False, True),
    ("[!a]x", "ax", False, False),
    ("[ab]x", "bx", False, True),
    ("\\!bang", "!bang", False, True),
    ("\\#hash", "#hash", False, True),
    ("trail\\ ", "trail ", False, True),
    ("trailing   ", "trailing", False, True),
])
def test_gitignore_translation(pattern, path, is_dir, expected):
    (rule,) = compile_rules([pattern])
    assert rule.test(path + ("/" if is_dir and not path.endswith("/") else ""), is_dir) is expected


def test_comments_and_blanks_compile_to_nothing():
    assert compile_rules(["", "   ", "# a comment", "\r"]) == ()


@pytest.mark.parametrize("pattern", ["*.log", "/build", "docs/*.md", "a/**/b.txt", "**/x.cfg", "sub/dir/f.txt", "[ab]*.txt"])
@pytest.mark.parametrize("path", ["x.log", "a/x.log", "build", "docs/r.md", "a/q/r/b.txt", "p/x.cfg", "sub/dir/f.txt", "b1.txt", "a/b2.txt"])
def test_file_verdicts_agree_with_pathspec(pattern, path):
    """For file paths whose ancestors no rule matches, our translation and
    pathspec's must agree — it is the same gitignore syntax."""
    pathspec = pytest.importorskip("pathspec")
    try:
        factory = pathspec.lookup_pattern("gitignore")  # pathspec 1.x
    except Exception:  # noqa: BLE001
        factory = pathspec.lookup_pattern("gitwildmatch")  # 0.12
    spec = pathspec.PathSpec.from_lines(factory, [pattern])
    parts = path.split("/")
    if any(spec.match_file("/".join(parts[:i]) + "/") for i in range(1, len(parts))):
        pytest.skip("an ancestor matches; the two models differ by design there")
    (rule,) = compile_rules([pattern])
    assert rule.test(path, False) is spec.match_file(path)
