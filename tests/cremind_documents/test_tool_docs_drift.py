"""Doc/code drift pin for the per-tool bundled reference docs.

Each configurable built-in tool has a bundled ``[tool] …`` doc so
``cremind_documentation_search`` can answer "what are <tool>'s settings / permission
modes?". This test fails if a tool's ``TOOL_CONFIG`` grows/renames a variable,
enum value, or argument that its doc doesn't mention — mechanizing the
CLAUDE.md "keep docs in sync with the feature" mandate for these docs.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "cremind_documents" / "bundled"

# module stem  ->  bundled doc filename (without the .md extension)
DOC_MAP = {
    "claude_code": "[tool]claude code",
    "codex": "[tool]codex",
    "exec_shell": "[tool]shell executor",
    "system_file": "[tool]system file",
    "browser": "[tool]browser",
    "web_search": "[tool]web search",
    "web_fetch": "[tool]web fetch",
    "image_understanding": "[tool]image understanding",
    "audio_understanding": "[tool]audio understanding",
    "gg_places": "[tool]google places",
    "weather": "[tool]accuweather weather",
    "cremind_documentation_search": "[tool]cremind documentation search",
    "documentation_search": "[tool]documentation search",
}


def _doc_text(stem: str) -> str:
    path = BUNDLED / f"{stem}.md"
    assert path.exists(), f"missing bundled doc: {path.name}"
    return path.read_text(encoding="utf-8")


def _tool_config(module_stem: str) -> dict:
    mod = importlib.import_module(f"app.tools.builtin.{module_stem}")
    cfg = getattr(mod, "TOOL_CONFIG", None)
    assert isinstance(cfg, dict), f"{module_stem} has no TOOL_CONFIG dict"
    return cfg


@pytest.mark.parametrize("module_stem,doc_stem", sorted(DOC_MAP.items()))
def test_doc_mentions_every_variable_and_enum(module_stem: str, doc_stem: str) -> None:
    cfg = _tool_config(module_stem)
    text = _doc_text(doc_stem)

    # Every Tool Variable name (and each declared enum value) must appear.
    for key, spec in (cfg.get("required_config") or {}).items():
        assert key in text, f"{doc_stem}.md is missing variable '{key}'"
        for val in spec.get("enum", []) or []:
            assert str(val) in text, (
                f"{doc_stem}.md is missing enum value '{val}' for '{key}'"
            )

    # Every Tool Argument name (and each declared enum value) must appear.
    arg_props = (cfg.get("arguments") or {}).get("properties", {})
    for arg_name, spec in arg_props.items():
        assert arg_name in text, f"{doc_stem}.md is missing argument '{arg_name}'"
        for val in spec.get("enum", []) or []:
            assert str(val) in text, (
                f"{doc_stem}.md is missing arg enum value '{val}' for '{arg_name}'"
            )


_CLI_RECIPE_RE = re.compile(
    r"cremind (?:--json )?tools "
    r"(?:set-var|get|leaves|set-leaf|options|set-args|get-args|enable|disable) "
    r"([A-Za-z0-9_.\-]+)"
)


@pytest.mark.parametrize("module_stem,doc_stem", sorted(DOC_MAP.items()))
def test_cli_recipes_configure_the_documented_tool(module_stem: str, doc_stem: str) -> None:
    """A tool doc's `cremind tools …` recipes must target that tool's own id.

    The search-tool rename moved ``documentation_search`` from Cremind's manual
    to the user's own documents. A recipe left on the old id would still run —
    and quietly retune the other tool — so every recipe is checked against the
    id the registry derives for the module (``slugify(SERVER_NAME)``).
    """
    from app.tools.ids import slugify

    mod = importlib.import_module(f"app.tools.builtin.{module_stem}")
    tool_id = slugify(getattr(mod, "SERVER_NAME", module_stem))
    text = _doc_text(doc_stem)
    targets = set(_CLI_RECIPE_RE.findall(text))
    assert targets <= {tool_id}, f"{doc_stem}.md configures {sorted(targets - {tool_id})}, not {tool_id}"
    assert re.search(rf"(?<![\w]){re.escape(tool_id)}(?![\w])", text), (
        f"{doc_stem}.md never names its tool id {tool_id!r}"
    )


def test_all_configurable_visible_tools_are_documented() -> None:
    """Any visible built-in with Tool Variables must have a reference doc.

    Guards against adding a new configurable, user-visible tool without a
    bundled doc (which is what left "Claude Code permission mode" unanswerable).
    Hidden/agent-internal tools are exempt.
    """
    from app.tools.builtin import _BUILTIN_MODULE_NAMES

    undocumented: list[str] = []
    for stem in _BUILTIN_MODULE_NAMES:
        cfg = _tool_config(stem)
        if cfg.get("hidden"):
            continue
        has_config = bool(cfg.get("required_config")) or bool(cfg.get("arguments"))
        if has_config and stem not in DOC_MAP:
            undocumented.append(stem)

    assert not undocumented, (
        "these visible, configurable built-in tools have no [tool] reference "
        f"doc (add one under app/cremind_documents/bundled/): {undocumented}"
    )
