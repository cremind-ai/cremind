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
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

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
# is the ONLY discriminator, so it must see the whole bounded system-doc set --
# not an arbitrary alphabetical prefix. This cap is deliberately far above both
# the vector ``top_k`` and the ~18 bundled docs; it exists only to bound a
# pathologically large per-profile corpus, and any truncation is logged.
FALLBACK_MAX_CANDIDATES = 50


class DocumentSyncService:
    """Sync `.md` documents under the working directory into Qdrant."""

    def __init__(
        self,
        *,
        working_dir: Path,
        vector_store: Optional["VectorStore"],
        embedding: Optional[LocalEmbeddings],
    ):
        self._working_dir = Path(working_dir)
        self._vector_store = vector_store
        self._embedding = embedding
        self._lock = threading.Lock()
        self._collection_ready = False

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

        - Upsert every eligible `.md` whose content hash differs from Qdrant.
        - Delete points for files that are no longer on disk or have lost
          their frontmatter.
        """
        if self._vector_store is None:
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
                self._ensure_collection(len(points[0]["vector"] or []))
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
        if self._vector_store is None:
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
        if self._vector_store is None:
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

            embed_text = _embedding_text(Path(relpath).stem, parsed.description)
            content_hash = _hash_text(embed_text + "\0" + parsed.body)
            existing = self._fetch_one(fid)
            if existing and existing.get("content_hash") == content_hash:
                return

            vector = self._embed(embed_text)
            if vector is None:
                return
            self._ensure_collection(len(vector))
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
        vector store), falls back to enumerating every eligible document in
        the requested scopes from disk — capped at ``limit`` — so the LLM
        relevance judge in the search tool still has candidates to choose
        from.
        """
        scopes = scopes if scopes is not None else [SHARED_SCOPE, profile]
        logger.debug(
            f"[documents] search: query={query!r} profile={profile!r} "
            f"scopes={scopes} limit={limit}"
        )
        if self._vector_store is None or self._embedding is None:
            logger.debug("[documents] vector search unavailable, falling back to full scan")
            return self._list_all_for_scopes(
                scopes=scopes, limit=FALLBACK_MAX_CANDIDATES
            )

        if not self._vector_store.collection_exists(COLLECTION_NAME):
            logger.debug("[documents] collection does not exist, falling back to full scan")
            return self._list_all_for_scopes(
                scopes=scopes, limit=FALLBACK_MAX_CANDIDATES
            )

        try:
            vector = self._embedding.embed_query(query)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] embed_query failed: {e}")
            return []

        try:
            hits = self._vector_store.query_by_vector(
                collection_name=COLLECTION_NAME,
                vector=vector,
                limit=limit,
                filter={"scope": scopes},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] query_by_vector failed: {e}")
            return []

        return hits

    def _list_all_for_scopes(self, *, scopes: list[str], limit: int) -> list[dict]:
        """Return every eligible doc in ``scopes``, no ranking.

        Used as the fallback when vector search is unavailable (embedding
        disabled or vector store not connected). The caller's LLM judge
        does the relevance pick over the resulting list.

        Documents are ordered scope-then-relpath following ``scopes`` order
        (shared/system docs first by default), so they survive truncation.
        Because the judge is the sole discriminator in this mode, ``limit`` must
        be generous enough to include the whole system-doc set (see
        ``FALLBACK_MAX_CANDIDATES``); capping below it hides docs from the judge
        -- which is exactly what made tail-sorted docs like ``profile``
        unreachable. The cap therefore only bounds a pathologically large
        per-profile corpus, and any truncation is logged, never silent.
        """
        results: list[dict] = []
        for scope in scopes:
            scanned = self._scan_scope(scope)
            payloads = sorted(
                scanned.values(),
                key=lambda p: (p.get("scope", ""), p.get("relpath", "")),
            )
            for payload in payloads:
                payload = dict(payload)
                payload.pop("_embed_text", None)
                payload["score"] = 0.0
                results.append(payload)
        if limit and len(results) > limit:
            logger.warning(
                f"[documents] full-scan fallback truncated {len(results)} "
                f"candidates to {limit}; {len(results) - limit} doc(s) hidden "
                f"from the relevance judge (raise FALLBACK_MAX_CANDIDATES or "
                f"enable vector search)"
            )
            results = results[:limit]
        return results

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
        if self._vector_store is None or not self._vector_store.collection_exists(COLLECTION_NAME):
            return []
        try:
            points = self._vector_store.list_all_points(
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
        if self._vector_store is None or not self._vector_store.collection_exists(COLLECTION_NAME):
            return None
        try:
            records = self._vector_store.get_texts(
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

    def _ensure_collection(self, dimension: int) -> None:
        if self._collection_ready or self._vector_store is None:
            return
        client = self._raw_client()
        if client is None:
            return
        if not client.collection_exists(COLLECTION_NAME):
            client.create_named_collection(
                collection_name=COLLECTION_NAME, size=dimension,
            )
            logger.info(
                f"[documents] created collection "
                f"{COLLECTION_NAME!r} (dim={dimension})"
            )
        self._collection_ready = True

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
        if self._embedding is None:
            return None
        try:
            return self._embedding.embed_query(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] embedding failed: {e}")
            return None

    def _raw_client(self):
        if self._vector_store is None:
            return None
        return getattr(self._vector_store, "_client", None)

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


def _embedding_text(stem: str, description: str) -> str:
    """Compose the text embedded for a doc: identity (name) then description.

    The cleaned name leads so the doc's own identity term (e.g. ``profile``)
    carries weight in the vector instead of being drowned out by boilerplate
    the descriptions share across docs. This is the *single* definition of
    what gets embedded — the content hash (below, in the sync methods) hashes
    exactly this string so any change to the formula forces a re-embed.
    """
    ident = _clean_name(stem)
    if not ident:
        return description
    return f"{ident}\n\n{description}"
