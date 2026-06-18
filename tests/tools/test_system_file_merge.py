"""Tests for merging the file-watcher tool into the System File tool.

The three file-watcher functions (``register_file_watcher`` /
``list_file_watchers`` / ``delete_file_watcher``) used to be a standalone
"Register File Watcher" built-in tool group; they are now subtools of the
``system_file`` group. These tests pin that merge:

- the standalone tool is gone from registration and the catalog,
- the watcher functions ship inside ``system_file`` and are self-describing,
- the ``register_file_watcher`` subtool is suppressed on event-triggered runs
  (via ``system_file.get_prepare_tools``) to prevent recursive event storms,
  while ``list``/``delete`` and the six file ops stay available.
"""

from __future__ import annotations

from app.tools.builtin import (
    _BUILTIN_MODULE_NAMES,
    get_builtin_tool_config,
    list_builtin_tool_catalog,
)
from app.tools.builtin import system_file


_WATCHER_NAMES = {"register_file_watcher", "list_file_watchers", "delete_file_watcher"}

_ALL_SYSTEM_FILE_FUNCS = [
    "search_files", "grep_files", "list_files", "get_file_info", "read_file",
    "write_file", "overwrite_file",
    "register_file_watcher", "list_file_watchers", "delete_file_watcher",
]


def _fake_openai_tools() -> list[dict]:
    """The OpenAI-format tool list the adapter hands ``prepare_tools``."""
    return [{"type": "function", "function": {"name": n}} for n in _ALL_SYSTEM_FILE_FUNCS]


def test_register_file_watcher_not_a_standalone_module() -> None:
    assert "register_file_watcher" not in _BUILTIN_MODULE_NAMES
    # TOOL_CONFIG was removed from the module, so the config lookup is empty.
    assert get_builtin_tool_config("register_file_watcher") == {}


def test_catalog_has_no_standalone_file_watcher_tool() -> None:
    catalog = list_builtin_tool_catalog()
    tool_ids = {row["tool_id"] for row in catalog}
    names = {row["name"] for row in catalog}
    assert "register_file_watcher" not in tool_ids
    assert "Register File Watcher" not in names


def test_system_file_catalog_description_mentions_watching() -> None:
    catalog = {row["tool_id"]: row for row in list_builtin_tool_catalog()}
    assert "system_file" in catalog
    # The parent agent / wizard must learn System File now handles watching.
    assert "watch" in catalog["system_file"]["description"].lower()


def test_get_tools_includes_watcher_subtools() -> None:
    tools = system_file.get_tools({})
    names = {t.name for t in tools}
    assert _WATCHER_NAMES.issubset(names)
    assert "grep_files" in names
    assert len(tools) == 10  # 7 file ops + 3 watcher functions


def test_watcher_subtools_are_self_describing() -> None:
    # With no group system_prompt, per-function descriptions drive routing and
    # the parent's Sub-Tools list — they must not be empty.
    tools = {t.name: t for t in system_file.get_tools({})}
    for name in _WATCHER_NAMES:
        assert tools[name].description.strip(), f"{name} needs a non-empty description"


def test_prepare_tools_passthrough_on_normal_run() -> None:
    prepare = system_file.get_prepare_tools()
    tools = _fake_openai_tools()
    # No arguments at all -> nothing filtered (the non-event-run case).
    result = prepare("q", list(tools), arguments=None, context_id="c", profile="p")
    assert [t["function"]["name"] for t in result] == _ALL_SYSTEM_FILE_FUNCS
    # Explicit falsy flag -> nothing filtered.
    result = prepare("q", list(tools), arguments={"_triggered_by_event": False})
    assert len(result) == 10


def test_prepare_tools_suppresses_only_register_on_event_run() -> None:
    prepare = system_file.get_prepare_tools()
    result = prepare("q", _fake_openai_tools(), arguments={"_triggered_by_event": True})
    names = {t["function"]["name"] for t in result}
    assert "register_file_watcher" not in names  # the storm risk: suppressed
    assert "list_file_watchers" in names          # harmless: stays
    assert "delete_file_watcher" in names          # harmless: stays
    assert "grep_files" in names                   # read-only: stays
    assert len(result) == 9
