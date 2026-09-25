"""The semantic side of search: one query vector against the profile's
collection, joined back through the index.

The vector store only knows point ids (= chunk ids) and a small integer
payload, so every hit is checked against the index before it is trusted:

- the chunk must still exist (a crash between the index write and the vector
  delete leaves orphans until GC);
- its ``vec_gen`` must be the active generation (a point written for an older
  version of the chunk, or by a model the profile no longer uses, is stale);
- when the store filtered on the payload's file id, the chunk's file must be
  one of the files asked for — a payload that disagrees with the row is stale.

Scope handling follows the design's numbers: a scope of at most 2000 files is
pushed into the store as a payload filter; a larger (or unrestricted) scope
over-fetches k×5 (at most 400) and filters in the index, escalating once to
1600 when too few survive. Nothing here raises: an unready embedder, a
missing collection, a model change mid-way or a store error all come back as
``available=False`` with a reason the result header can show.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.userdocs.query.filters import Scope
from app.utils.logger import logger

PREFILTER_MAX_FILES = 2000
OVERFETCH = 5
OVERFETCH_CAP = 400
ESCALATE_TO = 1600


@dataclass
class VectorResult:
    hits: list[tuple[int, float]] = field(default_factory=list)   # (chunk id, cosine), best first
    available: bool = True
    reason: str | None = None
    gen: int | None = None
    stale_dropped: int = 0


def _unavailable(reason: str) -> VectorResult:
    return VectorResult(available=False, reason=reason)


def embed_query(emb: Any, text: str) -> list[float]:
    fn = getattr(emb, "embed_search_query", None) or getattr(emb, "embed_query")
    return list(fn(text))


def search_vector(
    db: Any,
    text: str,
    *,
    scope: Scope,
    k: int,
    accept: Callable[[dict[str, Any]], bool],
    ctypes: list[str] | None = None,
    handles: tuple[Any, Any] | None = None,
    vector: list[float] | None = None,
) -> VectorResult:
    """The ``k`` best chunks for ``text`` that pass ``accept`` (the engine's
    scope and visibility check). ``ctypes`` limits to chunk types (file or
    folder cards for the catalog). ``handles``/``vector`` let a caller that
    runs several lists embed the query once."""
    from app.userdocs import vectors as V

    if handles is None:
        from app.userdocs.vector_sync import live_handles

        handles = live_handles()
    if handles is None:
        return _unavailable("Vector Embedding is not ready")
    emb, store = handles
    try:
        coll = db.active_collection()
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"the index could not be read ({exc})")
    if not coll:
        return _unavailable("no vectors have been built for this index yet")
    try:
        same_model = coll.get("model_key") == emb.model_key and int(coll.get("dim") or 0) == int(emb.dimension)
    except Exception:  # noqa: BLE001 — an embedder without identity: cannot be trusted
        same_model = False
    if not same_model:
        # The embedder changed and the re-embed has not created the new
        # generation yet: this model's query vector means nothing to the old
        # collection (it may not even have the same dimension).
        return _unavailable("the embedding model changed; vectors are being rebuilt")
    gen = int(coll["gen"])
    name = coll["name"]
    if vector is None:
        try:
            vector = embed_query(emb, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[userdocs] query embedding failed: {exc}")
            return _unavailable("embedding the query failed")

    base: dict[str, Any] = {}
    if scope.source:
        base["sources"] = [V.source_code(scope.source)]
    if ctypes:
        base["ctypes"] = [V.ctype_code(c) for c in ctypes]

    res = VectorResult(gen=gen)

    def query(limit: int, filt: Any, *, critical: bool = True) -> list[tuple[int, float]] | None:
        try:
            return list(store.query_vectors(name, vector, int(limit), filt))
        except Exception as exc:  # noqa: BLE001 — store errors never escape
            logger.warning(f"[userdocs] vector query on {name} failed: {exc}")
            if critical:
                res.available = False
                res.reason = "the vector store did not answer" if not isinstance(exc, NotImplementedError) \
                    else "this vector store does not support document search"
            return None

    def validate(raw: list[tuple[int, float]], prefiltered: set[int] | None) -> list[tuple[int, float]]:
        rows = {int(r["id"]): r for r in db.chunk_rows([cid for cid, _ in raw])}
        out: list[tuple[int, float]] = []
        for cid, score in raw:
            row = rows.get(int(cid))
            if row is None or row.get("vec_gen") != gen:
                res.stale_dropped += 1
                continue
            fid = row.get("file_id")
            if prefiltered is not None and fid is not None and int(fid) not in prefiltered:
                res.stale_dropped += 1
                continue
            if accept(row):
                out.append((int(cid), float(score)))
        return out

    ids = scope.file_ids
    if ids is not None and len(ids) <= PREFILTER_MAX_FILES:
        if not ids:
            return res
        raw = query(k * 2, V.VectorFilter(file_ids=list(ids), **base))
        if raw is None:
            return res
        hits = validate(raw, set(int(i) for i in ids))
        if scope.cards_allowed and scope.folder_prefixes and not ctypes:
            # Folder cards have no file id, so the payload filter above
            # excludes them; fetch the few that may qualify separately.
            cards = query(10, V.VectorFilter(ctypes=[V.ctype_code("folder_card")], **{
                k2: v for k2, v in base.items() if k2 != "ctypes"}), critical=False)
            if cards:
                hits += validate(cards, None)
                hits.sort(key=lambda h: -h[1])
    else:
        fetch = min(OVERFETCH_CAP, max(k, k * OVERFETCH))
        filt = V.VectorFilter(**base) if base else None
        raw = query(fetch, filt)
        if raw is None:
            return res
        hits = validate(raw, None)
        if len(hits) < k and len(raw) >= fetch:
            more = query(ESCALATE_TO, filt)
            if more is not None:
                res.stale_dropped = 0
                hits = validate(more, None)
    res.hits = hits[: max(k, 0)]
    if res.stale_dropped:
        logger.debug(f"[userdocs] dropped {res.stale_dropped} stale vector hit(s)")
    return res


__all__ = ["ESCALATE_TO", "OVERFETCH", "OVERFETCH_CAP", "PREFILTER_MAX_FILES", "VectorResult", "embed_query",
           "search_vector"]
