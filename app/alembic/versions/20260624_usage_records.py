"""Token-usage + estimated-pricing fact table.

Revision ID: 20260624_usage_records
Revises: 20260622_split_jira_events
Create Date: 2026-06-24

Adds ``usage_records`` — one row per LLM invocation within an agent turn, with
raw four-way token counts (uncached input / cache-read / cache-write / output)
plus frozen per-component USD cost and the rate snapshot used. This is the fact
table behind the "Usage & Cost" dashboard and the per-conversation usage panel;
everything the dashboard groups by (conversation, profile, provider, model,
source, tool, time) is an indexed column.

Purely additive and defensive — like ``20260618_memory`` it inspects the live
schema and only creates what is missing, so it upgrades fresh installs and
pre-existing ``~/.cremind`` databases alike. Pure ``create_table`` +
``create_index`` (no ALTERs), so it is SQLite-safe regardless of batch mode.

Backfill: each historical ``role='agent'`` message with a non-empty
``token_usage`` JSON becomes one ``source_kind='aggregate'`` row so old
conversations still appear in the dashboard. Token counts are exact; cost is
best-effort — left NULL when the model that produced the turn can't be recovered
(today nothing stamps provider/model onto historical messages, so most
backfilled rows are token-only). New turns (post-instrumentation) are exact on
both. Idempotent: messages already represented are skipped on re-run.
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260624_usage_records"
down_revision: Union[str, Sequence[str], None] = "20260622_split_jira_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXED_COLUMNS = (
    "conversation_id", "message_id", "profile", "provider",
    "model", "source_kind", "tool_id", "total_usd",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "usage_records" in set(inspector.get_table_names()):
        return  # already created (rerun / fresh install where it pre-exists)

    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("profile", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("model_group", sa.String(length=32), nullable=True),
        sa.Column("source_kind", sa.String(length=16), nullable=False, server_default="reasoning"),
        sa.Column("tool_id", sa.String(length=128), nullable=True),
        sa.Column("label", sa.String(length=256), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uncached_input_usd", sa.Float(), nullable=True),
        sa.Column("cache_read_usd", sa.Float(), nullable=True),
        sa.Column("cache_write_usd", sa.Float(), nullable=True),
        sa.Column("output_usd", sa.Float(), nullable=True),
        sa.Column("total_usd", sa.Float(), nullable=True),
        sa.Column("rate_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in _INDEXED_COLUMNS:
        op.create_index(f"ix_usage_records_{col}", "usage_records", [col])
    op.create_index("ix_usage_records_conv_msg", "usage_records", ["conversation_id", "message_id"])
    op.create_index("ix_usage_records_profile_created", "usage_records", ["profile", "created_at"])

    _backfill_from_messages(bind)


def _backfill_from_messages(bind) -> None:
    """One aggregate usage row per historical assistant message with tokens."""
    meta = sa.MetaData()
    messages = sa.Table("messages", meta, autoload_with=bind)
    conversations = sa.Table("conversations", meta, autoload_with=bind)
    usage = sa.Table("usage_records", meta, autoload_with=bind)

    rows = bind.execute(
        sa.select(
            messages.c.id,
            messages.c.conversation_id,
            messages.c.token_usage,
            messages.c.created_at,
            messages.c.metadata,  # MessageModel.message_metadata maps to the "metadata" column
            conversations.c.profile,
        )
        .select_from(
            messages.join(conversations, messages.c.conversation_id == conversations.c.id)
        )
        .where(messages.c.role == "agent")
    ).fetchall()

    to_insert = []
    for mid, conv_id, tok, created_at, msg_meta, profile in rows:
        d = _as_dict(tok)
        if not d:
            continue
        it = int(d.get("input_tokens") or 0)
        cr = int(d.get("cache_read_input_tokens") or 0)
        cc = int(d.get("cache_creation_input_tokens") or 0)
        ot = int(d.get("output_tokens") or 0)
        if not (it or cr or cc or ot):
            continue
        provider, model = _model_from_metadata(msg_meta)
        cost = _safe_cost(provider, model, it, cr, cc, ot)
        to_insert.append({
            "id": str(uuid.uuid4()),
            "conversation_id": conv_id,
            "message_id": mid,
            "profile": profile,
            "provider": provider,
            "model": model,
            "model_group": cost["model_group"],
            "source_kind": "aggregate",
            "tool_id": None,
            "label": None,
            "step_index": 0,
            "input_tokens": it,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc,
            "output_tokens": ot,
            "uncached_input_usd": cost["uncached_input_usd"],
            "cache_read_usd": cost["cache_read_usd"],
            "cache_write_usd": cost["cache_write_usd"],
            "output_usd": cost["output_usd"],
            "total_usd": cost["total_usd"],
            # rate_snapshot omitted (left NULL) for backfilled aggregate rows —
            # avoids dialect-specific JSON binding in raw bulk_insert; tokens are
            # exact and the row can be re-priced on demand.
            "rate_snapshot": None,
            "created_at": created_at,
        })

    if to_insert:
        op.bulk_insert(usage, to_insert)


def _as_dict(value) -> dict:
    """Coerce a JSON column value (dict on Postgres, str on SQLite) to a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _model_from_metadata(msg_meta) -> tuple:
    """Best-effort recovery of (provider, model) from message metadata.

    Returns (None, None) when not recorded — historical messages generally do
    not carry the model, so cost stays NULL while tokens remain exact.
    """
    meta = _as_dict(msg_meta)
    if not meta:
        return (None, None)
    provider = meta.get("provider") or meta.get("llm_provider")
    model = meta.get("model") or meta.get("model_name")
    return (provider, model)


def _safe_cost(provider, model, it, cr, cc, ot) -> dict:
    """Compute frozen cost columns, swallowing any pricing error to all-NULL.

    A pricing bug must never block a boot-time migration. Pricing is imported
    lazily so a config/import problem can't break ``alembic upgrade``.
    """
    null = {
        "uncached_input_usd": None, "cache_read_usd": None, "cache_write_usd": None,
        "output_usd": None, "total_usd": None, "model_group": None,
    }
    if not provider or not model:
        return null
    try:
        from app.lib.llm.pricing import cost_columns_for
        cols = cost_columns_for(provider, model, {
            "input_tokens": it,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc,
            "output_tokens": ot,
        })
        return {
            "uncached_input_usd": cols["uncached_input_usd"],
            "cache_read_usd": cols["cache_read_usd"],
            "cache_write_usd": cols["cache_write_usd"],
            "output_usd": cols["output_usd"],
            "total_usd": cols["total_usd"],
            "model_group": cols["model_group"],
        }
    except Exception:  # noqa: BLE001 — migrations must never crash on pricing
        return null


def downgrade() -> None:
    bind = op.get_bind()
    if "usage_records" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("usage_records")
