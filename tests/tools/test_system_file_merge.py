"""Tests for merging the file-watcher tool into the System File tool.

The three file-watcher functions (``register_file_watcher`` /
``list_file_watchers`` / ``delete_file_watcher``) used to be a standalone
"Register File Watcher" built-in tool group; they are now subtools of the
``system_file`` group. These tests pin that merge:

- the standalone tool is gone from registration and the catalog,
- the watcher functions ship inside ``system_file`` and are self-describing,
- ``register_file_watcher`` stays in the schema on EVERY run (no
  ``get_prepare_tools`` hook), so the ``tools=`` block is byte-identical for
  prompt-cache reuse; recursive-event-storm prevention moved to the reasoning
  agent's dispatch (it refuses the call while reacting to an event).
"""

from __future__ import annotations

from app.tools.builtin import (
    _BUILTIN_MODULE_NAMES,
    get_builtin_tool_config,
    list_builtin_tool_catalog,
)
from app.tools.builtin import system_file


_WATCHER_NAMES = {"register_file_watcher", "list_file_watchers", "delete_file_watcher"}

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
    assert {"move_file", "copy_file"}.issubset(names)
    assert len(tools) == 12  # 9 file ops (incl. move/copy) + 3 watcher functions


def test_watcher_subtools_are_self_describing() -> None:
    # With no group system_prompt, per-function descriptions drive routing and
    # the parent's Sub-Tools list — they must not be empty.
    tools = {t.name: t for t in system_file.get_tools({})}
    for name in _WATCHER_NAMES:
        assert tools[name].description.strip(), f"{name} needs a non-empty description"


def test_no_prepare_tools_hook_keeps_schema_byte_stable() -> None:
    # The event-run schema filter was removed: register_file_watcher now stays in
    # the tools= block on every run (byte-stable prompt-cache prefix). Storm
    # prevention is enforced at dispatch in the reasoning agent instead. The module
    # must expose NO prepare_tools hook — otherwise the tools list would mutate
    # per run and bust the cache (the bug this fix addresses).
    assert not hasattr(system_file, "get_prepare_tools")
