"""Guard: the Alembic migration tree must have exactly one head.

A second head sneaks in when a migration is merged with a ``down_revision`` that
branches off an earlier revision instead of chaining onto the current tip (as
happened when the jira ``issue_changed`` split was merged onto the schedule
branch). ``cremind db upgrade`` and boot-time ``ensure_at_head`` both target
``head`` (singular) and abort on multiple heads, so this is release-blocking.
"""
from __future__ import annotations

from app.storage import migrations


def test_single_alembic_head():
    heads = migrations.heads()
    assert len(heads) == 1, (
        "Alembic migration tree must have exactly one head; found "
        f"{len(heads)}: {heads}. Re-parent the divergent migration's "
        "down_revision onto the current tip (or add a merge revision)."
    )
