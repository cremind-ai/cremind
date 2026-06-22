"""Guard the prompt steering that keeps created titles in the user's language.

These are deterministic string checks (the actual behavior depends on the LLM):
they fail loudly if the language-preservation instruction is dropped in a future
prompt edit.
"""

from __future__ import annotations


def test_reasoning_template_has_language_preservation_rule():
    from app.agent.reasoning_agent import template_instruction
    t = template_instruction.lower()
    assert "preserve the user's language" in t
    assert "never translate" in t or "do not translate" in t


def test_schedule_create_title_param_asks_for_original_language():
    from app.tools.builtin.scheduler_actions import ScheduleCreateTool
    desc = ScheduleCreateTool().parameters["properties"]["title"]["description"].lower()
    assert "original language" in desc
    assert "never translate" in desc


def test_scheduler_group_prompt_reinforces_title_language():
    from app.tools.builtin.scheduler import TOOL_CONFIG
    sp = TOOL_CONFIG["llm_parameters"]["system_prompt"].lower()
    assert "original language" in sp and "translate" in sp
