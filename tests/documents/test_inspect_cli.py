"""`cremind docs inspect FILE` — what the index holds for one file.

The command imports its client functions inside the body, so the client is
patched in ``app.cli.client.docs`` and nothing reaches the network. What is
pinned: a file id, a citation token (either prefix) and a path (made absolute,
then looked up) all reach the same preview; each lookup state that is not
"indexed" exits 1 with its own message; the summary of a scanned PDF waiting
for OCR says so and where to fix it; `--text` prints the first page of
passages and how to get the rest; `--all` pages to the end and starts over
once when the file is re-indexed meanwhile; `--json` prints data only.
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

FID = "k7m2xq9a"


def _summary(**over) -> dict:
    out = {
        "state": "complete", "phase": "indexed", "badge": "indexed",
        "headline": "Indexed: 3 passages of text.",
        "readable": {"passages": 3, "chars": 1200},
        "segments": {"text": 3, "ocr": 0, "image_description": 0, "metadata": 1},
        "chars": {"text": 1200, "ocr": 0, "image_description": 0, "metadata": 80},
        "indexed_at": 1790000000000.0, "pages": None, "reasons": [],
        "embedding": {"state": "ready", "ready": 4, "total": 4},
        "refresh_queued": False, "previewable": True, "revision": "rev1",
    }
    out.update(over)
    return out


def _seg(index: int, text: str, **over) -> dict:
    seg = {"token": f"[doc:{FID}#{index:08x}]", "index": index, "ordinal": index, "type": "text",
           "heading": "", "locator": {}, "locator_label": f"p. {index + 1}", "text": text}
    seg.update(over)
    return seg


def _page(segments=(), *, next_cursor=None, summary=None, revision="rev1", total=3, start=0) -> dict:
    return {
        "fid": FID, "name": "report.pdf", "rel_path": "Reports/report.pdf", "kind": "pdf", "source": "local",
        "status": "indexed", "summary": summary or _summary(revision=revision),
        "metadata": {"card": "File: report.pdf\nPath: Reports/report.pdf", "document": {"pages": 3}},
        "revision": revision, "total_segments": total, "start_index": start,
        "segments": list(segments), "next_cursor": next_cursor,
    }


def _stale(revision="rev2"):
    from app.cli.client._base import APIError

    raw = json.dumps({"error": "StalePreview", "message": "The file was re-indexed since this page was loaded; "
                      "reload the preview.", "revision": revision}).encode()
    return APIError(409, "StalePreview", raw)


@pytest.fixture
def api(monkeypatch):
    """Scripted client. ``previews`` maps a cursor (None = first page) to an
    answer, or to a list of answers used in turn; an exception is raised.
    Every call is recorded."""
    import app.cli.client.docs as c

    state: dict = {"calls": [], "lookup": None, "previews": {None: _page()}}

    async def lookup_paths(client, paths):
        state["calls"].append(("lookup", list(paths)))
        return state["lookup"]

    async def file_preview(client, fid, *, cursor=None, limit=None):
        state["calls"].append(("preview", fid, cursor, limit))
        answer = state["previews"][cursor]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(c, "lookup_paths", lookup_paths)
    monkeypatch.setattr(c, "file_preview", file_preview)
    return state


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


def _previews(state) -> list:
    return [call for call in state["calls"] if call[0] == "preview"]


# ── what FILE may be ───────────────────────────────────────────────────────


@pytest.mark.parametrize("arg", [FID, FID.upper(), f"[doc:{FID}]", f"[doc:{FID}#1a2b3c4d]", f"doc:{FID}",
                                 f"[ud:{FID}]", f"[DOC:{FID.upper()}#1A2B3C4D]"])
def test_a_file_id_or_a_citation_token_goes_straight_to_the_preview(api, arg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _run("docs", "inspect", arg)
    assert result.exit_code == 0, result.output
    assert api["calls"] == [("preview", FID, None, 1)], "no lookup, one summary-sized page"
    assert result.stdout.startswith("report.pdf · Indexed content\n")


def test_a_relative_path_is_made_absolute_and_looked_up(api, tmp_path, monkeypatch):
    (tmp_path / "Reports").mkdir()
    (tmp_path / "Reports" / "report.pdf").write_bytes(b"%PDF")
    monkeypatch.chdir(tmp_path)
    expected = os.path.abspath(os.path.join("Reports", "report.pdf"))
    api["lookup"] = {"enabled": True, "root": str(tmp_path), "available": True, "items": {
        expected: {"state": "indexed", "fid": FID.upper(), "name": "report.pdf", "rel_path": "Reports/report.pdf",
                   "kind": "pdf", "status": "indexed", "summary": _summary()}}}
    result = _run("docs", "inspect", os.path.join("Reports", "report.pdf"))
    assert result.exit_code == 0, result.output
    assert api["calls"] == [("lookup", [expected]), ("preview", FID, None, 1)]
    assert f"path:      {expected}" in result.stdout


def test_a_file_named_like_an_id_is_a_path(api, tmp_path, monkeypatch):
    (tmp_path / FID).write_text("x")
    monkeypatch.chdir(tmp_path)
    path = str(tmp_path / FID)
    api["lookup"] = {"enabled": True, "root": str(tmp_path), "items": {path: {"state": "indexed", "fid": FID}}}
    result = _run("docs", "inspect", FID)
    assert result.exit_code == 0, result.output
    assert api["calls"][0] == ("lookup", [path])


def test_a_folder_is_refused_before_asking_the_server(api, tmp_path):
    result = _run("docs", "inspect", str(tmp_path))
    assert result.exit_code == 1
    assert "is a folder" in result.stderr
    assert api["calls"] == []


# ── lookup states that are not "indexed" ───────────────────────────────────


@pytest.mark.parametrize("state, reason, expected", [
    ("outside", "outside_root", "Not in the indexed folder"),
    ("outside", "foreign", "another profile's working directory"),
    ("outside", "system", "Cremind's system folder"),
    ("excluded", "excluded", "Excluded from indexing"),
    ("unmatched", "not_indexed", "Not indexed yet"),
    ("unmatched", "root_unavailable", "not available right now"),
    ("gone", "tombstone", "Removed from the index"),
    ("gone", "missing", "disappeared from the folder"),
])
def test_each_lookup_failure_says_why_and_exits_1(api, tmp_path, state, reason, expected):
    path = str(tmp_path / "x.pdf")
    api["lookup"] = {"enabled": True, "root": "/root/Documents", "available": True,
                     "items": {path: {"state": state, "reason": reason}}}
    result = _run("docs", "inspect", path)
    assert result.exit_code == 1
    assert expected in result.stderr and path in result.stderr
    assert result.stdout == ""
    assert not _previews(api), "nothing to preview"


def test_outside_names_the_indexed_folder(api, tmp_path):
    path = str(tmp_path / "x.pdf")
    api["lookup"] = {"enabled": True, "root": "/root/Documents", "items": {path: {"state": "outside",
                                                                             "reason": "outside_root"}}}
    result = _run("docs", "inspect", path)
    assert "/root/Documents, your working directory" in result.stderr


def test_the_local_source_off_says_so(api, tmp_path):
    api["lookup"] = {"enabled": False, "root": None, "items": {}}
    result = _run("docs", "inspect", str(tmp_path / "x.pdf"))
    assert result.exit_code == 1
    assert "Search my documents is off for this profile" in result.stderr
    assert "cremind docs enable" in result.stderr


def test_a_lookup_failure_with_json_is_data_on_stdout(api, tmp_path):
    path = str(tmp_path / "x.pdf")
    api["lookup"] = {"enabled": True, "root": "/r", "items": {path: {"state": "excluded", "reason": "excluded"}}}
    result = _run("--json", "docs", "inspect", path)
    assert result.exit_code == 1
    out = json.loads(result.stdout)
    assert out["state"] == "excluded" and out["path"] == path and "Excluded from indexing" in out["message"]


def test_an_unknown_file_id_exits_1_with_the_servers_message(api):
    from app.cli.client._base import APIError

    raw = json.dumps({"error": "NotFound", "message": "No such file."}).encode()
    api["previews"] = {None: APIError(404, "NotFound: No such file.", raw)}
    result = _run("docs", "inspect", FID)
    assert result.exit_code == 1
    assert f"{FID}: No such file." in result.stderr


# ── the summary ────────────────────────────────────────────────────────────


def _scanned_pdf_waiting_for_ocr() -> dict:
    """The real summary of an 8-page scanned PDF whose pages wait for a
    vision model: only its file card is stored."""
    from app.documents import content as C

    row = {"id": 1, "cite_id": FID, "kind": "pdf", "status": "indexed", "status_reason": None,
           "indexed_at": 1790000000.0, "doc_meta": {"extraction": C.coverage_record(
               pages=8, scanned=range(1, 9), ocr_reason="awaiting_vision")}}
    stats = C.ChunkStats(counts={"metadata": 1}, chars={"metadata": 90}, total=1, vectors=1)
    return C.summarize(row, stats, gen=1, revision="0f3a9c1d2b4e5f60")


def test_a_scanned_pdf_waiting_for_ocr_says_so_and_where_to_fix_it(api):
    summary = _scanned_pdf_waiting_for_ocr()
    assert summary["badge"] == "blocked"  # the contract this rendering relies on
    api["previews"] = {None: _page(summary=summary, total=0)}
    result = _run("docs", "inspect", FID)
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.startswith("report.pdf · Blocked\n")
    assert "Only file details were indexed. 8 pages are waiting for OCR" in out
    assert "readable:  0 passages · 0 characters" in out
    assert "segments:  0 text · 0 OCR · 0 image descriptions · 1 metadata" in out
    assert ("pages:     8 total · 0 native text · 8 scanned · 0 OCR done · 0 blank · 8 waiting for OCR · "
            "0 OCR failed · 0 unreadable") in out
    assert "embedding: ready · 1 of 1 chunk embedded" in out
    assert "revision:  0f3a9c1d2b4e5f60" in out
    assert f"id:        {FID} (cite it as [doc:{FID}])" in out
    # The reason, and where its action is fixed.
    assert "    - 8 pages are waiting for OCR: no Specialized Vision Model is chosen" in out
    assert "      fix: choose a Specialized Vision Model in Settings → LLM Providers" in out
    assert "--text" not in out and "stored text" not in out, "no passages without --text"


def test_each_action_points_to_its_fix(api):
    reasons = [
        {"code": "awaiting_consent", "message": "consent", "action": "vision_consent"},
        {"code": "ocr_failed", "message": "failed", "action": "retry", "pages": 2},
        {"code": "awaiting_extractor", "message": "reader", "action": "install"},
        {"code": "over_cap", "message": "quota"},
        {"code": "deferred", "message": "space", "action": "wait"},
    ]
    api["previews"] = {None: _page(summary=_summary(state="partial", badge="partial", reasons=reasons))}
    out = _run("docs", "inspect", FID).stdout
    assert "report.pdf · Partly indexed" in out
    assert "fix: allow sending images and scanned pages to the vision model" in out
    assert "cremind docs caption --consent-vision" in out
    assert f"fix: re-read it now: cremind docs reindex {FID}" in out
    assert "cremind features install documentation_search" in out
    assert out.count("fix:") == 3, "no fix line for a reason without an action (or 'wait')"


def test_embedding_pending_and_unavailable(api):
    api["previews"] = {None: _page(summary=_summary(embedding={"state": "pending", "ready": 0, "total": 4}))}
    assert "embedding: pending · 0 of 4 chunks embedded (the rest are being embedded" in \
        _run("docs", "inspect", FID).stdout
    api["previews"] = {None: _page(summary=_summary(embedding={"state": "unavailable", "ready": 0, "total": 4}))}
    assert "embedding: unavailable · no vector collection is active" in _run("docs", "inspect", FID).stdout


# ── --text ─────────────────────────────────────────────────────────────────


def test_text_prints_the_first_page_in_order_and_how_to_get_the_rest(api):
    first = [
        _seg(0, "Chapter one text.", heading="Chapter 1"),
        _seg(1, "Transcribed scan.", type="ocr", locator_label="p. 2"),
        _seg(2, "Two dogs in a park.", type="image_description", locator_label="", heading="Figure 1"),
    ]
    api["previews"] = {None: _page(first, next_cursor="c2", total=40)}
    result = _run("docs", "inspect", FID, "--text")
    assert result.exit_code == 0, result.output
    assert _previews(api) == [("preview", FID, None, None)], "the server's default page size"
    out = result.stdout
    assert "── stored text: passages 1–3 of 40 ──" in out
    assert f"[1] p. 1 · Chapter 1 · [doc:{FID}#00000000]\nChapter one text." in out
    assert f"[2] p. 2 · OCR · [doc:{FID}#00000001]\nTranscribed scan." in out
    assert f"[3] Figure 1 · image description · [doc:{FID}#00000002]\nTwo dogs in a park." in out
    assert out.index("Chapter one") < out.index("Transcribed") < out.index("Two dogs")
    assert f"── 37 more passages: cremind docs inspect {FID} --all prints the whole file ──" in out


def test_text_says_when_a_passage_continues_on_the_next_page(api):
    long = _seg(0, "a" * 100, part={"start": 0, "end": 100, "length": 250})
    api["previews"] = {None: _page([long], next_cursor="c2", total=1)}
    out = _run("docs", "inspect", FID, "--text").stdout
    assert "(this passage continues: 150 more characters)" in out
    assert "the rest of this passage: cremind docs inspect" in out


def test_text_without_passages_shows_the_file_details(api):
    api["previews"] = {None: _page([], total=0)}
    out = _run("docs", "inspect", FID, "--text").stdout
    assert "── no stored passages ──" in out
    assert "Only the file's details are indexed:\nFile: report.pdf" in out


# ── --all ──────────────────────────────────────────────────────────────────


def test_all_follows_every_cursor_and_joins_split_passages(api):
    api["previews"] = {
        None: _page([_seg(0, "one"), _seg(1, "two-a", part={"start": 0, "end": 5, "length": 9})],
                    next_cursor="c2", total=3),
        "c2": _page([_seg(1, "-rest", part={"start": 5, "end": 9, "length": 9}), _seg(2, "three")],
                    next_cursor=None, total=3, start=1),
    }
    result = _run("docs", "inspect", FID, "--all")
    assert result.exit_code == 0, result.output
    assert _previews(api) == [("preview", FID, None, 60), ("preview", FID, "c2", 60)]
    out = result.stdout
    assert "── stored text: all 3 passages ──" in out
    assert "\ntwo-a-rest\n" in out and "continues" not in out
    assert out.index("one") < out.index("two-a-rest") < out.index("three")
    assert "--all prints the whole file" not in out


def test_all_starts_over_once_when_the_file_is_re_indexed_meanwhile(api):
    api["previews"] = {
        None: [_page([_seg(0, "old one")], next_cursor="c2", revision="rev1"),
               _page([_seg(0, "new one")], next_cursor="d2", revision="rev2")],
        "c2": _stale(),
        "d2": _page([_seg(1, "new two")], next_cursor=None, revision="rev2", start=1),
    }
    result = _run("docs", "inspect", FID, "--all")
    assert result.exit_code == 0, result.output
    assert [c[2] for c in _previews(api)] == [None, "c2", None, "d2"]
    assert "re-indexed while it was being read; starting again" in result.stderr
    assert "old one" not in result.stdout, "nothing of the stale pass is printed"
    assert "new one" in result.stdout and "new two" in result.stdout
    assert "revision:  rev2" in result.stdout


def test_all_gives_up_when_the_file_is_re_indexed_again(api):
    api["previews"] = {None: _page([_seg(0, "one")], next_cursor="c2"), "c2": _stale()}
    result = _run("docs", "inspect", FID, "--all")
    assert result.exit_code == 1
    assert "re-indexed twice while it was being read" in result.stderr
    assert result.stdout == ""
    assert [c[2] for c in _previews(api)] == [None, "c2", None, "c2"]


# ── --json ─────────────────────────────────────────────────────────────────


def test_json_prints_the_summary_without_decoration(api):
    api["previews"] = {None: _page([_seg(0, "one")])}
    result = _run("--json", "docs", "inspect", FID)
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["fid"] == FID and out["summary"]["badge"] == "indexed" and out["revision"] == "rev1"
    assert out["metadata"]["document"] == {"pages": 3}
    assert "segments" not in out, "passages only with --text or --all"


def test_json_with_all_carries_every_passage(api):
    api["previews"] = {
        None: _page([_seg(0, "one"), _seg(1, "tw", part={"start": 0, "end": 2, "length": 3})], next_cursor="c2"),
        "c2": _page([_seg(1, "o", part={"start": 2, "end": 3, "length": 3}), _seg(2, "three")]),
    }
    result = _run("--json", "docs", "inspect", FID, "--all")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert [s["text"] for s in out["segments"]] == ["one", "two", "three"]
    assert all("part" not in s for s in out["segments"])
    assert out["next_cursor"] is None and out["restarted"] is False


def test_json_with_text_keeps_the_cursor(api):
    api["previews"] = {None: _page([_seg(0, "one")], next_cursor="c2")}
    out = json.loads(_run("--json", "docs", "inspect", FID, "--text").stdout)
    assert [s["text"] for s in out["segments"]] == ["one"] and out["next_cursor"] == "c2"


# ── the client wrappers ────────────────────────────────────────────────────


def test_the_client_builds_the_documented_requests():
    import asyncio

    import app.cli.client.docs as c

    seen = []

    class _Client:
        async def get_json(self, path, *, params=None):
            seen.append(("GET", path, params))
            return {}

        async def post_json(self, path, body=None, *, params=None):
            seen.append(("POST", path, body))
            return {}

    async def go():
        cl = _Client()
        await c.lookup_paths(cl, ["/a/b.pdf"])
        await c.file_preview(cl, FID)
        await c.file_preview(cl, FID, cursor="abc", limit=60)

    asyncio.run(go())
    assert seen == [
        ("POST", "/api/documentation-search/files/lookup", {"paths": ["/a/b.pdf"]}),
        ("GET", f"/api/documentation-search/files/{FID}/preview", None),
        ("GET", f"/api/documentation-search/files/{FID}/preview", {"cursor": "abc", "limit": 60}),
    ]
