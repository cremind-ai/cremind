"""Single source of truth for the Cremind version.

Read by ``pyproject.toml`` (via ``tool.hatch.version``) and by the runtime
``/version`` endpoint, the ``cremind version`` CLI, and the upgrader. Bump this
on release and the package metadata, the API response, and the CLI output
all move together.

Schema migrations are tracked separately by Alembic — see ``alembic/`` at the
repo root. ``MIN_SUPPORTED_UPGRADE_FROM`` is the oldest version this build
knows how to migrate from; the upgrader refuses to proceed when the live
install is older than this.

NOTE on ``"0.0.0"``: the upgrade floor accepts any install. Bump it the
next time Alembic introduces a migration that genuinely requires a minimum
schema version, not before.
"""

__version__ = "0.0.5"
MIN_SUPPORTED_UPGRADE_FROM = "0.0.0"
