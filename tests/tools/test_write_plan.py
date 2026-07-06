"""``write_plan`` tool + the plans-dir path helper.

The tool writes the plan markdown to a durable per-conversation file, parks it
in :mod:`app.agent.plan_state`, and queues a ``plan_ready`` event.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.agent import plan_state
from app.utils.task_context import current_task_id_var


# ── plans_dir path helper ─────────────────────────────────────────────────

def test_plan_file_path_sanitizes_and_forces_md(tmp_path, monkeypatch):
    from app.config.settings import BaseConfig
    import app.utils.plans_dir as pd
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)

    # A traversal-y name is reduced to its basename and gets a .md suffix.
    p = pd.plan_file_path("default", "c1", "../../etc/refactor")
    assert p.endswith(".md")
    assert os.path.basename(p) == "refactor.md"
    assert pd.is_inside_conversation_plans("default", "c1", p)


def test_plan_file_path_avoids_collision(tmp_path, monkeypatch):
    from app.config.settings import BaseConfig
    import app.utils.plans_dir as pd
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)

    p1 = pd.plan_file_path("default", "c1", "plan.md")
    with open(p1, "w", encoding="utf-8") as fh:
        fh.write("first")
    p2 = pd.plan_file_path("default", "c1", "plan.md")
    assert p1 != p2
    assert os.path.basename(p2) == "plan-2.md"


def test_remove_conversation_plans(tmp_path, monkeypatch):
    from app.config.settings import BaseConfig
    import app.utils.plans_dir as pd
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    p = pd.plan_file_path("default", "c1", "plan.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("x")
    assert os.path.exists(p)
    pd.remove_conversation_plans("default", "c1")
    assert not os.path.exists(os.path.dirname(p))


# ── write_plan tool ───────────────────────────────────────────────────────

class _FakeStorage:
    async def get_conversation_by_context(self, profile, context_id):
        return {"id": "c1"}

    async def get_conversation(self, cid):
        return {"id": cid}


def test_write_plan_writes_file_and_parks(tmp_path, monkeypatch):
    from app.config.settings import BaseConfig
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)

    import app.events.runner as runner
    monkeypatch.setattr(runner, "get_conversation_storage", lambda: _FakeStorage())

    from app.tools.builtin.write_plan import WritePlanTool
    tool = WritePlanTool()
    run_id = "msg:c1:plan"
    args = {
        "filename": "refactor-auth.md",
        "title": "Refactor auth",
        "markdown": "# Refactor auth\n\n1. Step one",
        "_profile": "default",
        "_context_id": "ctx-c1",
    }

    async def _run():
        token = current_task_id_var.set(run_id)
        try:
            result = await tool.run(args)
            return result, plan_state.get_plan(run_id), plan_state.drain_emit(run_id)
        finally:
            current_task_id_var.reset(token)

    try:
        result, parked, emits = asyncio.run(_run())
        assert result.content and "awaiting_approval" not in result.content[0]["text"]  # user-facing text
        assert parked is not None
        assert parked["status"] == "awaiting_approval"
        assert parked["title"] == "Refactor auth"
        assert os.path.isfile(parked["path"])
        with open(parked["path"], encoding="utf-8") as fh:
            assert "Refactor auth" in fh.read()
        assert len(emits) == 1 and emits[0]["event"] == "plan_ready"
        assert emits[0]["data"]["markdown"].startswith("# Refactor auth")
    finally:
        plan_state.clear(run_id)


def test_write_plan_requires_markdown(monkeypatch):
    from app.tools.builtin.write_plan import WritePlanTool
    tool = WritePlanTool()

    async def _run():
        token = current_task_id_var.set("msg:c1:empty")
        try:
            return await tool.run({"filename": "x.md", "markdown": "  "})
        finally:
            current_task_id_var.reset(token)

    result = asyncio.run(_run())
    assert result.content and "no plan content" in result.content[0]["text"].lower()
    plan_state.clear("msg:c1:empty")
