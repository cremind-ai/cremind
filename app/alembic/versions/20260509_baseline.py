"""Baseline schema — captures the post-v0.1.0 inline-migration state.

Revision ID: 20260509_baseline
Revises:
Create Date: 2026-05-09

This is the first managed migration. It corresponds to the union of:

- Every table declared in :mod:`app.storage.models`.
- Every additive ``ALTER TABLE`` that was previously applied at boot from
  ``ConversationStorage.initialize()`` (skill_mode, channel_id,
  working_directory, task_id widening, the ``DROP INDEX uq_skill_event_subs``
  one-shot).

Implementation strategy: rather than transcribing 14 ``op.create_table``
calls (which would drift from ``models.py`` on the next field rename), the
baseline rebuilds the schema from the declarative metadata. That mirrors
what ``initialize()`` did before Alembic, with one important difference —
it runs only on fresh installs. Existing databases (with rows in
``profiles``) are stamped at this revision by the runtime migration
helper without re-running ``upgrade()``.

Every future migration is explicit (no ``create_all`` cheating), so the
chain is auditable from this point forward.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import Table
from sqlalchemy.orm import class_mapper

# revision identifiers, used by Alembic.
revision: str = "20260509_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cremind_tables():
    """Resolve the Cremind table list from the live ORM metadata.

    Mirrors the order used by the previous in-process initializer so FK
    dependencies are honored even on engines that don't defer constraint
    checks (Postgres without ``deferrable``).
    """
    from a2a.server.models import Base  # noqa: F401 — registers a2a tables
    from app.storage.models import (
        AuthTokenModel,
        AutostartProcessModel,
        ChannelModel,
        ChannelSenderModel,
        ConversationModel,
        FileWatcherSubscriptionModel,
        LLMConfigModel,
        MessageModel,
        ProfileModel,
        ProfileToolModel,
        ServerConfigModel,
        SkillEventSubscriptionModel,
        ToolConfigModel,
        ToolModel,
        UserConfigModel,
    )

    ordered_models = [
        ProfileModel,
        ChannelModel,
        ConversationModel,
        MessageModel,
        ChannelSenderModel,
        ServerConfigModel,
        LLMConfigModel,
        UserConfigModel,
        ToolModel,
        ProfileToolModel,
        ToolConfigModel,
        AuthTokenModel,
        AutostartProcessModel,
        SkillEventSubscriptionModel,
        FileWatcherSubscriptionModel,
    ]
    tables: list[Table] = []
    for model in ordered_models:
        for table in class_mapper(model).tables:
            if isinstance(table, Table):
                tables.append(table)
    return tables


def upgrade() -> None:
    bind = op.get_bind()
    tables = _cremind_tables()
    # ``checkfirst=True`` keeps this idempotent — re-runs after a partial
    # failure won't error on tables that already exist.
    for table in tables:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    # Reverse order so FK children drop before parents.
    for table in reversed(_cremind_tables()):
        table.drop(bind=bind, checkfirst=True)
