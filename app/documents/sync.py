"""DocumentSyncService -- keeps disk, profile dirs, and the vector store in lockstep.

Single source of truth for the ``documentation_search`` collection in whichever
vector store back-end the user picked (Qdrant or Chroma).

Responsibilities
----------------
- ``seed_shared_from_app(...)``  -- on boot, mirror bundled
  ``<repo>/documents/*.md`` into ``<CREMIND_SYSTEM_DIR>/documents/`` exactly:
  missing files are copied in, divergent files are overwritten, and files
  not present in the bundle are deleted. The bundle is authoritative for
  system documents, so any in-session edits or extras only live until the
  next restart.
- ``full_reconcile(scope, profile=None)`` -- scan a scope's on-disk directory,
  upsert new/changed docs, delete points whose source files have disappeared.
- ``apply_event(scope, path, event_type)`` -- handle a single watcher event
  (created / modified / deleted / moved-from / moved-to).

Thread safety: watchdog dispatches callbacks on its own thread, so all
methods that mutate the collection take the same ``threading.Lock``.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator, Optional

from app.config.embedding_state import embedding_state
from app.documents.parser import parse_document
from app.lib.embedding import LocalEmbeddings
from app.utils.logger import logger
from app.vectorstores.base import StoredPoint

# ``StoredPoint`` is a provider-neutral TypedDict in
# ``app.vectorstores.base`` (core, no extras needed). Use it instead of
# ``qdrant_client.http.models.PointStruct`` so this module does not pull
# in qdrant-client when the user picked Chroma as their vector store.
if TYPE_CHECKING:
    from app.vectorstores.base import VectorStore

COLLECTION_NAME = "documentation_search"
SHARED_SCOPE = "shared"

# Retired scope. CLI-reference docs (the bundled ``[cli]cremind *.md`` files)
# once lived in their own ``"cli"`` scope, searched by a dedicated ``cli``
# built-in tool. That tool was removed and the docs folded back into the shared
# corpus; ``seed_shared_from_app`` drops any stale ``<working_dir>/cli`` tree and
# ``prune_scope("cli")`` removes leftover points so upgraded installs carry no
# orphaned CLI-scope state.
_LEGACY_CLI_SCOPE = "cli"

# In degraded mode (no embedding model / vector store) the LLM relevance judge
# is the ONLY discriminator, so it must see the whole shared system-doc set --
# not an arbitrary alphabetical prefix. Shared docs are therefore NEVER
# truncated: the bundle is authoritative, bounded, and grows with the product,
# so a cap sized against today's count silently hides new docs the moment it is
# outgrown. Only the per-profile corpus -- user-authored and unbounded -- is
# capped, and any truncation is logged.
FALLBACK_MAX_PROFILE_CANDIDATES = 50

# Deprecated alias kept for one release; prefer FALLBACK_MAX_PROFILE_CANDIDATES.
FALLBACK_MAX_CANDIDATES = FALLBACK_MAX_PROFILE_CANDIDATES

# How much of a frontmatter description is actually used. Only
# ``<identity>\n\n<description>`` is embedded, and the default model
# (intfloat/multilingual-e5-base) silently truncates at a 512-token window, so a
# long description's tail never reached the ranker at all. The same cap bounds
# what the relevance judge is shown per candidate (documentation_search), which
# matters in fallback mode where every document is a candidate. One number, so
# the authoring rule is simple: only the first DESCRIPTION_MAX_CHARS count, and
# a longer description is logged. ~1200 chars is about 300 gpt-4o tokens; XLM-R
# tokenizes denser, leaving margin under 512 even for multilingual text once
# the identity line is added.
DESCRIPTION_MAX_CHARS = 1200

# ``(scope, relpath)`` pairs already warned about this process, so a long
# description is reported once at boot rather than on every reconcile pass.
_OVERSIZED_DESCRIPTIONS_WARNED: set[tuple[str, str]] = set()

# Search paths, reported on the tool's per-search INFO line and logged once per
# transition so an operator can tell a degraded search from a ranked one.
MODE_VECTOR = "vector"
MODE_FALLBACK_DISABLED = "fallback-disabled"
MODE_FALLBACK_NO_COLLECTION = "fallback-no-collection"
MODE_FALLBACK_ERROR = "fallback-error"


class DocumentSyncService:
    """Sync `.md` documents under the working directory into the vector store.

    The embedding model and vector store are resolved from
    :data:`app.config.embedding_state.embedding_state` on **every call**, never
    captured at construction. Handles captured once went stale the moment an
    admin toggled Vector Embedding in Settings: the lifecycle rewires the agent
    and the state singleton but has no way to reach this service, so a disabled
    install kept vector-searching (and a freshly enabled one kept skipping
    indexing) until the process restarted.

    ``vector_store`` / ``embedding`` remain as an override seam for tests and
    for :meth:`pinned`, which the embedding lifecycle needs because it rebuilds
    into a store that has not been published READY yet.
    """

    def __init__(
        self,
        *,
        working_dir: Path,
        vector_store: Optional["VectorStore"] = None,
        embedding: Optional[LocalEmbeddings] = None,
    ):
        self._working_dir = Path(working_dir)
        self._vector_store_override = vector_store
        self._embedding_override = embedding
        self._lock = threading.Lock()
        self._last_search_mode: Optional[str] = None

    # ── Live handles ───────────────────────────────────────────────────────

    def _store(self) -> Optional["VectorStore"]:
        """The vector store to use right now, or None when unavailable."""
        if self._vector_store_override is not None:
            return self._vector_store_override
        return embedding_state.vector_store if embedding_state.is_ready() else None

    def _embedder(self) -> Optional[LocalEmbeddings]:
        """The embedding model to use right now, or None when unavailable."""
        if self._embedding_override is not None:
            return self._embedding_override
        return embedding_state.embedding if embedding_state.is_ready() else None

    @contextmanager
    def pinned(
        self,
        *,
        vector_store: Optional["VectorStore"],
        embedding: Optional[LocalEmbeddings],
    ) -> Iterator[None]:
        """Run a block against explicitly supplied handles.

        :func:`app.lib.embedding_lifecycle._rebuild_caches` reconciles into a
        store while ``embedding_state`` is still REBUILDING — ``is_ready()`` is
        False throughout, so the call-time accessors would resolve to None and
        the rebuild would silently index nothing. The previous overrides are
        restored on the way out (including on an exception), so a failed rebuild
        can never leave this service holding a handle the state has disowned.
        """
        previous = (self._vector_store_override, self._embedding_override)
        self._vector_store_override = vector_store
        self._embedding_override = embedding
        try:
            yield
        finally:
            self._vector_store_override, self._embedding_override = previous

    @property
    def last_search_mode(self) -> Optional[str]:
        """Path taken by the most recent :meth:`search` (see ``MODE_*``)."""
        return self._last_search_mode

    def _note_mode(self, mode: str, detail: str = "") -> str:
        """Record the search path, logging only when it CHANGES.

        Steady state stays silent; a flip between ranked and degraded search is
        exactly the event an operator needs in ``logs/app.log`` (the per-path
        DEBUG lines in :meth:`search` never reach the default INFO file sink).
        """
        previous = self._last_search_mode
        self._last_search_mode = mode
        if mode != previous:
            logger.info(
                f"[documents] search mode -> {mode} "
                f"(was {previous or 'n/a'}){detail}"
            )
        return mode

    # ── Public paths ────────────────────────────────────────────────────────

    def shared_dir(self) -> Path:
        return self._working_dir / "documents"

    def profile_dir(self, profile: str) -> Path:
        return self._working_dir / profile / "documents"

    def scope_dir(self, scope: str) -> Path:
        return self.shared_dir() if scope == SHARED_SCOPE else self.profile_dir(scope)

    # ── System-level seeding ───────────────────────────────────────────────

    def seed_shared_from_app(self, app_documents_dir: Path) -> None:
        """Mirror every bundled doc into the shared scope dir exactly.

        The bundle is authoritative for system documents, so on every boot:

        - Files missing from ``shared_dir()`` are copied in.
        - Files whose content differs from the bundle are overwritten.
        - Any file in ``shared_dir()`` not present in the bundle is deleted.

        Mid-session edits or extras therefore live only until the next restart.
        Profile-scoped docs under ``<working_dir>/<profile>/`` are unaffected.

        The bundled ``[cli]cremind *.md`` CLI-reference docs are part of this
        shared corpus (searched by ``documentation_search``). Older installs
        that seeded them into a separate ``<working_dir>/cli`` scope are cleaned
        up here — the stale tree is removed and its vector points are pruned by
        ``prune_scope("cli")`` at boot.
        """
        if not app_documents_dir.exists():
            return

        sources = list(app_documents_dir.glob("**/*.md"))
        self._mirror_bundle(sources, app_documents_dir, self.shared_dir())

        # Retire the legacy CLI scope directory from upgraded installs (its docs
        # now live in the shared corpus above).
        legacy_cli_dir = self._working_dir / _LEGACY_CLI_SCOPE
        if legacy_cli_dir.exists():
            shutil.rmtree(legacy_cli_dir, ignore_errors=True)

    def _mirror_bundle(
        self, sources: list[Path], app_documents_dir: Path, target: Path,
    ) -> None:
        """Mirror ``sources`` (a subset of the bundle) into ``target`` exactly.

        Copies new/changed files in and deletes any file in ``target`` that is
        not one of ``sources``. Nested scope roots do not overlap
        (``documents/`` vs ``cli/documents`` vs ``<profile>/documents``), so
        this never deletes another scope's files.
        """
        target.mkdir(parents=True, exist_ok=True)

        bundled_dst: set[Path] = set()
        copied = 0
        overwritten = 0

        for src in sources:
            rel = src.relative_to(app_documents_dir)
            dst = target / rel
            bundled_dst.add(dst.resolve())

            if dst.exists():
                try:
                    same = _hash_file(src) == _hash_file(dst)
                except OSError as e:
                    logger.warning(f"[documents] failed to hash {dst}: {e}")
                    continue
                if same:
                    continue
                try:
                    shutil.copy2(src, dst)
                    overwritten += 1
                except OSError as e:
                    logger.warning(f"[documents] failed to overwrite {src} -> {dst}: {e}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                except OSError as e:
                    logger.warning(f"[documents] failed to seed {src} -> {dst}: {e}")

        deleted = 0
        for path in list(target.rglob("*")):
            if path.is_dir():
                continue
            if path.resolve() in bundled_dst:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError as e:
                logger.warning(f"[documents] failed to delete extra {path}: {e}")

        if copied or overwritten or deleted:
            logger.info(
                f"[documents] mirror -> {target}: "
                f"copied={copied} overwritten={overwritten} deleted={deleted}"
            )

    # ── Reconciliation ─────────────────────────────────────────────────────

    def full_reconcile(self, scope: str) -> None:
        """Reconcile a single scope (``shared`` or a profile name) end-to-end.

        - Upsert every eligible `.md` whose content hash differs from the store.
        - Delete points for files that are no longer on disk or have lost
          their frontmatter.
        """
        store = self._store()
        if store is None:
            return

        directory = self.scope_dir(scope)
        directory.mkdir(parents=True, exist_ok=True)

        with self._lock:
            disk_state = self._scan_scope(scope)
            existing = self._fetch_scope_payloads(scope)

            disk_ids = {fid for fid, _ in disk_state.items()}
            existing_ids = {p["id"]: p for p in existing}

            to_delete = [pid for pid in existing_ids if pid not in disk_ids]
            if to_delete:
                self._delete_ids(to_delete)

            points: list[StoredPoint] = []
            for fid, payload in disk_state.items():
                cur = existing_ids.get(fid)
                if cur and cur.get("content_hash") == payload["content_hash"]:
                    continue
                embed_text = payload.pop("_embed_text")
                vector = self._embed(embed_text)
                if vector is None:
                    continue
                points.append({"id": fid, "vector": vector, "payload": payload})

            if points:
                if not self._ensure_collection(len(points[0]["vector"] or [])):
                    return
                self._upsert_points(points)
                logger.info(
                    f"[documents] reconcile scope={scope!r}: upserted {len(points)} "
                    f"deleted {len(to_delete)}"
                )
            elif to_delete:
                logger.info(
                    f"[documents] reconcile scope={scope!r}: deleted {len(to_delete)}"
                )

    def prune_scope(self, scope: str) -> None:
        """Delete every vector point belonging to ``scope`` (a one-shot cleanup).

        Unlike :meth:`full_reconcile`, this never scans disk or upserts — it
        removes all points whose payload ``scope`` matches, regardless of what
        is on disk. Used to retire a scope that no longer exists (e.g. the
        legacy ``cli`` corpus) so upgraded installs carry no orphaned points.
        Idempotent: a no-op once the scope is empty.
        """
        if self._store() is None:
            return
        with self._lock:
            existing = self._fetch_scope_payloads(scope)
            ids = [p["id"] for p in existing]
            if ids:
                self._delete_ids(ids)
                logger.info(f"[documents] pruned scope={scope!r}: deleted {len(ids)}")

    def apply_event(self, scope: str, path: Path) -> None:
        """Apply a single watcher event for ``path`` in ``scope``.

        Equivalent to a per-file reconcile: read disk, compute id+hash, then
        upsert or delete to match. Idempotent and tolerant of double-fired
        events.
        """
        if self._store() is None:
            return
        if path.suffix.lower() != ".md":
            return

        relpath = self._safe_relpath(scope, path)
        if relpath is None:
            return
        fid = self._file_id(scope, relpath)

        with self._lock:
            if not path.exists():
                self._delete_ids([fid])
                logger.debug(f"[documents] removed {scope}/{relpath} from vector store")
                return

            parsed = parse_document(path)
            if parsed is None:
                # File lost (or never had) valid frontmatter -- ensure it's gone.
                self._delete_ids([fid])
                logger.debug(
                    f"[documents] {scope}/{relpath} ineligible (no frontmatter "
                    "with description); skipped"
                )
                return

            _warn_if_description_oversized(
                scope=scope, relpath=relpath, description=parsed.description,
            )
            embed_text = _embedding_text(Path(relpath).stem, parsed.description)
            content_hash = _hash_text(embed_text + "\0" + parsed.body)
            existing = self._fetch_one(fid)
            if existing and existing.get("content_hash") == content_hash:
                return

            vector = self._embed(embed_text)
            if vector is None:
                return
            if not self._ensure_collection(len(vector)):
                return
            payload = self._build_payload(
                scope=scope,
                relpath=relpath,
                file_path=str(path),
                description=parsed.description,
                content_hash=content_hash,
            )
            self._upsert_points([
                {"id": fid, "vector": vector, "payload": payload},
            ])
            logger.info(f"[documents] upserted {scope}/{relpath}")

    # ── Read helpers (used by the search tool) ─────────────────────────────

    def search(
        self,
        *,
        query: str,
        profile: str,
        limit: int = 10,
        scopes: Optional[list[str]] = None,
    ) -> list[dict]:
        """Vector-search the collection, filtered to ``scopes``.

        ``scopes`` defaults to ``[shared, profile]`` (the general documentation
        corpus). It is a generic filter, so a caller may narrow the search to a
        subset of scopes if needed.

        Returns flat payload dicts plus ``id`` and ``score`` keys. Body
        content is loaded from disk by the caller, not stored here.

        When Vector Embedding is disabled (no embedding model and/or no
        vector store), when the collection has not been built yet, or when the
        store errors, falls back to enumerating every eligible document in the
        requested scopes from disk so the LLM relevance judge in the search
        tool still has candidates to choose from. :attr:`last_search_mode`
        records which path ran.
        """
        scopes = scopes if scopes is not None else [SHARED_SCOPE, profile]
        logger.debug(
            f"[documents] search: query={query!r} profile={profile!r} "
            f"scopes={scopes} limit={limit}"
        )
        store = self._store()
        embedder = self._embedder()
        if store is None or embedder is None:
            self._note_mode(MODE_FALLBACK_DISABLED)
            return self._list_all_for_scopes(scopes=scopes)

        state = self._collection_state(store)
        if state == "error":
            self._note_mode(MODE_FALLBACK_ERROR, " — vector store unreachable")
            return self._list_all_for_scopes(scopes=scopes)
        if state == "missing":
            self._note_mode(
                MODE_FALLBACK_NO_COLLECTION,
                f" — collection {COLLECTION_NAME!r} not built yet",
            )
            return self._list_all_for_scopes(scopes=scopes)

        try:
            vector = embedder.embed_query(query)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] embed_query failed: {e}")
            self._note_mode(MODE_FALLBACK_ERROR, f" — embed_query failed: {e}")
            return self._list_all_for_scopes(scopes=scopes)

        try:
            hits = store.query_by_vector(
                collection_name=COLLECTION_NAME,
                vector=vector,
                limit=limit,
                filter={"scope": scopes},
            )
        except Exception as e:  # noqa: BLE001
            # Degrade to the full scan rather than reporting an empty result:
            # an unreachable store used to surface to the agent as "no relevant
            # result found", i.e. indistinguishable from a genuine miss.
            logger.warning(f"[documents] query_by_vector failed: {e}")
            self._note_mode(MODE_FALLBACK_ERROR, f" — query_by_vector failed: {e}")
            return self._list_all_for_scopes(scopes=scopes)

        self._note_mode(MODE_VECTOR)
        return hits

    def _list_all_for_scopes(
        self, *, scopes: list[str], profile_limit: int = FALLBACK_MAX_PROFILE_CANDIDATES,
    ) -> list[dict]:
        """Return every eligible doc in ``scopes``, no ranking.

        Used as the fallback when vector search is unavailable (embedding
        disabled, store not connected, collection missing, or a store error).
        The caller's LLM judge does the relevance pick over the resulting list.

        Documents are ordered scope-then-relpath following ``scopes`` order
        (shared/system docs first by default). Because the judge is the sole
        discriminator in this mode, every SHARED doc is always included: the
        bundle is authoritative and grows with the product, so a fixed cap
        sized against yesterday's count silently hides new system docs -- which
        is exactly what once made tail-sorted docs like ``profile``
        unreachable. Only the per-profile corpus, which is user-authored and
        unbounded, is capped, and any truncation is logged, never silent.
        """
        results: list[dict] = []
        for scope in scopes:
            scanned = self._scan_scope(scope)
            payloads = sorted(
                scanned.values(),
                key=lambda p: (p.get("scope", ""), p.get("relpath", "")),
            )
            rows: list[dict] = []
            for payload in payloads:
                payload = dict(payload)
                payload.pop("_embed_text", None)
                payload["score"] = 0.0
                rows.append(payload)
            if scope != SHARED_SCOPE and profile_limit and len(rows) > profile_limit:
                logger.warning(
                    f"[documents] full-scan fallback truncated scope={scope!r} "
                    f"from {len(rows)} to {profile_limit} candidates; "
                    f"{len(rows) - profile_limit} doc(s) hidden from the "
                    f"relevance judge (raise FALLBACK_MAX_PROFILE_CANDIDATES or "
                    f"enable vector search)"
                )
                rows = rows[:profile_limit]
            results.extend(rows)
        return results

    def list_document_names(self, scopes: list[str]) -> list[dict]:
        """Every eligible document in ``scopes``, in scope-then-relpath order.

        Lightweight rows (``name``, ``scope``, ``relpath``, ``file_path``,
        ``description``) for name resolution and "did you mean" suggestions —
        no vector store involved.
        """
        rows: list[dict] = []
        for scope in scopes:
            payloads = sorted(
                self._scan_scope(scope).values(),
                key=lambda p: (p.get("scope", ""), p.get("relpath", "")),
            )
            for payload in payloads:
                rows.append({
                    "name": payload.get("name") or "",
                    "scope": payload.get("scope") or scope,
                    "relpath": payload.get("relpath") or "",
                    "file_path": payload.get("file_path") or "",
                    "description": payload.get("text") or "",
                })
        return rows

    @staticmethod
    def qualified_reference(row: dict) -> str:
        """``<scope>/<relpath>``: names exactly one document, always.

        A bare name (the stem) can be shared — a profile doc named like a
        bundled one, or two nested files with the same stem — so this is what
        a caller prints when the bare name would not lead back to this doc.
        """
        return f"{row['scope']}/{row['relpath']}"

    def resolve_document(
        self, name: str, scopes: list[str], *, rows: Optional[list[dict]] = None,
    ) -> list[dict]:
        """Every document in ``scopes`` that ``name`` refers to (0, 1 or many).

        ``name`` is agent-supplied, so it is only ever COMPARED with what this
        service published — never used to build a path — and only rows from
        ``scopes`` are considered, so the caller's ``[shared, profile]`` cannot
        reach another profile's documents however ``name`` is spelled.

        Accepted forms: a qualified reference (``shared/[cli]cremind
        channels.md``, see :meth:`qualified_reference`), or a document name
        with the ``[tag]``, the ``.md`` suffix and case all optional. A name
        that fits several documents returns all of them rather than silently
        picking one — the caller reports the ambiguity. A name that carries its
        tag only matches documents with exactly that tagged name.
        """
        candidate = (name or "").strip()
        if not candidate:
            return []
        rows = self.list_document_names(scopes) if rows is None else rows

        qualified = [r for r in rows if self.qualified_reference(r) == candidate]
        if not qualified:
            qualified = [
                r for r in rows
                if self.qualified_reference(r).casefold() == candidate.casefold()
            ]
        if qualified:
            return qualified[:1]

        if candidate.lower().endswith(".md"):
            candidate = candidate[: -len(".md")]
        cleaned = _clean_name(candidate).casefold()
        matches = [r for r in rows if _clean_name(r["name"]).casefold() == cleaned]
        if _clean_name(candidate) != candidate:  # the caller gave the tag
            tagged = [r for r in matches if r["name"].casefold() == candidate.casefold()]
            if tagged:
                matches = tagged
        return matches

    def find_document(self, name: str, scopes: list[str]) -> Optional[dict]:
        """The one document ``name`` refers to, or None when it names none or
        several (see :meth:`resolve_document`)."""
        matches = self.resolve_document(name, scopes)
        return matches[0] if len(matches) == 1 else None

    def reference_for(self, *, scope: str, relpath: str, scopes: list[str]) -> str:
        """The shortest reference that resolves back to exactly this document:
        its bare name when that is unambiguous in ``scopes``, else its
        qualified reference."""
        rows = self.list_document_names(scopes)
        target = next(
            (r for r in rows if r["scope"] == scope and r["relpath"] == relpath), None,
        )
        if target is None:
            return f"{scope}/{relpath}"
        if self.resolve_document(target["name"], scopes, rows=rows) == [target]:
            return target["name"]
        return self.qualified_reference(target)

    @staticmethod
    def read_body(path: Path) -> Optional[str]:
        """Re-parse ``path`` and return only the body (frontmatter excluded).

        Returns None if the file is no longer eligible (e.g. it was deleted
        or had its frontmatter removed between search and read).
        """
        parsed = parse_document(path)
        return None if parsed is None else parsed.body

    # ── Internals: scanning and Qdrant plumbing ────────────────────────────

    def _scan_scope(self, scope: str) -> dict[int, dict]:
        """Build ``{file_id: payload-dict-with-_embed_text}`` for a scope."""
        directory = self.scope_dir(scope)
        out: dict[int, dict] = {}
        if not directory.exists():
            return out

        for path in directory.glob("**/*.md"):
            parsed = parse_document(path)
            if parsed is None:
                continue
            relpath = self._safe_relpath(scope, path)
            if relpath is None:
                continue
            fid = self._file_id(scope, relpath)
            _warn_if_description_oversized(
                scope=scope, relpath=relpath, description=parsed.description,
            )
            embed_text = _embedding_text(Path(relpath).stem, parsed.description)
            content_hash = _hash_text(embed_text + "\0" + parsed.body)
            payload = self._build_payload(
                scope=scope,
                relpath=relpath,
                file_path=str(path),
                description=parsed.description,
                content_hash=content_hash,
            )
            # Transient (stripped before upsert): the exact string to embed, so
            # full_reconcile embeds precisely what the content hash covers.
            payload["_embed_text"] = embed_text
            out[fid] = payload
        return out

    def _fetch_scope_payloads(self, scope: str) -> list[dict]:
        store = self._store()
        if store is None or not store.collection_exists(COLLECTION_NAME):
            return []
        try:
            points = store.list_all_points(
                collection_name=COLLECTION_NAME,
                with_vectors=False,
                filter={"scope": scope},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] list_all_points failed for scope={scope}: {e}")
            return []
        rows: list[dict] = []
        for p in points:
            payload = dict(p.get("payload") or {})
            payload["id"] = p["id"]
            rows.append(payload)
        return rows

    def _fetch_one(self, fid: int) -> Optional[dict]:
        store = self._store()
        if store is None or not store.collection_exists(COLLECTION_NAME):
            return None
        try:
            records = store.get_texts(
                collection_name=COLLECTION_NAME, ids=[fid],
            )
        except Exception:  # noqa: BLE001
            return None
        if not records:
            return None
        rec = records[0]
        payload = dict(rec.get("metadata") or {})
        if rec.get("text") is not None:
            payload["text"] = rec["text"]
        return payload

    @staticmethod
    def _collection_state(store) -> str:
        """``"present"``, ``"missing"`` or ``"error"`` for the docs collection.

        Never ``collection_exists``: both adapters turn ANY failure into False,
        which reads "missing" during a network blip — and both implement
        ``create_named_collection`` as drop-and-recreate (Qdrant
        ``recreate_collection``; Chroma delete then create). Acting on that
        False would wipe every indexed document. ``list_collections`` raises
        instead of guessing, so an unreachable store is reported as one.
        """
        try:
            names = store.list_collections()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] could not list vector-store collections: {e}")
            return "error"
        return "present" if COLLECTION_NAME in names else "missing"

    def _ensure_collection(self, dimension: int) -> bool:
        """Make sure the collection exists; False when it cannot be confirmed.

        Stateless on purpose: a cached "already created" flag belongs to the
        store it was set for, and the lifecycle can swap in a brand new (empty)
        store at any time. The collection is created ONLY when it is confirmed
        missing (see :meth:`_collection_state`); on an error nothing is created
        and the caller skips its write, so a transient failure costs one
        missed update instead of the whole index.
        """
        client = self._raw_client()
        if client is None:
            return False
        state = self._collection_state(client)
        if state == "present":
            return True
        if state == "error":
            logger.warning(
                "[documents] vector store unreachable; skipping this write rather "
                "than risk recreating the collection"
            )
            return False
        client.create_named_collection(
            collection_name=COLLECTION_NAME, size=dimension,
        )
        logger.info(
            f"[documents] created collection "
            f"{COLLECTION_NAME!r} (dim={dimension})"
        )
        return True

    def _upsert_points(self, points: Iterable[StoredPoint]) -> None:
        client = self._raw_client()
        if client is None:
            return
        stored = [
            {"id": p["id"], "vector": list(p["vector"] or []), "payload": dict(p["payload"] or {})}
            for p in points
        ]
        client.add_points(collection_name=COLLECTION_NAME, points=stored)

    def _delete_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        client = self._raw_client()
        if client is None or not client.collection_exists(COLLECTION_NAME):
            return
        try:
            client.delete_texts(collection_name=COLLECTION_NAME, ids=ids)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] delete_ids failed: {e}")

    def _embed(self, text: str) -> Optional[list[float]]:
        embedder = self._embedder()
        if embedder is None:
            return None
        try:
            return embedder.embed_query(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] embedding failed: {e}")
            return None

    def _raw_client(self):
        store = self._store()
        if store is None:
            return None
        return getattr(store, "_client", None)

    def _safe_relpath(self, scope: str, path: Path) -> Optional[str]:
        base = self.scope_dir(scope)
        try:
            return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
        except ValueError:
            return None

    @staticmethod
    def _file_id(scope: str, relpath: str) -> int:
        # Qdrant point ids must be unsigned 64-bit integers.
        digest = hashlib.blake2b(
            f"{scope}/{relpath}".encode("utf-8"), digest_size=8,
        ).digest()
        return int.from_bytes(digest, "big")

    def _build_payload(
        self,
        *,
        scope: str,
        relpath: str,
        file_path: str,
        description: str,
        content_hash: str,
    ) -> dict:
        return {
            "text": description,
            "scope": scope,
            "relpath": relpath,
            "file_path": file_path,
            "content_hash": content_hash,
            "name": Path(relpath).stem,
        }


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean_name(stem: str) -> str:
    """Strip a leading ``[tag]`` from a filename stem for embedding.

    Doc stems look like ``[cli]cremind profile``; the bracketed tag is a
    filename convention, not identity. Dropping it lets the embedder see
    ``cremind profile`` as clean identity tokens rather than a fragmented
    bracketed blob.
    """
    if stem.startswith("[") and "]" in stem:
        stem = stem.split("]", 1)[1]
    return stem.strip()


def _cap_description_for_embedding(description: str) -> str:
    """Cut ``description`` to :data:`DESCRIPTION_MAX_CHARS` on a word break.

    Returned unchanged when it already fits. The cut is at the last whitespace
    inside the budget so the embedder never sees a half word; if there is no
    whitespace to break on, it is a hard slice.
    """
    if len(description) <= DESCRIPTION_MAX_CHARS:
        return description
    head = description[:DESCRIPTION_MAX_CHARS]
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else head).rstrip()


def _embedding_text(stem: str, description: str) -> str:
    """Compose the text embedded for a doc: identity (name) then description.

    The cleaned name leads so the doc's own identity term (e.g. ``profile``)
    carries weight in the vector instead of being drowned out by boilerplate
    the descriptions share across docs. This is the *single* definition of
    what gets embedded — the content hash (in the sync methods above) hashes
    exactly this string so any change to the formula forces a re-embed.

    The description is capped (see :data:`DESCRIPTION_MAX_CHARS`) because the
    model truncates silently at its own window: past that point the extra
    keywords were doing nothing for retrieval while reading as if they were.
    """
    ident = _clean_name(stem)
    capped = _cap_description_for_embedding(description)
    if not ident:
        return capped
    return f"{ident}\n\n{capped}"


def _warn_if_description_oversized(
    *, scope: str, relpath: str, description: str,
) -> None:
    """Warn once per document per process when its description will be cut."""
    if len(description) <= DESCRIPTION_MAX_CHARS:
        return
    key = (scope, relpath)
    if key in _OVERSIZED_DESCRIPTIONS_WARNED:
        return
    _OVERSIZED_DESCRIPTIONS_WARNED.add(key)
    logger.warning(
        f"[documents] description of {scope}/{relpath} is {len(description)} "
        f"chars; only the first {DESCRIPTION_MAX_CHARS} are used for ranking "
        f"and shown to the relevance judge — trim it, see document.md"
    )
