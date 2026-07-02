"""The `cli` built-in tool and its disjoint-corpus guarantee.

The `cli` tool reuses ``documentation_search``'s vector-search + LLM-judge
pipeline (via ``run_doc_search``) but restricts the corpus to the ``cli`` scope,
so the two tools never surface each other's documents. These tests pin the
tool's identity/registration, that it delegates with ``scopes=[CLI_SCOPE]``, and
that ``DocumentSyncService.search`` actually isolates the scopes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import app.tools.builtin.cli as cli
import app.tools.builtin.documentation_search as ds
from app.documents.sync import CLI_SCOPE, SHARED_SCOPE, DocumentSyncService
from app.tools.builtin.base import BuiltInToolResult


# --- tool identity / registration ------------------------------------------

def test_cli_tool_config_identity():
    assert cli.TOOL_CONFIG["name"] == "cli"
    assert cli.TOOL_CONFIG["display_name"] == "CLI"
    # Locked-on like documentation_search — a core capability, always exposed.
    assert cli.TOOL_CONFIG.get("locked") is True

    tools = cli.get_tools({})
    assert len(tools) == 1
    assert isinstance(tools[0], cli.CliTool)
    assert tools[0].name == "cli"
    # The description must steer the model to actually run commands via exec_shell.
    assert "exec_shell" in cli.CliTool.description


def test_cli_registered_in_builtin_modules():
    from app.tools.builtin import _BUILTIN_MODULE_NAMES

    assert "cli" in _BUILTIN_MODULE_NAMES


# --- delegation to the shared search core -----------------------------------

def test_cli_run_delegates_with_cli_scope(monkeypatch):
    captured = {}

    async def _fake_run(arguments, *, scopes=None, log_label="documentation_search"):
        captured["scopes"] = scopes
        captured["log_label"] = log_label
        captured["query"] = arguments.get("query")
        return BuiltInToolResult(structured_content={"ok": True})

    # cli.run imports run_doc_search from the documentation_search module at
    # call time, so patching the attribute there intercepts the delegation.
    monkeypatch.setattr(ds, "run_doc_search", _fake_run)

    res = asyncio.run(cli.CliTool().run({"query": "create a profile"}))

    assert res.structured_content == {"ok": True}
    assert captured["scopes"] == [CLI_SCOPE]
    assert captured["log_label"] == "cli"
    assert captured["query"] == "create a profile"


# --- corpus isolation (no vector store → disk-scan fallback) ----------------

def _write_doc(path: Path, description: str, body: str = "BODY") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ndescription: "{description}"\n---\n\n{body}\n', encoding="utf-8")


def test_search_scopes_isolate_cli_from_general(tmp_path):
    # No embedding/vector store → search() uses the deterministic disk-scan
    # fallback, which is enough to prove scope filtering.
    svc = DocumentSyncService(working_dir=tmp_path, vector_store=None, embedding=None)
    _write_doc(svc.shared_dir() / "general.md", "a general how-to document")
    _write_doc(svc.cli_dir() / "[cli]cremind profile.md", "manage profiles via the cli")

    # cli scope returns only the CLI doc.
    cli_names = {h["name"] for h in svc.search(query="profile", profile="admin", scopes=[CLI_SCOPE])}
    assert "[cli]cremind profile" in cli_names
    assert "general" not in cli_names

    # default scopes ([shared, profile]) return the general doc, never the CLI one.
    default_names = {h["name"] for h in svc.search(query="profile", profile="admin")}
    assert "general" in default_names
    assert "[cli]cremind profile" not in default_names


def test_cli_dir_matches_cli_scope_dir(tmp_path):
    svc = DocumentSyncService(working_dir=tmp_path, vector_store=None, embedding=None)
    assert svc.cli_dir() == svc.scope_dir(CLI_SCOPE)
    assert svc.cli_dir() == tmp_path / "cli" / "documents"
    # And it is distinct from the shared corpus.
    assert svc.cli_dir() != svc.scope_dir(SHARED_SCOPE)
