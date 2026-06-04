"""Read install/_catalog.json so the TUI shares the same prompts as install.sh.

The catalog is produced by ``install/scripts/build_catalog.py`` from
``install/catalog.toml`` and emitted as ``_catalog.sh``, ``_catalog.ps1``,
and ``_catalog.json``. The TUI reads the JSON form so it doesn't have to
parse shell syntax — keeping deployment labels, custom-field prompts, and
mode descriptions consistent across the bash, PowerShell, and Python
front-ends.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CustomField:
    """One advanced field for the ``custom`` deployment.

    ``choices`` is empty for free-text fields and populated for radio
    fields (e.g. wizard_preset). ``key`` matches the shell-side
    ``CUSTOM_<key>`` variable name so the TUI can write back to it.
    """

    key: str
    prompt: str
    hint: str
    default: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Deployment:
    id: str
    label: str
    short: str
    description: str
    requires_host: bool
    advanced_fields: tuple[CustomField, ...] = ()


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    description: str
    hint: str
    badge: str = ""


@dataclass(frozen=True)
class Catalog:
    deployments: tuple[Deployment, ...] = ()
    modes: tuple[Mode, ...] = ()

    def deployment(self, deployment_id: str) -> Deployment | None:
        for d in self.deployments:
            if d.id == deployment_id:
                return d
        return None

    def mode(self, mode_id: str) -> Mode | None:
        for m in self.modes:
            if m.id == mode_id:
                return m
        return None


def load(path: str | Path) -> Catalog:
    """Load the catalog from a JSON file written by build_catalog.py."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    deployments_raw = data.get("deployments", {})
    deployments = []
    for dep_id, entry in sorted(
        deployments_raw.items(), key=lambda kv: kv[1].get("order", 0)
    ):
        fields: list[CustomField] = []
        for field_entry in entry.get("advanced_fields", []) or []:
            fields.append(
                CustomField(
                    key=field_entry["key"],
                    prompt=field_entry.get("prompt", ""),
                    hint=field_entry.get("hint", ""),
                    default=field_entry.get("default", ""),
                    choices=tuple(field_entry.get("choices", []) or []),
                )
            )
        deployments.append(
            Deployment(
                id=dep_id,
                label=entry.get("label", dep_id),
                short=entry.get("short", ""),
                description=entry.get("description", ""),
                requires_host=bool(entry.get("requires_host", False)),
                advanced_fields=tuple(fields),
            )
        )

    modes_raw = data.get("modes", {})
    modes = []
    for mode_id, entry in sorted(
        modes_raw.items(), key=lambda kv: kv[1].get("order", 0)
    ):
        modes.append(
            Mode(
                id=mode_id,
                label=entry.get("label", mode_id),
                description=entry.get("description", ""),
                hint=entry.get("hint", ""),
                badge=entry.get("badge", ""),
            )
        )

    return Catalog(deployments=tuple(deployments), modes=tuple(modes))
