"""`cremind docs search|find|read|cite` — the terminal side of the tool.

The commands import their client functions inside the body, so the client is
patched in ``app.cli.client.docs`` and nothing reaches the network. What
is pinned: the flags become exactly the arguments the agent's leaves take,
the text is printed as the server rendered it, errors keep their candidates,
and `cite` normalises a token before asking.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402


def _patch_query(monkeypatch, answer=None, error=None):
    import app.cli.client.docs as client

    captured: dict = {}

    async def fake_query(c, leaf, body):
        captured["leaf"], captured["body"] = leaf, body
        if error is not None:
            from app.cli.client._base import APIError

            raw = json.dumps(error).encode()
            raise APIError(404, raw.decode(), raw)
        return answer or {"text": "[Documentation Search · search] ok", "mode": "hybrid", "items": []}

    monkeypatch.setattr(client, "query", fake_query)
    return captured


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


def test_search_flags_become_the_leaf_arguments(monkeypatch):
    captured = _patch_query(monkeypatch)
    result = _run("docs", "search", "AI challenges", "--folder", "Reports", "--folder", "Notes",
                  "--type", "document", "--from", "2026-09-22", "--to", "2026-09-24",
                  "--date-field", "modified", "--group-by", "folder", "--top-k", "5", "--thorough")
    assert result.exit_code == 0, result.output
    assert "[Documentation Search · search] ok" in result.stdout
    assert captured["leaf"] == "search"
    body = captured["body"]
    assert body["query"] == "AI challenges"
    assert body["filters"] == {"folder": ["Reports", "Notes"], "types": ["document"],
                               "date_from": "2026-09-22", "date_to": "2026-09-24", "date_field": "modified"}
    assert body["group_by"] == "folder" and body["top_k"] == 5 and body["thorough"] is True


def test_find_without_a_query_lists(monkeypatch):
    captured = _patch_query(monkeypatch, answer={"text": "listing"})
    result = _run("docs", "find", "--kind", "project", "--sort", "newest", "--limit", "3")
    assert result.exit_code == 0, result.output
    assert captured["leaf"] == "find"
    assert captured["body"]["query"] is None
    assert captured["body"]["kind"] == "project" and captured["body"]["limit"] == 3
    assert captured["body"]["filters"] is None


def test_read_passes_every_locator(monkeypatch):
    captured = _patch_query(monkeypatch, answer={"text": "file text"})
    result = _run("docs", "read", "[doc:k7m2xq9a]", "--section", "Điều 203", "--pages", "3-5")
    assert result.exit_code == 0, result.output
    assert captured["leaf"] == "read"
    assert captured["body"]["file"] == "[doc:k7m2xq9a]"
    assert captured["body"]["section"] == "Điều 203" and captured["body"]["pages"] == "3-5"


def test_json_prints_the_structured_result(monkeypatch):
    _patch_query(monkeypatch, answer={"text": "t", "mode": "lexical_only", "items": [{"fid": "k7m2xq9a"}]})
    result = _run("--json", "docs", "search", "x")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["mode"] == "lexical_only"


def test_errors_print_the_message_and_candidates(monkeypatch):
    _patch_query(monkeypatch, error={"error": "NotFound", "message": "No indexed file matches 'x'.",
                                     "candidates": ["[doc:k7m2xq9a] Reports/x.md"]})
    result = _run("docs", "read", "x")
    assert result.exit_code == 1
    assert "No indexed file matches" in result.output
    assert "[doc:k7m2xq9a] Reports/x.md" in result.output


def test_cite_normalises_the_token_and_prints_where_it_points(monkeypatch):
    import app.cli.client.docs as client

    asked: dict = {}

    async def fake_resolve(c, tokens, conversation_id=None):
        asked["tokens"] = tokens
        return {"items": {"[doc:k7m2xq9a#3f9c2e1b]": {
            "status": "verified", "locator_label": "p. 2", "snippet": "The main AI challenges",
            "file": {"name": "AI.docx", "rel_path": "Reports/AI.docx"},
        }}}

    monkeypatch.setattr(client, "resolve_citations", fake_resolve)
    result = _run("docs", "cite", "DOC:K7M2XQ9A#3F9C2E1B")
    assert result.exit_code == 0, result.output
    assert asked["tokens"] == ["[doc:k7m2xq9a#3f9c2e1b]"]
    assert "verified" in result.stdout and "Reports/AI.docx" in result.stdout and "p. 2" in result.stdout


def test_cite_rejects_what_is_not_a_token(monkeypatch):
    result = _run("docs", "cite", "hello")
    assert result.exit_code == 1
    assert "not a citation token" in result.output
