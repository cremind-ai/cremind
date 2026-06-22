"""Split the jira skill's ``issue_changed`` subscriptions into lifecycle events.

Revision ID: 20260622_split_jira_issue_changed
Revises: 20260621c_drop_reminder
Create Date: 2026-06-22

The jira skill replaced its single generic ``issue_changed`` event with five
lifecycle events (``issue_created``, ``issue_updated``, ``issue_transitioned``,
``issue_commented``, ``issue_deleted``). Existing ``skill_event_subscriptions``
rows still reference ``issue_changed`` — a now-undeclared event — so they would
silently stop firing.

This migration fans out each such row into four sibling rows, preserving the
subscriber's ``action``. We map to created / updated / transitioned / commented
but deliberately **omit** ``issue_deleted``: the old ``issue_changed`` never meant
deletions, so we don't newly notify on deletes the user never asked for.

``event_type = 'issue_changed'`` was only ever declared by the jira skill, so it
uniquely identifies the rows to migrate — no skill_name match is needed. The
new rows copy ``conversation_id``/``profile``/``skill_name``/``action``/``created_at``
from the original.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

from alembic import op
from sqlalchemy import bindparam, text

# revision identifiers, used by Alembic.
revision: str = "20260622_split_jira_issue_changed"
down_revision: Union[str, Sequence[str], None] = "20260621c_drop_reminder"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_EVENT = "issue_changed"
_NEW_EVENTS = ["issue_created", "issue_updated", "issue_transitioned", "issue_commented"]


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        text(
            "SELECT id, conversation_id, profile, skill_name, action, created_at "
            "FROM skill_event_subscriptions WHERE event_type = :old"
        ),
        {"old": _OLD_EVENT},
    ).mappings().all()

    for row in rows:
        for event_type in _NEW_EVENTS:
            bind.execute(
                text(
                    "INSERT INTO skill_event_subscriptions "
                    "(id, conversation_id, profile, skill_name, event_type, action, created_at) "
                    "VALUES (:id, :conversation_id, :profile, :skill_name, :event_type, :action, :created_at)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "conversation_id": row["conversation_id"],
                    "profile": row["profile"],
                    "skill_name": row["skill_name"],
                    "event_type": event_type,
                    "action": row["action"],
                    "created_at": row["created_at"],
                },
            )

    bind.execute(
        text("DELETE FROM skill_event_subscriptions WHERE event_type = :old"),
        {"old": _OLD_EVENT},
    )


def downgrade() -> None:
    """Best-effort collapse of the four lifecycle rows back to one ``issue_changed``.

    Round-trip fidelity is not guaranteed if rows were edited in between.
    """
    bind = op.get_bind()
    groups = bind.execute(
        text(
            "SELECT conversation_id, profile, skill_name, action, MIN(created_at) AS created_at "
            "FROM skill_event_subscriptions WHERE event_type IN :events "
            "GROUP BY conversation_id, profile, skill_name, action"
        ).bindparams(bindparam("events", expanding=True)),
        {"events": _NEW_EVENTS},
    ).mappings().all()

    for g in groups:
        bind.execute(
            text(
                "INSERT INTO skill_event_subscriptions "
                "(id, conversation_id, profile, skill_name, event_type, action, created_at) "
                "VALUES (:id, :conversation_id, :profile, :skill_name, :event_type, :action, :created_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "conversation_id": g["conversation_id"],
                "profile": g["profile"],
                "skill_name": g["skill_name"],
                "event_type": _OLD_EVENT,
                "action": g["action"],
                "created_at": g["created_at"],
            },
        )

    bind.execute(
        text("DELETE FROM skill_event_subscriptions WHERE event_type IN :events").bindparams(
            bindparam("events", expanding=True)
        ),
        {"events": _NEW_EVENTS},
    )
