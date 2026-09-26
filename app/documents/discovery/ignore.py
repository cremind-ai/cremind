"""What under a documents root is never indexed, indexed by name only, or read.

Every file gets one of three dispositions:

- ``skip`` — not in the index at all (junk, tooling trees, excluded paths);
- ``metadata_only`` — name, path, size and dates are indexed, the content is
  never opened (secrets, bundles, anything the user marked so);
- ``index`` — the content is extracted and embedded.

The rules come in three layers, and the order is the security property:

1. **Non-overridable.** Credential directories (``.ssh``, ``.aws``, the
   coding-CLI login stores, Cremind's own system directory when it sits under
   the root, other profiles' working directories, and any other locked
   exclude) are pruned outright. Secret-looking
   files (``.env``, ``*.pem``, ``id_rsa``, ``credentials.json`` …) are capped
   at ``metadata_only``. Nothing in layers 2 and 3 can lift either: a
   ``!*.pem`` in a ``.cremindignore`` does not make a private key readable by
   the agent, because the agent can read whatever the index holds.
2. **Defaults.** Version-control and build trees, caches, trash, OS junk,
   editor swap files and hidden entries. A ``.cremindignore`` negation *can*
   re-include these — a folder of documents called ``build`` is plausible,
   a readable ``.ssh`` is not.
3. **The user's rules.** ``.cremindignore`` files at any depth (gitignore
   syntax, applied to their own subtree, deeper files winning, last match
   winning within a file) and the exclude list from the profile's settings.
   Settings excludes only ever exclude; they cannot re-include.

Decisions are per directory and inherited: once a directory is pruned nothing
under it is looked at, which is also what makes git's "you cannot re-include a
file whose parent directory is excluded" hold here. Directory decisions are
cached; :meth:`IgnoreMatcher.invalidate` drops the cache when a
``.cremindignore`` changes.

Case: on Windows and macOS (case-insensitive filesystems) names and patterns
compare case-insensitively; on Linux exactly. Secret and junk file names
always compare case-insensitively — ``ID_RSA`` is still a key.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat as stat_mod
import sys
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.documents.discovery.hashing import fs_path, is_icloud_stub, is_placeholder
from app.utils.credential_paths import CREDENTIAL_DIR_NAMES
from app.utils.logger import logger

SKIP = "skip"
METADATA_ONLY = "metadata_only"
INDEX = "index"

IGNORE_FILE_NAME = ".cremindignore"
# A .cremindignore is a handful of lines; anything bigger is not one we wrote
# and reading all of it on every directory would be a denial of service.
_IGNORE_FILE_MAX_BYTES = 64 * 1024

CASE_INSENSITIVE = os.name == "nt" or sys.platform == "darwin"


def fold_case(s: str) -> str:
    """``s`` as the filesystem compares it: lowercased on Windows/macOS."""
    return s.lower() if CASE_INSENSITIVE else s


def norm_rel(rel_path: str) -> str:
    """A relative path in the one spelling discovery uses: POSIX separators,
    no leading/trailing slash, NFC (macOS hands back decomposed names)."""
    rel = (rel_path or "").replace("\\", "/").strip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    return unicodedata.normalize("NFC", rel)


def _fnmatch_regex(patterns: Iterable[str]) -> re.Pattern[str]:
    return re.compile("|".join(fnmatch.translate(p) for p in patterns))


# ── Layer 1: never read ────────────────────────────────────────────────────

# Home-directory credential stores, plus the coding-CLI login stores shared
# with the file API. ``.azure`` and ``.password-store`` are the same kind of
# thing as ``.aws`` (a cloud login, a pass(1) vault) and cost nothing to add.
_CREDENTIAL_DIRS = frozenset(
    n.lower() for n in {".ssh", ".aws", ".kube", ".gnupg", ".docker", ".azure", ".password-store"}
    | set(CREDENTIAL_DIR_NAMES)
)

# Matched against the lowercased file name. ``*.key`` also catches Apple
# Keynote decks: they are indexed by name only, the price of never reading a
# TLS private key.
_SECRET_FILE_RE = _fnmatch_regex((
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "*.ppk", "*.kdbx", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    ".netrc", ".pgpass", ".npmrc", ".pypirc", ".git-credentials",
    "credentials*.json", "credentials.toml", "token.json", ".google_token.json",
    "wallet.dat",
))

# ── Layer 2: defaults ──────────────────────────────────────────────────────

_DEFAULT_PRUNE_DIRS = frozenset(fold_case(n) for n in (
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode", "dist", "build",
    "target", ".next", ".cache", ".gradle", "$RECYCLE.BIN", "System Volume Information",
))
# ``.Trash``, ``.Trashes`` (macOS volumes), ``.Trash-1000`` (freedesktop).
_TRASH_DIR_RE = _fnmatch_regex((".trash*",))
# Only directly under a root that *is* the home directory: the OS keeps
# application state there, not documents. A folder called "Library" deeper
# down is somebody's library.
_HOME_ONLY_PRUNE_DIRS = frozenset(fold_case(n) for n in ("AppData", "Library"))

# Matched against the lowercased file name.
_JUNK_FILE_RE = _fnmatch_regex((
    ".ds_store", "thumbs.db", "desktop.ini", "~$*", ".~lock.*#", "*.tmp", "*.temp",
    "*.swp", "*.swo", "*.part", "*.crdownload", "*.pyc", "*.o", "*.class",
))

# macOS directory bundles: an .app is thousands of files that mean one thing.
_BUNDLE_SUFFIXES = (".app", ".bundle", ".framework")


# ── Gitignore-syntax rules ─────────────────────────────────────────────────
#
# Our own translation of gitignore(5) rather than pathspec's. pathspec's regexes
# answer "does this pattern match the path *or one of its ancestors*" — right
# for testing a full path in isolation, wrong for a walk that decides each
# directory level on its own and prunes: ``*`` + ``!*/`` + ``!*.md`` ("only
# Markdown, at any depth") re-includes every file through its parent's match,
# and even ``GitIgnoreSpec`` reports the directory itself as ignored. Its regex
# shapes also differ between 0.12 and 1.x. Git's wildmatch rules are short, so
# they are translated here into regexes that match the path *itself* only.


@dataclass(frozen=True)
class _Rule:
    # The rule matches a path (relative to the .cremindignore's directory, no
    # trailing slash) when this matches it in full.
    regex: re.Pattern[str]
    # True: exclude. False: a "!" negation, re-include.
    ignore: bool
    dir_only: bool

    def test(self, path: str, is_dir: bool) -> bool:
        """``path`` may carry a trailing "/" for a directory; it is ignored."""
        if self.dir_only and not is_dir:
            return False
        return self.regex.match(path.rstrip("/")) is not None


def _translate_segment(seg: str) -> str:
    """One path segment of a wildmatch pattern -> regex (never crosses "/")."""
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(seg[i + 1]))
            i += 2
        elif c == "*":
            while i < n and seg[i] == "*":
                i += 1  # "**" inside a segment is an ordinary "*"
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and seg[j] in "!^":
                j += 1
            if j < n and seg[j] == "]":
                j += 1  # a "]" first in the class is literal
            while j < n and seg[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape("["))  # unterminated: literal "["
                i += 1
                continue
            # Escape what Python's class syntax treats specially but a glob
            # class does not (a backslash, and "[", "&", "~", "|", which also
            # raise "possible set operation" warnings).
            body = "".join("\\" + ch if ch in "\\[&~|" else ch for ch in seg[i + 1:j])
            if body[:1] in ("!", "^"):
                body = "^/" + body[1:]  # a negated class still never matches "/"
            out.append(f"[{body}]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _parse_line(raw: str) -> _Rule | None:
    line = raw.rstrip("\r\n")
    # Trailing spaces are dropped unless escaped ("foo\ ").
    while line.endswith(" ") and not line.endswith("\\ "):
        line = line[:-1]
    if not line or line.startswith("#"):
        return None
    ignore = True
    if line.startswith("!"):
        ignore, line = False, line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    # A slash at the start or in the middle anchors the pattern to the
    # .cremindignore's directory; otherwise it matches a name at any depth.
    anchored = "/" in line
    line = line.lstrip("/")
    segs: list[str] = []
    for seg in line.split("/"):
        if seg and not (seg == "**" and segs and segs[-1] == "**"):
            segs.append(seg)
    if not segs:
        return None

    if segs == ["**"]:
        body = ".+"
    else:
        pieces: list[str] = []
        after_globstar = False
        last = len(segs) - 1
        for i, seg in enumerate(segs):
            if seg == "**":
                if i == 0:
                    pieces.append("(?:.*/)?")   # "**/x": x in any directory
                elif i == last:
                    pieces.append("/.+")        # "x/**": everything inside x
                else:
                    pieces.append("/(?:.*/)?")  # "a/**/b": zero or more dirs
                after_globstar = True
                continue
            if i > 0 and not after_globstar:
                pieces.append("/")
            pieces.append(_translate_segment(seg))
            after_globstar = False
        body = "".join(pieces)
        if not anchored:
            body = "(?:.*/)?" + body
    flags = re.IGNORECASE if CASE_INSENSITIVE else 0
    try:
        # ``\Z``, not ``$``: "$" also matches before a trailing newline, and
        # a file name may legally end in one.
        regex = re.compile(f"^{body}\\Z", flags)
    except re.error:
        logger.debug(f"[documents] ignoring malformed ignore pattern {raw!r}")
        return None
    return _Rule(regex, ignore, dir_only)


def compile_rules(lines: Iterable[str]) -> tuple[_Rule, ...]:
    """Compile gitignore-syntax lines into rules, in order (last match wins)."""
    rules: list[_Rule] = []
    for raw in lines:
        rule = _parse_line(raw)
        if rule is not None:
            rules.append(rule)
    return tuple(rules)


# ── Per-directory state ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _DirState:
    pruned: bool = False   # do not descend
    skip: bool = False     # excluded entirely (pruned, and not a bundle)
    meta: bool = False     # everything below is metadata-only (or this is a bundle)
    bundle: bool = False
    # The .cremindignore files in force here, root first: (depth of the
    # directory holding the file, its rules). Depth rather than the directory's
    # spelling, so a path spelled in a different case still strips correctly.
    chain: tuple[tuple[int, tuple[_Rule, ...]], ...] = ()


_SKIPPED = _DirState(pruned=True, skip=True)

# Directory decisions cached before the cache is thrown away and rebuilt. A
# walk of 500k entries touches far fewer directories; this only bounds a
# long-lived watcher's matcher.
_CACHE_MAX = 200_000


def _norm_abs(p: str) -> str:
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _inside(child: str, parent: str) -> bool:
    if child == parent:
        return True
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


class IgnoreMatcher:
    """Decides, per path under one root, ``skip`` / ``metadata_only`` / ``index``.

    Thread-safe: the only mutable state is the directory cache, and a
    computation always writes into the cache object it started from, so an
    :meth:`invalidate` racing a lookup can at worst waste the work.
    """

    def __init__(
        self,
        root: str,
        *,
        excludes: list[dict] | None = None,
        locked_excludes: list[str] | None = None,
        include_hidden: bool = False,
        system_dir: str | None = None,
        root_sanctioned: bool = False,
    ):
        """``root_sanctioned``: ``validate_root`` approved this root although
        it lies inside ``system_dir`` — the profile's own default workspace
        (``<SYS>/workspaces/<profile>``). Only then does the system folder
        not lock the whole root; it still locks everything else, and every
        other locked exclude applies as always. Pass it only for a root that
        check approved."""
        self.root = os.path.abspath(root)
        self.include_hidden = bool(include_hidden)
        root_n = _norm_abs(self.root)
        sys_n = _norm_abs(system_dir) if system_dir else None

        locked = [p for p in (locked_excludes or []) if p]
        if system_dir:
            locked.append(system_dir)
        self._locked_abs: set[str] = set()
        self._locked_rel: set[str] = set()
        self._root_locked = False
        for p in locked:
            n = _norm_abs(p)
            self._locked_abs.add(n)
            if _inside(root_n, n):
                if root_sanctioned and n == sys_n and root_n != n:
                    continue
                # The whole root is inside a locked directory: index nothing.
                # validate_root refuses such a root; this is the backstop.
                self._root_locked = True
            elif _inside(n, root_n):
                rel = os.path.relpath(n, root_n).replace(os.sep, "/")
                self._locked_rel.add(fold_case(norm_rel(rel)))

        home = os.path.expanduser("~")
        self._is_home = bool(home) and home != "~" and _norm_abs(home) == root_n

        self._x_glob: list[tuple[_Rule, str]] = []
        self._x_dir: list[tuple[str, str]] = []
        self._x_ext: list[tuple[str, str]] = []
        for rule in excludes or []:
            pattern = str(rule.get("pattern") or "").strip()
            mode = SKIP if rule.get("mode") != METADATA_ONLY else METADATA_ONLY
            typ = rule.get("type") or "glob"
            if not pattern:
                continue
            if typ == "ext":
                self._x_ext.append(("." + pattern.lower().lstrip("*").lstrip("."), mode))
            elif typ == "dir":
                self._x_dir.append((fold_case(norm_rel(pattern)), mode))
            else:
                if pattern.startswith("!"):
                    # Settings excludes only exclude; a negation here would be
                    # a way to lift a default the user never saw.
                    continue
                for compiled in compile_rules([pattern]):
                    self._x_glob.append((compiled, mode))
        self._has_user_rules = bool(self._x_glob or self._x_dir or self._x_ext)

        self._cache: dict[str, _DirState] = {}

    # ── public API ─────────────────────────────────────────────────────────

    def invalidate(self) -> None:
        """Forget every cached directory decision (a .cremindignore changed)."""
        self._cache = {}

    @staticmethod
    def is_bundle(name: str) -> bool:
        low = name.lower()
        return any(low.endswith(s) and len(low) > len(s) for s in _BUNDLE_SUFFIXES)

    def prune_dir(self, rel_dir: str, abs_dir: str | None = None) -> bool:
        """True when the walker must not descend into this directory: it is
        excluded, or it is a bundle (listed as one metadata-only entry)."""
        return self._state(norm_rel(rel_dir), os.path.normpath(abs_dir) if abs_dir else None).pruned

    def classify(self, rel_path: str, abs_path: str | None = None, *, is_dir: bool = False) -> str:
        """``skip`` | ``metadata_only`` | ``index`` for a path under the root.

        Ancestors are checked too, so this is safe for a path that did not
        come from a walk (a watcher event, a file the agent just wrote). For a
        directory (``is_dir=True``): ``skip`` when excluded, ``metadata_only``
        for a bundle or a directory under a metadata-only rule.
        """
        rel = norm_rel(rel_path)
        if not rel:
            return SKIP if self._root_locked else INDEX
        # A trailing separator would make dirname() below return the path itself.
        abs_path = os.path.normpath(abs_path) if abs_path else None
        if is_dir:
            st = self._state(rel, abs_path)
            if st.skip:
                return SKIP
            return METADATA_ONLY if st.meta else INDEX
        parent_rel, _, name = rel.rpartition("/")
        parent_abs = os.path.dirname(abs_path) if abs_path else None
        ps = self._state(parent_rel, parent_abs)
        if ps.pruned:
            return SKIP
        return self._file_disposition(rel, name, ps)

    def bundle_root(self, rel_path: str) -> str | None:
        """The bundle a path sits *inside* (``Tool.app`` for
        ``Tool.app/Contents/Info.plist``), or ``None``. A change inside a bundle
        is a change of the bundle's single index entry."""
        parts = norm_rel(rel_path).split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            st = self._state(prefix, None)
            if st.bundle:
                return prefix
            if st.pruned:
                return None
        return None

    # ── internals ──────────────────────────────────────────────────────────

    def _abs_of(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def _state(self, rel: str, abs_dir: str | None) -> _DirState:
        cache = self._cache
        if len(cache) > _CACHE_MAX:
            cache = self._cache = {}
        hit = cache.get(fold_case(rel))
        if hit is not None:
            return hit
        # Collect the uncached ancestors, then decide them top-down: a child's
        # decision needs its parent's (pruning, inherited rules).
        todo: list[tuple[str, str | None]] = []
        r, a = rel, abs_dir
        state: _DirState | None = None
        while True:
            state = cache.get(fold_case(r))
            if state is not None:
                break
            todo.append((r, a))
            if not r:
                break
            r, _, _ = r.rpartition("/")
            a = os.path.dirname(a) if a else None
        for r, a in reversed(todo):
            if not r:
                state = self._root_state()
            else:
                assert state is not None
                state = self._compute_dir(r, a or self._abs_of(r), state)
            cache[fold_case(r)] = state
        assert state is not None
        return state

    def _root_state(self) -> _DirState:
        if self._root_locked:
            return _SKIPPED
        rules = self._load_ignore_file(self.root)
        return _DirState(chain=((0, rules),) if rules else ())

    def _compute_dir(self, rel: str, abs_dir: str, parent: _DirState) -> _DirState:
        if parent.pruned:
            return _SKIPPED
        name = rel.rpartition("/")[2]
        # Layer 1 — nothing below can undo these.
        if name.lower() in _CREDENTIAL_DIRS or fold_case(rel) in self._locked_rel:
            return _SKIPPED
        if self._locked_abs and _norm_abs(abs_dir) in self._locked_abs:
            return _SKIPPED
        # Layer 2, then the .cremindignore chain, which may flip it either way.
        key = fold_case(name)
        ignored = (
            key in _DEFAULT_PRUNE_DIRS
            or _TRASH_DIR_RE.match(name.lower()) is not None
            or (self._is_home and "/" not in rel and key in _HOME_ONLY_PRUNE_DIRS)
            or (name.startswith(".") and not self.include_hidden)
        )
        if self._apply_chain(parent.chain, rel, True, ignored):
            return _SKIPPED
        # Layer 3, the settings excludes.
        mode = self._user_mode(rel, name, True)
        if mode == SKIP:
            return _SKIPPED
        meta = parent.meta or mode == METADATA_ONLY
        if self.is_bundle(name):
            return _DirState(pruned=True, skip=False, meta=True, bundle=True)
        chain = parent.chain
        rules = self._load_ignore_file(abs_dir)
        if rules:
            chain = chain + ((rel.count("/") + 1, rules),)
        return _DirState(meta=meta, chain=chain)

    def _file_disposition(self, rel: str, name: str, parent: _DirState) -> str:
        low = name.lower()
        if low == IGNORE_FILE_NAME:
            return SKIP  # our own control file
        secret = _SECRET_FILE_RE.match(low) is not None
        ignored = _JUNK_FILE_RE.match(low) is not None or (
            # An iCloud stub is hidden by its spelling, but it stands for a
            # visible file; the walker lists it as a placeholder.
            name.startswith(".") and not self.include_hidden and not is_icloud_stub(name)
        )
        if self._apply_chain(parent.chain, rel, False, ignored):
            return SKIP
        mode = self._user_mode(rel, name, False)
        if mode == SKIP:
            return SKIP
        if mode == METADATA_ONLY or parent.meta or secret:
            return METADATA_ONLY
        return INDEX

    @staticmethod
    def _apply_chain(chain: tuple[tuple[int, tuple[_Rule, ...]], ...], rel: str, is_dir: bool, ignored: bool) -> bool:
        if not chain:
            return ignored
        parts = rel.split("/")
        for depth, rules in chain:
            sub = "/".join(parts[depth:])
            if is_dir:
                sub += "/"
            for rule in rules:
                # Last match wins, so a rule that agrees with the current
                # verdict cannot change it and need not be tested.
                if rule.ignore != ignored and rule.test(sub, is_dir):
                    ignored = rule.ignore
        return ignored

    def _user_mode(self, rel: str, name: str, is_dir: bool) -> str | None:
        if not self._has_user_rules:
            return None
        modes: set[str] = set()
        probe = rel + "/" if is_dir else rel
        for rule, mode in self._x_glob:
            if rule.test(probe, is_dir):
                modes.add(mode)
        if is_dir:
            key, rel_key = fold_case(name), fold_case(rel)
            for pat, mode in self._x_dir:
                if "/" in pat:
                    hit = rel_key == pat or rel_key.endswith("/" + pat)
                else:
                    hit = fnmatch.fnmatchcase(key, pat)
                if hit:
                    modes.add(mode)
        else:
            low = name.lower()
            for ext, mode in self._x_ext:
                if low.endswith(ext) and len(low) > len(ext):
                    modes.add(mode)
        if SKIP in modes:
            return SKIP
        return METADATA_ONLY if modes else None

    def _load_ignore_file(self, abs_dir: str) -> tuple[_Rule, ...]:
        path = os.path.join(abs_dir, IGNORE_FILE_NAME)
        try:
            st = os.stat(fs_path(path))
        except OSError:
            return ()
        # A FIFO would block the open; a cloud placeholder would be downloaded.
        if not stat_mod.S_ISREG(st.st_mode) or is_placeholder(IGNORE_FILE_NAME, st):
            return ()
        try:
            with open(fs_path(path), "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(_IGNORE_FILE_MAX_BYTES)
        except OSError as exc:
            logger.debug(f"[documents] cannot read {path}: {exc}")
            return ()
        return compile_rules(text.splitlines())


__all__ = [
    "CASE_INSENSITIVE",
    "IGNORE_FILE_NAME",
    "INDEX",
    "IgnoreMatcher",
    "METADATA_ONLY",
    "SKIP",
    "compile_rules",
    "fold_case",
    "norm_rel",
]
