"""Filling in vectors: new chunks, backlogs, and re-embeds after a model change.

The pipeline writes chunks with ``vec_gen = NULL``. This module turns them into
vectors in the profile's *active collection generation* and stamps them with
that generation. A chunk is "embedded" exactly when its ``vec_gen`` equals the
active generation, which makes three situations the same loop:

- **New or edited content** — the diff inserted a few rows with no ``vec_gen``.
- **A backlog** — the vector store was unreachable for a while; lexical search
  stayed current and the vectors catch up when it returns.
- **A model or store change** — a new generation is created for the live
  (model, dimension, store); every chunk now lacks a vector *for it*, and the
  re-embed runs from the text already in the index, with no re-extraction and
  no new vision calls.

It never touches the global ``embedding_state`` busy flag (that would refuse
every chat) and pauses while that flag is set by someone else, so a batch
never straddles a model swap: each batch pins the embedder and collection it
started with.
"""

from __future__ import annotations

import time
from typing import Any

from app.utils.logger import logger

# Chunks embedded per step for one profile. Small enough that pause, shutdown
# and a waiting search query are noticed between steps.
BATCH = 48
# How often a live collection's existence is re-checked (a wiped store volume
# must lead to a rebuild from the index, not to silent search misses).
EXISTENCE_CHECK_S = 300.0


def store_key() -> str:
    """Which physical store vectors live in. A change means the new store is
    empty and everything must be re-embedded into it."""
    from app.config.settings import BaseConfig

    provider = (BaseConfig.get_vectorstore_provider() or "").lower()
    if provider == "chroma":
        if (BaseConfig.get_chroma_mode() or "") == "persistent":
            return f"chroma:persistent:{BaseConfig.get_chroma_persist_path()}"
        return f"chroma:http:{BaseConfig.get_chroma_host()}:{BaseConfig.get_chroma_port()}"
    return f"qdrant:{BaseConfig.get_qdrant_host()}:{BaseConfig.get_qdrant_port()}"


def live_handles() -> tuple[Any, Any] | None:
    """(embedding, vector_store) when the subsystem is ready, else None."""
    from app.config.embedding_state import embedding_state

    if not embedding_state.is_ready() or embedding_state.is_busy():
        return None
    emb, store = embedding_state.embedding, embedding_state.vector_store
    if emb is None or store is None:
        return None
    return emb, store


def active_collection(rt: Any, emb: Any, store: Any) -> dict[str, Any] | None:
    """The profile's collection for the live model and store, creating a new
    generation (and retiring the old one) when the model or store changed."""
    from app.documents.vectors import PAYLOAD_INDEXES, collection_name

    db = rt.db
    if db is None:
        return None
    model_key, dim, skey = emb.model_key, int(emb.dimension), store_key()
    active = db.active_collection()
    if active and active.get("model_key") == model_key and int(active.get("dim") or 0) == dim \
            and active.get("store_key") == skey:
        now = time.monotonic()
        if now - rt.last_vector_check > EXISTENCE_CHECK_S:
            rt.last_vector_check = now
            try:
                present = active["name"] in store.list_collections()
            except Exception as exc:  # noqa: BLE001 — unreachable store: try later
                logger.debug(f"[documents] {rt.profile}: cannot list collections: {exc}")
                return None
            if not present:
                logger.warning(f"[documents] {rt.profile}: collection {active['name']} vanished; re-embedding")
                store.ensure_collection(active["name"], dim, payload_indexes=PAYLOAD_INDEXES)
                db.clear_vec_gen()
                db.add_activity("reembed", "The vector store lost this index; rebuilding it from the stored text.",
                                level="warning")
        return active

    gen = db.next_gen()
    name = collection_name(rt.uid, db.epoch, model_key, dim)
    store.ensure_collection(name, dim, payload_indexes=PAYLOAD_INDEXES)
    db.add_collection(gen, name, model_key=model_key, dim=dim, store_key=skey, state="active")
    if active:
        # The old generation cannot answer queries from the new model, so it
        # is dropped now rather than kept for a rollback that could not work.
        if active.get("store_key") == skey and active.get("name") != name:
            try:
                store.delete_collection(active["name"])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[documents] {rt.profile}: dropping {active['name']} failed: {exc}")
        db.delete_collection_row(int(active["gen"]))
        db.add_activity(
            "reembed",
            f"Embedding model or vector store changed; re-embedding the index from stored text ({model_key}).",
            detail={"from": active.get("model_key"), "to": model_key},
        )
    rt.last_vector_check = time.monotonic()
    return db.active_collection()


def _embed_text(row: dict[str, Any]) -> str:
    heading = row.get("heading") or ""
    text = row.get("text") or ""
    return f"{heading}\n{text}" if heading else text


def _payloads(rt: Any, rows: list[dict[str, Any]]) -> list[dict[str, int]]:
    from app.documents.vectors import epoch_day, make_payload

    files: dict[int, dict[str, Any] | None] = {}
    out: list[dict[str, int]] = []
    for r in rows:
        fid = r.get("file_id")
        f = None
        if fid is not None:
            if fid not in files:
                files[fid] = rt.db.get_file(int(fid))
            f = files[fid]
        day = None
        if f:
            ts = f.get("taken_at") or f.get("doc_created_at") or f.get("mtime")
            day = epoch_day(float(ts)) if ts else None
        out.append(make_payload(
            source=r.get("source") or "local", ctype=r.get("ctype") or "body",
            file_id=int(fid) if fid is not None else None,
            folder_id=int(r["folder_id"]) if r.get("folder_id") is not None else (f or {}).get("folder_id"),
            kind=(f or {}).get("kind"), day=day,
        ))
    return out


