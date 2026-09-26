"""Names of directories that hold credentials and must never be served or read.

Shared by the file API (:mod:`app.api.files`, which refuses to list or serve
them) and Documentation search (:mod:`app.documents`, which never indexes them).
It lives here rather than in the API module so the indexer does not have to
import the web layer to learn the rule.

``coding-cli`` holds long-lived OAuth refresh tokens for a user's Claude and
ChatGPT accounts, ``codex-home`` the ``auth.json`` written for an API-key
credential, and ``cli-wizards`` plaintext keys until a wizard finishes. They
are kept as a name set because there is one store per profile, created on
demand, and the same names appear both per profile and at the shared root —
there is no list of live paths to enumerate, so the name is the rule.

The same module answers two location questions about document trees, because
the file API and the agent's file tool must agree on them exactly:

- :func:`is_documents_index_path` — Documentation search's index store, which
  nobody reaches through generic file access;
- :func:`authored_docs_owner` — whose uuid-keyed Cremind-manual directory a
  path is in, so only that profile reaches it.

:class:`DocumentTreeGuard` packages both for per-entry filters (a directory
listing, a recursive walk, a watch stream).

Case. ``realpath`` resolves links, not letter case: on a case-insensitive
filesystem that is not Windows — macOS's default APFS, a Docker Desktop bind
mount seen from a Linux container — ``<SYS>/STORAGE/documents`` IS the index
store, yet it is a different string, and ``normcase`` is the identity off
Windows. So a path whose letters match a protected root but whose case does
not is asked about on disk (:func:`_same_file`): the same directory there
means the same rule applies. Only such near-misses pay for the ``stat``; an
exact match and an unrelated path never touch the filesystem.

Pure path arithmetic only (no DB, no app imports): resolving a caller's uuid
is the caller's job.
"""

import os
from typing import Callable, Optional

CREDENTIAL_DIR_NAMES = frozenset({"coding-cli", "codex-home", "cli-wizards"})

# Relative to the system dir. ``storage/userdocs`` is where the indexes lived
# before the rename; an install that has not been relocated yet (or one whose
# relocation hit a conflict and kept the old copy) still has index files
# there, so it is protected exactly like the current root.
_INDEX_ROOTS: tuple[tuple[str, ...], ...] = (
    ("storage", "documents"),
    ("storage", "userdocs"),
)

# Mirrors app.cremind_documents.paths.PROFILES_PARTS (kept literal: this
# module stays import-free so the indexer and the offline CLI can use it).
_AUTHORED_DOCS_PARTS: tuple[str, ...] = ("storage", "cremind_documents", "profiles")

# Seams for the case rule (tests simulate a POSIX ``normcase`` and a
# case-insensitive or case-sensitive filesystem through these).
_normcase = os.path.normcase


def _same_file(a: str, b: str) -> bool:
    """Do ``a`` and ``b`` name the same directory on disk? False when either
    cannot be read — nothing exists there to protect (the exact spelling of a
    protected root is still matched without asking the disk)."""
    try:
        return os.path.samestat(os.stat(a), os.stat(b))
    except (OSError, ValueError):
        return False


def _fold(s: str) -> str:
    return _normcase(s).casefold()


def _split(path: str) -> list[str]:
    # ``os.sep`` alone: every path here has been through ``realpath``, which
    # normalises the separator (on POSIX a backslash is part of a name).
    stripped = path.rstrip(os.sep)
    return (stripped or path).split(os.sep)


def parts_below(resolved: str, root: str) -> Optional[list[str]]:
    """The components of ``resolved`` below ``root`` — ``[]`` for ``root``
    itself — or None when ``resolved`` is neither ``root`` nor inside it.

    Both must already be resolved (``realpath``). Compared component-wise:
    exactly (after ``normcase``) first; a spelling that differs only in case
    is inside ``root`` when the filesystem says the two are one directory
    (see the module docstring)."""
    rp, tp = _split(root), _split(resolved)
    n = len(rp)
    if len(tp) < n:
        return None
    head = tp[:n]
    if all(_normcase(a) == _normcase(b) for a, b in zip(head, rp)):
        return tp[n:]
    if not all(_fold(a) == _fold(b) for a, b in zip(head, rp)):
        return None
    return tp[n:] if _same_file(os.sep.join(head), root) else None


def within(resolved: str, root: str) -> bool:
    """Is the resolved path ``root`` or inside it (case rule included)?"""
    return parts_below(resolved, root) is not None


def documents_index_root(system_dir: str) -> str:
    """Where Documentation search keeps every profile's index."""
    return os.path.join(os.path.realpath(system_dir), *_INDEX_ROOTS[0])


def documents_index_roots(system_dir: str) -> tuple[str, ...]:
    """The index store and its pre-rename location, resolved."""
    base = os.path.realpath(system_dir)
    return tuple(os.path.join(base, *parts) for parts in _INDEX_ROOTS)


