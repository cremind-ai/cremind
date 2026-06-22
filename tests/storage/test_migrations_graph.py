"""Guard: the Alembic migration tree must have exactly one head.

A second head sneaks in when a migration is merged with a ``down_revision`` that
branches off an earlier revision instead of chaining onto the current tip (as
happened when the jira ``issue_changed`` split was merged onto the schedule
branch). ``cremind db upgrade`` and boot-time ``ensure_at_head`` both target
``head`` (singular) and abort on multiple heads, so this is release-blocking.
"""
from __future__ import annotations

from alembic.script import ScriptDirectory

from app.storage import migrations

# alembic_version.version_num is VARCHAR(32). Postgres enforces it; SQLite does
# not — so an over-long id passes the SQLite CI gate then truncates on a real
# Postgres upgrade. This test catches it at the gate.
ALEMBIC_VERSION_NUM_LEN = 32


def test_single_alembic_head():
    heads = migrations.heads()
    assert len(heads) == 1, (
        "Alembic migration tree must have exactly one head; found "
        f"{len(heads)}: {heads}. Re-parent the divergent migration's "
        "down_revision onto the current tip (or add a merge revision)."
    )


def test_revision_ids_fit_version_column():
    script = ScriptDirectory.from_config(migrations.build_alembic_config())
    offenders = {
        rev.revision: len(rev.revision)
        for rev in script.walk_revisions()
        if len(rev.revision) > ALEMBIC_VERSION_NUM_LEN
    }
    assert not offenders, (
        f"Alembic revision ids must be <= {ALEMBIC_VERSION_NUM_LEN} chars "
        f"(alembic_version.version_num is VARCHAR({ALEMBIC_VERSION_NUM_LEN}), "
        f"enforced on Postgres). Too long: {offenders}"
    )
