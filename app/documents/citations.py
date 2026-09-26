"""The citation registry: what the tools printed, checked against what the answer says.

A Documentation Search tool prints a token (``[doc:k7m2xq9a#3f9c2e1b]``, see
:mod:`app.documents.cite`) next to every passage it returns and records it here
(:func:`issue`). When the answer is saved, :func:`finalize_citations` looks at
every token *in the answer* and says how far each one can be trusted:

- ``verified`` — issued in this conversation, and the file/chunk still exists;
- ``verified_elsewhere`` — issued to this profile, in another conversation;
- ``unissued`` — it resolves in the index, but no tool printed it: the model
  guessed or reconstructed it, so the passage was never actually read;
- ``stale`` — issued, but the chunk has since changed or gone; the registry's
  snapshot (path, locator, snippet) is what can still be shown;
- ``removed`` — the file or folder is gone from the index;
- ``invalid`` — malformed, or unknown to this profile (an invented id, or
  another profile's — the lookup never leaves the caller's registry and index).

A quoted passage placed right before a token is also checked against the chunk
it cites (:mod:`app.documents.quotes`), so "the contract says '…'" is flagged
when the contract says something else.

The result is stored on the message (``metadata["citations"]``) and pushed live
as a ``citations`` stream event. The text itself is never edited: the web UI
turns tokens into numbered chips, while channels and the CLI — which cannot
render chips — use :class:`CitationRenderer` / :func:`render_plain` for "[1]"
markers and a "Sources:" footer. All of them number tokens with
:func:`app.documents.cite.number_tokens`, so "[2]" is the same source everywhere.

Everything here is synchronous (the registry and the index are both sync
stores) and is called off the event loop; :class:`CitationRenderer` wraps its
work in ``asyncio.to_thread`` itself.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from app.documents import quotes as q
from app.documents.cite import (
    TOKEN_RE,
    IssuedCitation,
    canonical_token,
    locator_label,
    mentions_citation,
    number_tokens,
    parse_tokens,
)
from app.utils.logger import logger

VERIFIED = "verified"
VERIFIED_ELSEWHERE = "verified_elsewhere"
UNISSUED = "unissued"
STALE = "stale"
REMOVED = "removed"
INVALID = "invalid"

# Statuses a reader may take at face value. Everything else counts towards
# ``unverified`` — as does a quote that does not match its source.
TRUSTED = frozenset({VERIFIED, VERIFIED_ELSEWHERE})

SNIPPET_MAX = 800
LABEL_MAX = 256
# An answer citing more distinct sources than this is not one a reader will
# check source by source; the rest are reported as invalid without lookups,
# which bounds the work finalization does before the message is saved.
MAX_TOKENS = 300
# Quotes shorter than this are not checked: too short to tell a quote from a
# phrase, and fuzzy matching has nothing to anchor on.
MIN_QUOTE_CHARS = 12
# How far before a token a quote may end: room for '", ' and the like.
QUOTE_GAP = 3
_QUOTE_LOOKBACK = 4000
_QUOTE_PAIRS = {"”": "“", '"': '"', "»": "«"}

_GONE_FILE_STATUSES = frozenset({"tombstone", "missing"})

# Rows issued before their conversation existed, by (profile, context id):
# the A2A executor creates a first message's conversation only after the turn,
# so its tools could not name it. Bound at finalization; process-local and
# bounded, because at worst an unbound row reads as "verified_elsewhere".
_PENDING_MAX = 256
_pending: dict[tuple[str, str], list[str]] = {}
_pending_lock = threading.Lock()


# ── issuing ────────────────────────────────────────────────────────────────


def issue(profile: str, context_id: str | None, citations: list[IssuedCitation]) -> None:
    """Record every token a Documentation Search tool printed in the conversation
    identified by ``context_id`` (the ``_context_id`` the tool adapter
    injects). Synchronous — tools call it from their worker thread. Never
    raises into the tool: a lost registration only downgrades a citation to
    "unissued" later, while a raised one would lose the tool's whole answer.
    """
    if not profile or not citations:
        return
    try:
        rows = [r for r in (_issued_row(c) for c in citations) if r is not None]
        if not rows:
            return
        from app.storage.documents_citations_storage import get_documents_citations_storage

        storage = get_documents_citations_storage()
        conversation_id = storage.resolve_conversation(profile, context_id) if context_id else None
        written = storage.issue(profile, conversation_id, rows)
        if conversation_id is None and context_id and written:
            with _pending_lock:
                _pending.setdefault((profile, context_id), []).extend(written)
                while len(_pending) > _PENDING_MAX:
                    _pending.pop(next(iter(_pending)))
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.warning(f"[documents] could not register {len(citations)} citation(s) for {profile}: {exc}")


def _issued_row(c: IssuedCitation) -> dict[str, Any] | None:
    m = TOKEN_RE.fullmatch(c.token or "")
    if m is None or m.group(1) != c.cite_id:
        # Only canonical tokens are registered: the registry is looked up by
        # the canonical form the parser produces.
        return None
    return {
        "token": c.token,
        "cite_id": c.cite_id,
        "target": c.target if c.target in ("file", "folder") else "file",
        "ref_id": c.ref_id,
        "text_hash": (c.text_hash or None) and str(c.text_hash)[:32],
        "source_kind": (c.source_kind or "local")[:8],
        "locator": dict(c.locator or {}),
        "label": (c.label or "")[:LABEL_MAX],
        "rel_path": c.rel_path or "",
        "snippet": (c.snippet or "")[:SNIPPET_MAX],
        "leaf": (c.leaf or "")[:16],
        "web_link": c.web_link or None,
    }


def _bind_pending(profile: str, context_id: str, conversation_id: str) -> None:
    with _pending_lock:
        ids = _pending.pop((profile, context_id), None)
    if ids:
        from app.storage.documents_citations_storage import get_documents_citations_storage

        get_documents_citations_storage().bind_conversation(profile, ids, conversation_id)


def purge_profile(profile: str) -> int:
    """Purge set P: forget every citation the profile was issued. Answers keep
    their own ``metadata.citations`` snapshots."""
    from app.storage.documents_citations_storage import get_documents_citations_storage

    with _pending_lock:
        for key in [k for k in _pending if k[0] == profile]:
            _pending.pop(key, None)
    return get_documents_citations_storage().delete_profile(profile)


# ── the profile's index ────────────────────────────────────────────────────


@contextmanager
def profile_index(profile: str) -> Iterator[Any]:
    """The profile's index (an :class:`~app.documents.index.IndexDB`) for
    reading, or None when it has none.

    Goes through the running engine, which owns the file (one writer per
    file per process), and only when the file already exists — a read must
    never be what creates an index. A process without an engine (tests, a
    server whose engine failed to start) opens the file just for this read.
    """
    from app.storage.documents_storage import get_documents_storage
    from app.documents.index import index_path
    from app.documents.service import get_service

    svc = get_service()
    rt = svc.runtime(profile) if svc is not None else None
    if rt is not None and rt.db is not None and not rt.db.closed:
        yield rt.db
        return
    uid = rt.uid if rt is not None else get_documents_storage().profile_uid(profile)
    if not uid or not os.path.exists(index_path(uid)):
        yield None
        return
    if svc is not None:
        rt = svc.runtime(profile, create=True)
        yield rt.ensure_db() if rt is not None else None
        return
    from app.documents.index import IndexDB, IndexDBError

    try:
        db = IndexDB.open(index_path(uid), profile_uid=uid, quick_check=False)
    except IndexDBError as exc:
        logger.warning(f"[documents] {profile}: index unreadable for citations ({exc})")
        yield None
        return
    try:
        yield db
    finally:
        db.close()


# ── resolving tokens ───────────────────────────────────────────────────────


class _Resolved:
    """One token's item plus, for a chunk that still exists, the texts a quote
    placed before it is checked against."""

    __slots__ = ("item", "candidates")

    def __init__(self, item: dict[str, Any], candidates: list[tuple[str | None, str]] | None = None):
        self.item = item
        self.candidates = candidates or []


def resolve_tokens(profile: str, conversation_id: str | None, tokens: Iterable[str]) -> dict[str, dict[str, Any]]:
    """``{token: item}`` for each token as given (tolerant spellings accepted),
    items shaped like ``metadata.citations.items`` with ``n`` and
    ``quote_status`` left None — the lookup behind the resolve API."""
    wanted = list(dict.fromkeys(str(t) for t in tokens if t))
    canon: dict[str, str | None] = {}
    for raw in wanted:
        parsed = parse_tokens(raw)
        canon[raw] = parsed[0]["token"] if len(parsed) == 1 else None
    resolved = _resolve(profile, conversation_id, [c for c in canon.values() if c], want_text=False)
    out: dict[str, dict[str, Any]] = {}
    for raw, c in canon.items():
        item = dict(resolved[c].item) if c else _invalid_item(raw)
        item.setdefault("n", None)
        item.setdefault("quote_status", None)
        out[raw] = item
    return out


def _resolve(
    profile: str, conversation_id: str | None, tokens: list[str], *, want_text: bool,
) -> dict[str, _Resolved]:
    """Resolve canonical tokens against the registry and the index."""
    from app.storage.documents_citations_storage import get_documents_citations_storage

    out: dict[str, _Resolved] = {}
    strict: dict[str, tuple[str, str | None]] = {}
    for i, token in enumerate(dict.fromkeys(tokens)):
        m = TOKEN_RE.fullmatch(canonical_token(token) or "")
        if m is None or i >= MAX_TOKENS:
            out[token] = _Resolved(_invalid_item(token))
        else:
            strict[token] = (m.group(1), m.group(2))
    if not strict:
        return out

    registry: dict[str, list[dict[str, Any]]] = {}
    for row in get_documents_citations_storage().rows_for_cite_ids(profile, {c for c, _ in strict.values()}):
        # Keyed by the canonical form: a row issued before the rename still
        # holds its "[ud:…]" token, and it is the same citation.
        registry.setdefault(canonical_token(row["token"]) or row["token"], []).append(row)

    with profile_index(profile) as db:
        files: dict[str, tuple[str, dict[str, Any] | None]] = {}
        chunk_lists: dict[tuple[str, int], list[Any]] = {}
        hidden = _hidden_sources(db)
        for token, (cite_id, c8) in strict.items():
            rows = registry.get(token, [])
            here = [r for r in rows if conversation_id and r.get("conversation_id") == conversation_id]
            snap = (here or rows[-1:] or [None])[0]
            issued_status = VERIFIED if here else (VERIFIED_ELSEWHERE if rows else UNISSUED)

            if cite_id not in files:
                files[cite_id] = _lookup_target(db, cite_id)
            target, rec = files[cite_id]

            if rec is None or _gone(target, rec):
                if rows or rec is not None:
                    out[token] = _Resolved(_snapshot_item(token, REMOVED, snap, rec, target))
                else:
                    out[token] = _Resolved(_invalid_item(token))
                continue
            if (rec.get("source") or "local") in hidden:
                # A Drive whose access was revoked or unlinked is held: show
                # what was cited (the snapshot taken when it was), never the
                # live index, until Google is re-linked.
                out[token] = _Resolved(_snapshot_item(token, STALE, snap, rec, target))
                continue

            if not c8:
                out[token] = _Resolved(_item(token, issued_status, target, rec, snap, None))
                continue

            key = (target, int(rec["id"]))
            if key not in chunk_lists:
                chunk_lists[key] = _chunk_list(db, target, int(rec["id"]))
            found = _find_chunk(chunk_lists[key], c8)
            if found is None:
                if rows:
                    out[token] = _Resolved(_snapshot_item(token, STALE, snap, rec, target))
                else:
                    out[token] = _Resolved(_invalid_item(token))
                continue
            idx, old = found
            body = chunk_lists[key]
            # Neighbours in reading order, body chunks only (a card is not
            # text a quote can run into).
            around = [
                j for j in (idx - 1, idx + 1)
                if want_text and old.ordinal >= 0 and 0 <= j < len(body) and body[j].ordinal >= 0
            ]
            full = {r["id"]: r for r in db.chunk_rows([old.id, *(body[j].id for j in around)])}
            chunk = full.get(old.id)
            if chunk is None:  # deleted between the two reads
                out[token] = _Resolved(
                    _snapshot_item(token, STALE, snap, rec, target) if rows else _invalid_item(token)
                )
                continue
            item = _item(token, issued_status, target, rec, snap, chunk)
            candidates: list[tuple[str | None, str]] = []
            if want_text:
                # The cited chunk alone first (a hit there gives the tightest
                # span), then joined with its neighbours for a quote that
                # runs across a chunk boundary.
                candidates.append((chunk.get("text_hash"), chunk.get("text") or ""))
                if around:
                    joined = [full.get(body[j].id) for j in around if j < idx]
                    joined += [chunk] + [full.get(body[j].id) for j in around if j > idx]
                    candidates.append((
                        chunk.get("text_hash"),
                        "\n".join((r or {}).get("text") or "" for r in joined if r is not None),
                    ))
            out[token] = _Resolved(item, candidates)
    return out


def _hidden_sources(db: Any) -> frozenset[str]:
    if db is None:
        return frozenset()
    from app.documents.query.filters import hidden_sources

    return hidden_sources(db)


def _lookup_target(db: Any, cite_id: str) -> tuple[str, dict[str, Any] | None]:
    if db is None:
        return "file", None
    rec = db.file_by_cite(cite_id)
    if rec is not None:
        return "file", rec
    rec = db.folder_by_cite(cite_id)
    if rec is not None:
        return "folder", rec
    return "file", None


def _gone(target: str, rec: dict[str, Any]) -> bool:
    if target == "file":
        return rec.get("status") in _GONE_FILE_STATUSES
    return rec.get("status") not in (None, "live")


def _chunk_list(db: Any, target: str, ref_id: int) -> list[Any]:
    """The owner's chunks in reading order (id, hash, ordinal, locator): body
    chunks first-class, the card (ordinal -1) last so a prefix lookup prefers
    real text when two hashes share their first 8 hex digits."""
    chunks = db.get_chunks(ref_id) if target == "file" else db.folder_chunks(ref_id)
    body = [c for c in chunks if c.ordinal >= 0]
    return body + [c for c in chunks if c.ordinal < 0]


def _find_chunk(chunks: list[Any], c8: str) -> tuple[int, Any] | None:
    for i, c in enumerate(chunks):
        if (c.text_hash or "").startswith(c8):
            return i, c
    return None


def _file_block(target: str, rec: dict[str, Any] | None, snap: dict[str, Any] | None) -> dict[str, Any] | None:
    if rec is not None:
        if target == "folder":
            return {
                "fid": rec.get("cite_id"), "name": rec.get("name") or _basename(rec.get("rel_path")),
                "rel_path": rec.get("rel_path") or "", "source": rec.get("source"), "kind": "folder",
                "web_link": None,
            }
        return {
            "fid": rec.get("cite_id"), "name": rec.get("name") or _basename(rec.get("rel_path")),
            "rel_path": rec.get("rel_path") or "", "source": rec.get("source"), "kind": rec.get("kind"),
            "web_link": rec.get("drive_web_link"),
        }
    if snap is not None:
        return {
            "fid": snap.get("cite_id"), "name": _basename(snap.get("rel_path")),
            "rel_path": snap.get("rel_path") or "", "source": snap.get("source_kind"),
            "kind": "folder" if snap.get("target") == "folder" else None,
            "web_link": snap.get("web_link"),
        }
    return None


def _item(
    token: str, status: str, target: str, rec: dict[str, Any],
    snap: dict[str, Any] | None, chunk: dict[str, Any] | None,
) -> dict[str, Any]:
    locator = dict((chunk or {}).get("locator") or {}) if chunk is not None else dict((snap or {}).get("locator") or {})
    label = locator_label(locator) or (snap or {}).get("label") or ""
    snippet = (snap or {}).get("snippet") or ""
    if not snippet and chunk is not None:
        snippet = (chunk.get("text") or "")[:SNIPPET_MAX]
    return {
        "token": token, "status": status, "quote_status": None,
        "file": _file_block(target, rec, snap),
        "locator": locator, "locator_label": label, "snippet": snippet,
    }


def _snapshot_item(
    token: str, status: str, snap: dict[str, Any] | None, rec: dict[str, Any] | None, target: str,
) -> dict[str, Any]:
    """An item built from what the registry saw when the token was issued."""
    locator = dict((snap or {}).get("locator") or {})
    return {
        "token": token, "status": status, "quote_status": None,
        # The index row while it lingers (a tombstone still knows its name),
        # else the snapshot.
        "file": _file_block(target, rec, snap),
        "locator": locator,
        "locator_label": (snap or {}).get("label") or locator_label(locator),
        "snippet": (snap or {}).get("snippet") or "",
    }


def _invalid_item(token: str) -> dict[str, Any]:
    return {
        "token": token, "status": INVALID, "quote_status": None, "file": None,
        "locator": {}, "locator_label": "", "snippet": "",
    }


def _basename(rel_path: Any) -> str:
    return str(rel_path or "").rstrip("/").rsplit("/", 1)[-1]


def _folder_of(rel_path: Any) -> str:
    rel = str(rel_path or "").rstrip("/")
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


# ── finalization ───────────────────────────────────────────────────────────


def finalize_citations(
    profile: str, conversation_id: str | None, text: str, *, context_id: str | None = None,
) -> dict[str, Any] | None:
    """Verify the tokens in a finished answer; the ``metadata["citations"]``
    object for its message, or None when it cites nothing (or verification
    failed — the message is then saved without it, never held back).

    ``context_id`` is passed by the A2A path, whose first message's tools ran
    before the conversation existed: their registrations are bound here.
    """
    if not mentions_citation(text):
        return None
    try:
        if context_id and conversation_id:
            _bind_pending(profile, context_id, conversation_id)
        numbers = number_tokens(text)
        if not numbers:
            return None
        quotes = _quotes_by_token(text)
        resolved = _resolve(profile, conversation_id, list(numbers), want_text=bool(quotes))
        items: list[dict[str, Any]] = []
        for token, n in numbers.items():
            r = resolved.get(token) or _Resolved(_invalid_item(token))
            item = dict(r.item)
            item["n"] = n
            statuses = [
                q.verify_quote(quote, r.candidates).status
                for quote in quotes.get(token, ())
                if r.candidates
            ]
            item["quote_status"] = q.worst(statuses)
            items.append(item)
        unverified = sum(
            1 for i in items if i["status"] not in TRUSTED or i.get("quote_status") == q.MISMATCH
        )
        return {"v": 1, "items": items, "unverified": unverified}
    except Exception:  # noqa: BLE001
        logger.exception(f"[documents] citation finalization failed for {profile}/{conversation_id}")
        return None


def _quotes_by_token(text: str) -> dict[str, list[str]]:
    """Quoted spans (“…”, "…", «…») of at least :data:`MIN_QUOTE_CHARS` that
    end at most :data:`QUOTE_GAP` characters before a token — or before a run
    of adjacent tokens, which then all share the quote."""
    parsed = parse_tokens(text)
    spans: list[tuple[int, int, list[str]]] = []
    for p in parsed:
        if spans and spans[-1][0] == p["start"]:
            spans[-1][2].append(p["token"])
        else:
            spans.append((p["start"], p["end"], [p["token"]]))
    out: dict[str, list[str]] = {}
    i = 0
    while i < len(spans):
        # A group: this span and the ones directly after it ("[1][2]", "[1], [2]").
        j = i
        tokens = list(spans[i][2])
        while j + 1 < len(spans) and _is_gap(text[spans[j][1]:spans[j + 1][0]]):
            j += 1
            tokens += spans[j][2]
        quote = _quote_before(text, spans[i][0])
        if quote:
            for t in dict.fromkeys(tokens):
                out.setdefault(t, []).append(quote)
        i = j + 1
    return out


def _is_gap(s: str) -> bool:
    return len(s) <= QUOTE_GAP and not any(c.isalnum() for c in s)


def _quote_before(text: str, pos: int) -> str | None:
    for back in range(0, QUOTE_GAP + 1):
        k = pos - 1 - back
        if k < 0:
            return None
        ch = text[k]
        if ch in _QUOTE_PAIRS:
            if not _is_gap(text[k + 1:pos]):
                return None
            opener = _QUOTE_PAIRS[ch]
            o = text.rfind(opener, max(0, k - _QUOTE_LOOKBACK), k)
            if o < 0:
                return None
            content = text[o + 1:k]
            # A token inside means the marks paired across two quotations.
            if mentions_citation(content) or len(content.strip()) < MIN_QUOTE_CHARS:
                return None
            return content
        if ch.isalnum():
            return None
    return None


# ── plain-text rendering (channels, CLI) ───────────────────────────────────


class CitationRenderer:
    """Renders one turn's answer for a surface that cannot show chips.

    Created per turn (a channel's reply forwarder makes one per run), because
    the numbering is per answer: an interim reply sent at a flow break and the
    final answer are halves of one message, so "[2]" in the second half must
    be the "[2]" of the first. Numbers are taken in order of first appearance
    across everything rendered so far — exactly
    :func:`~app.documents.cite.number_tokens` over the whole persisted text,
    which is what the web UI numbers by.

    ``include_paths=False`` (group rooms) leaves folder paths and links out of
    the footer: other people read it.
    """

    def __init__(self, profile: str, conversation_id: str | None, *, include_paths: bool = True) -> None:
        self.profile = profile
        self.conversation_id = conversation_id
        self.include_paths = include_paths
        self._numbers: dict[str, int] = {}
        self._items: dict[str, dict[str, Any]] = {}

    async def render(self, text: str) -> str:
        """``text`` with tokens as "[n]" and a "Sources:" footer. Returns the
        text unchanged when it cites nothing or anything goes wrong — a reply
        is never lost to its citations."""
        if not mentions_citation(text):
            return text
        try:
            return await asyncio.to_thread(self.render_sync, text)
        except Exception:  # noqa: BLE001
            logger.exception(f"[documents] citation rendering failed for {self.profile}/{self.conversation_id}")
            return text

    async def render_step(self, text: str) -> str:
        """A reasoning step's text (tool output included) with each token as
        the name of what it cites. Steps take no numbers: the numbering
        belongs to the answer, and a step citing a source the answer never
        uses would shift every number after it."""
        if not mentions_citation(text):
            return text
        try:
            return await asyncio.to_thread(self.render_step_sync, text)
        except Exception:  # noqa: BLE001
            logger.exception(f"[documents] citation rendering failed for {self.profile}/{self.conversation_id}")
            return text

    def render_sync(self, text: str) -> str:
        parsed = parse_tokens(text)
        if not parsed:
            return text
        for p in parsed:
            self._numbers.setdefault(p["token"], len(self._numbers) + 1)
        self._ensure([p["token"] for p in parsed])
        body = _rewrite(text, parsed, lambda toks: "".join(f"[{self._numbers[t]}]" for t in toks))
        cited = sorted(dict.fromkeys(p["token"] for p in parsed), key=lambda t: self._numbers[t])
        footer = "\n".join(self._footer_line(t) for t in cited)
        return f"{body.rstrip()}\n\nSources:\n{footer}"

    def render_step_sync(self, text: str) -> str:
        parsed = parse_tokens(text)
        if not parsed:
            return text
        self._ensure([p["token"] for p in parsed])
        return _rewrite(text, parsed, lambda toks: "".join(f"[{self._short(t)}]" for t in toks))

    def _ensure(self, tokens: list[str]) -> None:
        missing = [t for t in dict.fromkeys(tokens) if t not in self._items]
        if not missing:
            return
        resolved = _resolve(self.profile, self.conversation_id, missing, want_text=False)
        for t in missing:
            self._items[t] = (resolved.get(t) or _Resolved(_invalid_item(t))).item

    def _short(self, token: str) -> str:
        item = self._items.get(token) or {}
        name = (item.get("file") or {}).get("name") or "source"
        label = item.get("locator_label")
        return f"{name} · {label}" if label else name

    def _footer_line(self, token: str) -> str:
        n = self._numbers[token]
        item = self._items.get(token) or {}
        status = item.get("status")
        if status == INVALID:
            return f"[{n}] unknown source (not verified)"
        f = item.get("file") or {}
        parts = [f.get("name") or "source"]
        if item.get("locator_label"):
            parts.append(item["locator_label"])
        if self.include_paths:
            folder = _folder_of(f.get("rel_path"))
            if folder:
                parts.append(folder)
            if f.get("web_link"):
                parts.append(str(f["web_link"]))
        line = f"[{n}] " + " · ".join(parts)
        if status == REMOVED:
            line += " (no longer available)"
        elif status in (UNISSUED, STALE):
            line += " (unverified)"
        return line


def _rewrite(text: str, parsed: list[dict[str, Any]], fmt) -> str:
    """Replace each bracket the parser matched (one bracket may hold several
    tokens) with ``fmt(tokens)``, back to front so offsets stay valid."""
    groups: dict[tuple[int, int], list[str]] = {}
    for p in parsed:
        toks = groups.setdefault((p["start"], p["end"]), [])
        if p["token"] not in toks:
            toks.append(p["token"])
    out = text
    for (start, end), toks in sorted(groups.items(), reverse=True):
        out = out[:start] + fmt(toks) + out[end:]
    return out


def render_plain(
    profile: str, conversation_id: str | None, text: str, *, include_paths: bool = True,
) -> str:
    """``text`` with tokens replaced by "[n]" and a "Sources:" footer — for a
    surface that shows plain text once (a room fan-out, an export). Returns
    the text unchanged when it cites nothing or rendering fails."""
    if not mentions_citation(text):
        return text
    try:
        return CitationRenderer(profile, conversation_id, include_paths=include_paths).render_sync(text)
    except Exception:  # noqa: BLE001
        logger.exception(f"[documents] plain citation rendering failed for {profile}/{conversation_id}")
        return text


__all__ = [
    "CitationRenderer",
    "INVALID",
    "REMOVED",
    "STALE",
    "TRUSTED",
    "UNISSUED",
    "VERIFIED",
    "VERIFIED_ELSEWHERE",
    "finalize_citations",
    "issue",
    "profile_index",
    "purge_profile",
    "render_plain",
    "resolve_tokens",
]
