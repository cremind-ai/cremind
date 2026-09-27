"""`cremind docs inspect` against the real routes, in-process.

test_inspect_cli.py pins the command against scripted answers; this runs the
same command with its HTTP client answered by the real
``/api/documentation-search/files/lookup`` and ``…/files/{fid}/preview``
handlers over a real index, so a change on either side of the contract (the
lookup keys, the summary, the cursor, the 409 body) fails here. The re-index
between two pages is a real one: the server itself answers StalePreview.
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from urllib.parse import unquote, urlencode

import pytest

pytest.importorskip("a2a")
pytest.importorskip("typer")

from starlette.datastructures import QueryParams  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from app.api import documents_files as api  # noqa: E402
from app.documents.discovery.walker import path_hash  # noqa: E402
from app.documents.textnorm import text_hash  # noqa: E402
from app.documents.types import Chunk, ChunkDiff  # noqa: E402
from tests.documents._citations_env import build, close  # noqa: E402

LOOKUP = "/api/documentation-search/files/lookup"
PREVIEW = "/api/documentation-search/files/{fid}/preview"
_PREVIEW_PATH = re.compile(r"/api/documentation-search/files/([^/]+)/preview")


class _Req:
    def __init__(self, *, body=None, query="", path_params=None):
        self.user = SimpleNamespace(is_authenticated=True, username="alice")
        self.query_params = QueryParams(query)
        self.path_params = path_params or {}
        self._body = body or {}

    async def json(self):
        return self._body


def _route(path: str, method: str):
    for r in api.get_documents_files_routes():
        if r.path == path and method in (r.methods or ()):
            return r.endpoint
    raise AssertionError(f"no route {method} {path}")


def _answer(resp):
    from app.cli.client._base import APIError

    if resp.status_code >= 400:
        raise APIError(resp.status_code, "", bytes(resp.body))
    return json.loads(resp.body)


class _RouteClient:
    """Stands in for the CLI's ``Client``: each call goes to the real handler.
    ``before_page`` runs before every preview request (to re-index mid-read)."""

    before_page = None

    def __init__(self, cfg, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_json(self, path, *, params=None):
        m = _PREVIEW_PATH.fullmatch(path)
        assert m, f"unexpected GET {path}"
        if _RouteClient.before_page is not None:
            _RouteClient.before_page(params or {})
        req = _Req(query=urlencode(params or {}), path_params={"fid": unquote(m.group(1))})
        return _answer(await _route(PREVIEW, "GET")(req))

    async def post_json(self, path, body=None, *, params=None):
        assert path == LOOKUP, f"unexpected POST {path}"
        return _answer(await _route(LOOKUP, "POST")(_Req(body=body)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    import app.cli.client._base as base

    e = build(tmp_path, monkeypatch)
    (e.root / "Reports").mkdir()
    (e.root / "Reports" / "annual.pdf").write_bytes(b"%PDF-1.4 fake")
    rel = "Reports/annual.pdf"
    row = e.db.insert_file("local", rel, path_hash(rel), name="annual.pdf", kind="pdf", status="indexed")
    texts = ["File: annual.pdf"] + [f"Section {i} of the annual report." for i in range(40)]
    chunks = [Chunk(ordinal=-1 if i == 0 else i - 1, ctype="file_card" if i == 0 else "body", heading="",
                    text=x, text_hash=text_hash("", x), locator={} if i == 0 else {"page": i})
              for i, x in enumerate(texts)]
    e.db.apply_chunks(file_id=row["id"], folder_id=None, source="local", diff=ChunkDiff(add=chunks))
    e.annual = e.db.get_file(row["id"])
    monkeypatch.setattr(base, "Client", _RouteClient)
    monkeypatch.setattr(_RouteClient, "before_page", None)
    yield e
    close(e)


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


def _rewrite_section_0(env) -> None:
    from app.documents.chunking import diff_chunks

    old = env.db.get_chunks(env.annual["id"])
    rows = env.db.chunk_rows([c.id for c in old])

    def text_of(r):
        return "Section 0 was rewritten." if r["text"].startswith("Section 0 ") else r["text"]

    new = [Chunk(ordinal=r["ordinal"], ctype=r["ctype"], heading="", text=text_of(r),
                 text_hash=text_hash("", text_of(r)), locator=r["locator"]) for r in rows]
    env.db.apply_chunks(file_id=env.annual["id"], folder_id=None, source="local", diff=diff_chunks(old, new))


def test_a_relative_path_is_looked_up_and_summarised(env, monkeypatch):
    monkeypatch.chdir(env.root)
    result = _run("docs", "inspect", os.path.join("Reports", "annual.pdf"))
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.startswith("annual.pdf · Indexed content\n")
    assert "  Indexed: 40 passages of text." in out
    assert f"id:        {env.annual['cite_id']} " in out
    assert f"path:      {env.root / 'Reports' / 'annual.pdf'}" in out
    assert "readable:  40 passages" in out and "1 metadata" in out


def test_json_carries_the_servers_summary_and_card(env):
    result = _run("--json", "docs", "inspect", f"[doc:{env.annual['cite_id']}]", "--text")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["summary"]["state"] == "complete" and out["summary"]["revision"] == out["revision"]
    assert out["metadata"]["card"] == "File: annual.pdf"
    assert len(out["segments"]) == 30 and out["total_segments"] == 40 and out["next_cursor"]


def test_all_restarts_after_a_real_re_index_between_pages(env, monkeypatch):
    import app.cli.commands.docs as cmd

    monkeypatch.setattr(cmd, "_INSPECT_ALL_PAGE", 15)
    done = []

    def reindex_once(params):
        if params.get("cursor") and not done:
            _rewrite_section_0(env)
            done.append(True)

    monkeypatch.setattr(_RouteClient, "before_page", staticmethod(reindex_once))
    result = _run("docs", "inspect", env.annual["cite_id"], "--all")
    assert result.exit_code == 0, result.output
    assert "re-indexed while it was being read; starting again" in result.stderr
    out = result.stdout
    assert "── stored text: all 40 passages ──" in out
    assert "Section 0 was rewritten." in out and "Section 0 of the annual report." not in out
    assert out.index("Section 1 of") < out.index("Section 39 of")


def test_an_excluded_and_an_outside_path(env, tmp_path):
    env.storage.upsert_source("alice", "local", enabled=True, root_path=str(env.root),
                              excludes=[{"pattern": "Reports/**", "type": "glob", "mode": "skip"}])
    result = _run("docs", "inspect", str(env.root / "Reports" / "annual.pdf"))
    assert result.exit_code == 1
    assert "Excluded from indexing" in result.stderr
    result = _run("docs", "inspect", str(tmp_path / "elsewhere.pdf"))
    assert result.exit_code == 1
    assert "Not in the indexed folder" in result.stderr