def is_documents_index_path(target: str, system_dir: str) -> bool:
    """Is ``target`` Documentation search's index store, or inside it?

    Every profile's index lives there (one directory per profile uid) and an
    index holds the text of that profile's files, so generic file access —
    the file API, the agent's file tool — refuses it for every profile,
    admin included. Its content reaches clients only through the
    profile-scoped ``/api/documentation-search`` routes. Both the current
    root and the pre-rename ``storage/userdocs`` count.
    """
    real = os.path.realpath(target)
    return any(within(real, root) for root in documents_index_roots(system_dir))


def holds_documents_index(target: str, system_dir: str) -> bool:
    """Does the index store (either root) live strictly below ``target``?

    For moves and deletes: carrying or removing a parent carries or removes
    every profile's index with it."""
    real = os.path.realpath(target)
    return any(bool(parts_below(root, real)) for root in documents_index_roots(system_dir))


def authored_docs_root(system_dir: str) -> str:
    """``<SYS>/storage/cremind_documents/profiles``, resolved."""
    return os.path.join(os.path.realpath(system_dir), *_AUTHORED_DOCS_PARTS)


def authored_docs_owner(target: str, system_dir: str) -> str | None:
    """The uuid whose authored-manual directory ``target`` is (or is inside).

    ``None`` when ``target`` is not below ``storage/cremind_documents/profiles``
    — including the ``profiles`` directory itself, which belongs to nobody (a
    listing or walk of it is filtered per entry instead, see
    :class:`DocumentTreeGuard`). The uuid comes back ``normcase``-d, so
    compare it with :func:`same_uid`.
    """
    below = parts_below(os.path.realpath(target), authored_docs_root(system_dir))
    if not below or not below[0]:
        return None
    return _normcase(below[0])


def same_uid(a: str | None, b: str | None) -> bool:
    """Two profile uuids, compared without regard to case: a uuid is hex, and
    on a case-insensitive filesystem ``profiles/B2B2…`` IS ``profiles/b2b2…``."""
    return bool(a) and bool(b) and _fold(str(a)) == _fold(str(b))


def holds_authored_docs(target: str, system_dir: str) -> bool:
    """Do the per-profile manual directories live at or below ``target``?

    True for ``storage``, ``storage/cremind_documents`` and ``.../profiles``
    itself: moving or deleting any of them moves or deletes every profile's
    authored pages at once."""
    real = os.path.realpath(target)
    return within(authored_docs_root(system_dir), real)


class DocumentTreeGuard:
    """What a per-entry filter hides, for one caller: the index store (both
    roots, for everyone) and every uuid directory under the manual root but
    the caller's own. The ``profiles`` directory itself is not hidden — it
    belongs to nobody, and filtering its entries is what keeps a listing or a
    walk of it to the caller's own directory.

    Built once per request or walk. The system dir is resolved once, and the
    caller's uuid is asked of ``own_uid`` at most once, and only when an entry
    under the manual root actually needs it. Callers pass *resolved* paths
    (built from a root that was resolved before the route or tool accepted
    it), so nothing here resolves per entry.
    """

    def __init__(self, system_dir: str, own_uid: Optional[Callable[[], Optional[str]]] = None) -> None:
        base = os.path.realpath(system_dir)
        self.index_roots = documents_index_roots(base)
        self.manual_root = authored_docs_root(base)
        self._own_uid = own_uid
        self._own: list[Optional[str]] = []
        self._screen = tuple(_fold(r) for r in (*self.index_roots, self.manual_root))

    def own_uid(self) -> Optional[str]:
        if not self._own:
            try:
                self._own.append(self._own_uid() if self._own_uid is not None else None)
            except Exception:  # noqa: BLE001 — an unresolvable caller owns nothing
                self._own.append(None)
        return self._own[0]

    def near(self, resolved_dir: str) -> bool:
        """Could any entry of ``resolved_dir`` be hidden? Only in an ancestor
        of a protected root or inside one — a string screen (case-folded, so
        it errs towards yes) that lets a walk skip the per-entry test in every
        other directory."""
        d = _fold(resolved_dir)
        prefix = d if d.endswith(os.sep) else d + os.sep
        return any(r == d or r.startswith(prefix) or d.startswith(r + os.sep) for r in self._screen)

    def hides(self, resolved: str) -> bool:
        # One case-folded string test first (it errs towards "maybe"): an
        # entry nowhere near a protected tree — nearly every entry of a
        # listing or event of a watch — costs no component split and no stat.
        f = _fold(resolved)
        if not any(f == r or f.startswith(r + os.sep) for r in self._screen):
            return False
        if any(within(resolved, r) for r in self.index_roots):
            return True
        below = parts_below(resolved, self.manual_root)
        if not below:
            return False  # outside the manual root, or the root itself
        return not same_uid(below[0], self.own_uid())
