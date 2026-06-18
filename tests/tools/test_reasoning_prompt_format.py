"""Tests for sub-tool rendering in the reasoning prompt.

The reasoning prompt must show each sub-tool's NAME and argument signature (e.g.
``overwrite_file(path, diff)``) so the model knows to format its Action_Input as
``overwrite_file path="..." diff="..."`` instead of dumping a bare diff. The
renderer previously printed only the description, omitting the name entirely.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.reasoning_agent import _format_tool_for_prompt
from app.tools.base import ToolSkill


def test_subtool_signature_rendered_with_params() -> None:
    skills = [
        ToolSkill(
            id="overwrite_file", name="overwrite_file", description="Edit a file.",
            parameters={
                "type": "object",
                "properties": {"path": {}, "diff": {}},
                "required": ["path", "diff"],
            },
        ),
        # No parameters -> render just the name (no parentheses).
        ToolSkill(id="list_files", name="list_files", description="List files."),
    ]
    tool = SimpleNamespace(
        tool_id="system_file", description="File assistant.", skills=skills)
    text = _format_tool_for_prompt(tool, {})

    # The name + signature must appear, not just the description.
    assert "overwrite_file(path, diff): Edit a file." in text
    assert "list_files: List files." in text


def test_signature_falls_back_to_properties_when_no_required() -> None:
    skills = [
        ToolSkill(
            id="grep_files", name="grep_files", description="Search contents.",
            parameters={"type": "object", "properties": {"pattern": {}, "path": {}}},
        ),
    ]
    tool = SimpleNamespace(
        tool_id="system_file", description="File assistant.", skills=skills)
    text = _format_tool_for_prompt(tool, {})
    assert "grep_files(pattern, path): Search contents." in text
