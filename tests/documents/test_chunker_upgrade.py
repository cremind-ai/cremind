"""The chunker fix reaches documents that are already indexed.

A new ``CHUNKER_VERSION`` alone queues nothing: a file is re-chunked only
when something else sends it through the pipeline, so the reported decree —
indexed before the wrapped-reference fix — would keep Article 24 cut in two
until the user re-uploaded it. ``ProfileRuntime.queue_stale_chunking`` queues
such files once, when a source becomes active:

- only indexed rows with an older version — a queued file keeps its place,
  time and retries, a failed one its backoff; images never;
- after a bump that changed only the legal overlay, only files with articles;
- per source (the folder, and Drive after a sync), idempotent;
- the pipeline's chunk diff then keeps the file's citation id and replaces
  only the chunks that changed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.documents.chunking import CHUNKER_VERSION, LEGAL_ONLY_BUMPS, chunk_blocks, diff_chunks, make_file_card
from app.documents.chunking import legal as legal_overlay
from app.documents.runtime import P_UPGRADE, ProfileRuntime
from app.documents.types import Block
from tests.documents._foreign_worker_regs import DECREE_EN, DECREE_VI, FOOD_REG, NEWS_NOTE
from tests.documents.test_research_analyze import Index


def _rt(ix: Index, woke: list) -> ProfileRuntime:
    rt = ProfileRuntime(SimpleNamespace(wake=lambda: woke.append(1)), "alice", "uid-alice")
    rt.db = ix.db
    return rt


@pytest.fixture
def ix(tmp_path):
    ix = Index(tmp_path / "up.db", "uid-alice")
    yield ix
    ix.db.close()


def _age(ix: Index, rel: str, **fields) -> int:
    fid = int(ix.rows[rel]["id"])
    ix.db.update_file(fid, chunker_version=1, **fields)
    return fid


def test_old_legal_files_are_queued_once_and_nothing_else_is_touched(ix):
    assert CHUNKER_VERSION == 2 and 2 in LEGAL_ONLY_BUMPS
    ix.add("Regs/decree.txt", DECREE_EN)
    ix.add("Regs/food.txt", FOOD_REG)
    ix.add("Notes/tips.txt", NEWS_NOTE)
    ix.add("Regs/current.txt", DECREE_VI)
    stale = _age(ix, "Regs/decree.txt")
    notes = _age(ix, "Notes/tips.txt")  # old, but no article in it: cut the same today
    waiting = _age(ix, "Regs/food.txt")
    ix.db.mark_dirty([waiting], priority=1)
    ix.db.update_file(waiting, attempts=2)
    before = ix.db.get_file(waiting)
    failed = _age(ix, "Regs/current.txt")
    ix.db.update_file(failed, status="error", attempts=3, next_attempt_at=9e12)
    photo = ix.add("Photos/p.jpg", ["a photo"])
    ix.db.update_file(int(photo["id"]), kind="image", chunker_version=1)
    woke: list = []
    rt = _rt(ix, woke)

    assert rt.queue_stale_chunking("local") == 1
    row = ix.db.get_file(stale)
    assert row["status"] == "dirty" and row["priority"] == P_UPGRADE and woke
    assert ix.db.get_file(notes)["status"] == "indexed"
    after = ix.db.get_file(waiting)
    assert (after["status"], after["queued_at"], after["attempts"], after["priority"]) == (
        "dirty", before["queued_at"], 2, 1)
    assert ix.db.get_file(failed)["status"] == "error" and ix.db.get_file(failed)["attempts"] == 3
    assert ix.db.get_file(int(photo["id"]))["status"] == "indexed"
    # Idempotent: nothing is queued twice.
    assert rt.queue_stale_chunking("local") == 0
    # Another source's files are that source's business.
    assert rt.queue_stale_chunking("drive") == 0


def test_re_chunking_keeps_the_citation_id_and_replaces_only_what_changed(ix, monkeypatch):
    blocks = [Block(text=x, locator={"line_start": i + 1, "line_end": i + 1}) for i, x in enumerate(DECREE_VI)]
    # What the version-1 overlay made of the decree: the wrapped reference
    # read as a new "Điều 23".
    with monkeypatch.context() as m:
        m.setattr(legal_overlay, "_continues_reference", lambda rest, prev: False)
        old = chunk_blocks(blocks, legal=True)
    new = chunk_blocks(blocks, legal=True)
    assert [c.section_key for c in old] != [c.section_key for c in new]
    row = ix.add("Regs/nd219.txt", DECREE_VI)
    fid = int(row["id"])
    card = make_file_card(name="nd219.txt", rel_path="Regs/nd219.txt", kind="text", size=1, mtime_iso="2026-09-20",
                          summary_text=DECREE_VI[0])
    ix.db.apply_chunks(file_id=fid, folder_id=row.get("folder_id"), source="local",
                       diff=diff_chunks(ix.db.get_chunks(fid), [card] + old), file_fields={"chunker_version": 1})
    cite = ix.db.get_file(fid)["cite_id"]
    rt = _rt(ix, [])
    assert rt.queue_stale_chunking("local") == 1

    # The pipeline's write: the same diff every indexing run uses.
    diff = diff_chunks(ix.db.get_chunks(fid), [card] + new)
    # The two pieces Article 24 was cut into become one; everything else —
    # and its embeddings — stays.
    assert 0 < len(diff.add) <= 2 and 0 < len(diff.remove) <= 3
    assert len(diff.keep) >= len(new) - len(diff.add)
    ix.db.apply_chunks(file_id=fid, folder_id=row.get("folder_id"), source="local", diff=diff,
                       file_fields={"chunker_version": CHUNKER_VERSION, "status": "indexed"})
    after = ix.db.get_file(fid)
    assert after["cite_id"] == cite and after["chunker_version"] == CHUNKER_VERSION
    art24 = " ".join(c["text"] for c in ix.db.chunks_of_file(fid) if str(c.get("section_key")).startswith("art:24"))
    assert "Điều 23 Nghị định này." in art24 and "4. Giấy phép lao động còn thời hạn" in art24
    assert rt.queue_stale_chunking("local") == 0
