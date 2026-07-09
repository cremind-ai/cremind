"""``list_builtin_skill_catalog`` — the first-run Setup Wizard's skill list.

During first-run setup no profile exists yet, so the registry holds no skill
rows and ``GET /api/tools`` can't enumerate them (skills are profile-owned).
The catalog scans the shipped ``app/skills/builtin`` tree so the wizard can
still show built-in skills as enable/disable toggles. Its ``tool_id`` must be
the profile-independent ``slugify(name)`` — the *base* of the
``<profile>__<slug>`` id the skill receives when it's seeded on Apply — so the
first-setup enable pass (``<profile>__<catalog tool_id>``) resolves onto the
seeded skills. These tests lock that contract in.
"""

from __future__ import annotations

from app.skills.sync import builtin_skill_dir_names, list_builtin_skill_catalog
from app.tools.ids import slugify


def test_catalog_lists_every_builtin_skill_as_enableable() -> None:
    rows = list_builtin_skill_catalog()
    assert rows, "expected at least one shipped built-in skill"

    for row in rows:
        assert row["tool_type"] == "skill"
        assert row["is_builtin"] is True
        assert row["enabled"] is True          # default on out of the box
        assert row["default_enabled"] is True
        assert row["toggle_locked"] is False   # the user can opt out
        assert row["required_fields"] == {}    # skills render as a bare toggle
        # tool_id is the profile-independent base the seeded id is built from.
        assert row["tool_id"] == slugify(row["name"])


def test_catalog_tool_ids_are_clean_slugs() -> None:
    # ``<profile>__<catalog tool_id>`` (the first-setup enable-pass key) only
    # resolves if the catalog id is already a clean slug (slugify idempotent).
    for row in list_builtin_skill_catalog():
        assert row["tool_id"] == slugify(row["tool_id"])


def test_catalog_covers_shipped_builtin_skill_dirs() -> None:
    rows = list_builtin_skill_catalog()
    ids = {row["tool_id"] for row in rows}
    assert len(ids) == len(rows), "duplicate tool_ids in the catalog"

    # Built-in dir name == SKILL.md name, so every catalog name maps to a
    # shipped built-in skill directory.
    dir_names = builtin_skill_dir_names()
    assert dir_names, "no built-in skill dirs found"
    assert {row["name"] for row in rows} <= dir_names