def _syncing(rt: Any) -> bool:
    """Either source of the profile is on: the local folder syncing, or
    Google Drive (a Drive-only profile embeds just the same)."""
    drive = getattr(rt, "drive", None)
    return bool(rt.active or (drive is not None and drive.enabled))


def step(rt: Any) -> int:
    """Embed one batch for ``rt``; returns how many chunks got vectors."""
    from app.documents import governor as gov

    if rt.db is None or not _syncing(rt) or rt.paused_user:
        return 0
    handles = live_handles()
    if handles is None:
        return 0
    emb, store = handles
    try:
        level = gov.Level(rt.level)
    except ValueError:
        level = gov.Level.OK
    if not gov.Governor.allows(level, "upsert"):
        return 0
    try:
        coll = active_collection(rt, emb, store)
    except Exception as exc:  # noqa: BLE001 — store down: vectors wait, lexical is current
        logger.debug(f"[documents] {rt.profile}: collection unavailable: {exc}")
        return 0
    if not coll:
        return 0
    name, gen = coll["name"], int(coll["gen"])
    db = rt.db

    deletes = rt.take_vector_deletes()
    if deletes:
        try:
            store.delete_ids(name, deletes)
        except Exception as exc:  # noqa: BLE001 — orphans are dropped at search and by GC
            logger.debug(f"[documents] {rt.profile}: vector delete failed: {exc}")

    rows = db.chunks_needing_vectors(gen=gen, limit=BATCH)
    if not rows:
        _report_coverage(rt, gen)
        return 0

    # Identical text elsewhere in this profile already has a vector in this
    # generation (a copied file, a moved paragraph): reuse it.
    hashes = [r["text_hash"] for r in rows if r.get("text_hash")]
    reuse_src = db.reusable_vectors(hashes, gen=gen) if hashes else {}
    reuse: dict[int, list[float]] = {}
    if reuse_src:
        try:
            got = store.retrieve_vectors(name, list(set(reuse_src.values())))
        except Exception:  # noqa: BLE001
            got = {}
        for r in rows:
            src = reuse_src.get(r.get("text_hash"))
            if src is not None and src != r["id"] and src in got:
                reuse[int(r["id"])] = got[src]

    todo = [r for r in rows if int(r["id"]) not in reuse]
    vectors: dict[int, list[float]] = dict(reuse)
    if todo:
        try:
            embedded = emb.embed_passages([_embed_text(r) for r in todo])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[documents] {rt.profile}: embedding failed: {exc}")
            return 0
        for r, v in zip(todo, embedded):
            vectors[int(r["id"])] = v

    ids = [int(r["id"]) for r in rows if int(r["id"]) in vectors]
    try:
        store.upsert_vectors(name, ids, [vectors[i] for i in ids], _payloads(rt, [r for r in rows if int(r["id"]) in vectors]))
    except Exception as exc:  # noqa: BLE001
        if gov.is_enospc(exc):
            rt.service.note_enospc(rt)
        logger.warning(f"[documents] {rt.profile}: vector upsert failed: {exc}")
        return 0
    db.set_vec_gen(ids, gen)
    _report_coverage(rt, gen)
    return len(ids)


def flush_deletes(rt: Any) -> int:
    """Delete the vectors of chunks already gone from the index, now — for a
    runtime about to close its index file (both sources off), after which no
    embedding step would do it. Best effort: an unreachable store leaves
    them to search-time filtering and the weekly GC."""
    handles = live_handles()
    if handles is None or rt.db is None:
        return 0
    _emb, store = handles
    try:
        coll = rt.db.active_collection()
    except Exception:  # noqa: BLE001
        return 0
    ids = rt.take_vector_deletes()
    if not coll or not ids:
        return 0
    try:
        store.delete_ids(coll["name"], ids)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[documents] {rt.profile}: vector delete failed: {exc}")
        return 0
    return len(ids)


def _report_coverage(rt: Any, gen: int) -> None:
    try:
        st = rt.db.stats()
    except Exception:  # noqa: BLE001
        return
    total = int(st.get("chunks") or 0)
    missing = int(st.get("chunks_without_vectors") or 0)
    rt.progress.set_reembed(total - missing, total)


def gc(rt: Any) -> int:
    """Delete vectors whose chunk no longer exists (a crash between the index
    write and the vector delete). Returns how many were removed."""
    handles = live_handles()
    if handles is None or rt.db is None:
        return 0
    _emb, store = handles
    coll = rt.db.active_collection()
    if not coll:
        return 0
    removed = 0
    offset = None
    for _ in range(10_000):
        try:
            ids, offset = store.scroll_ids(coll["name"], offset=offset, limit=1000)
        except Exception:  # noqa: BLE001
            return removed
        if ids:
            known = {int(r["id"]) for r in rt.db.chunk_rows(ids)}
            orphans = [i for i in ids if int(i) not in known]
            if orphans:
                try:
                    store.delete_ids(coll["name"], orphans)
                    removed += len(orphans)
                except Exception:  # noqa: BLE001
                    return removed
        if offset is None:
            break
    if removed:
        logger.info(f"[documents] {rt.profile}: removed {removed} orphan vectors")
    return removed


def drop_collections(uid: str) -> int:
    """Drop every collection belonging to a profile uid (profile deleted, or
    its whole index deleted). Best effort: an unreachable store keeps them
    until the boot garbage collector finds them."""
    from app.documents.vectors import parse_collection_name, profile_tag

    handles = live_handles()
    if handles is None:
        return 0
    _emb, store = handles
    tag = profile_tag(uid)
    n = 0
    try:
        names = store.list_collections()
    except Exception:  # noqa: BLE001
        return 0
    for name in names:
        parsed = parse_collection_name(name)
        if parsed and parsed[0] == tag:
            try:
                store.delete_collection(name)
                n += 1
            except Exception:  # noqa: BLE001
                pass
    return n
