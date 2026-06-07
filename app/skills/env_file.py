"""Materialize a skill's configured variables into its ``scripts/.env``.

Skill scripts are launched via the generic exec_shell tool, which has no
per-skill hook to inject environment variables — so a skill's configuration
reaches its scripts only through ``{skill_dir}/scripts/.env``. This module
writes that file from the persisted (SQLite) variables. It is used both when the
user saves variables (``app/api/tools.py``) and on every boot after the built-in
skills are re-synced (``app/skills/sync.py``), so user overrides survive restarts
and any stale shipped defaults are cleared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

from app.utils.logger import logger


def escape_env_value(value: str) -> str:
    """Quote a value for safe inclusion in a ``.env`` file.

    Wraps in double quotes and escapes embedded ``"`` / ``\\`` if the value
    contains whitespace, ``#``, or quotes; otherwise returns it as-is.
    """
    if value == "":
        return ""
    needs_quoting = any(ch in value for ch in (" ", "\t", "\n", "\r", "#", '"', "'", "\\", "$"))
    if not needs_quoting:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_skill_env_file(
    scripts_dir: Path,
    declared: Iterable[str],
    variables: Mapping[str, str],
) -> None:
    """Overwrite ``{scripts_dir}/.env`` with declared, non-empty variables.

    Overwrites the file so deletions in the DB also disappear from disk, and an
    empty result clears any stale shipped values. Only variables named in
    ``declared`` are written — stray rows are ignored.
    """
    try:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        env_path = scripts_dir / ".env"
        declared_set = set(declared)
        lines = [
            f"{k}={escape_env_value(str(v))}"
            for k, v in variables.items()
            if k in declared_set and v != ""
        ]
        body = "\n".join(lines) + ("\n" if lines else "")
        env_path.write_text(body, encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.exception(f"Failed to write skill .env at {scripts_dir}")
