"""System variables — `GET /api/system-vars`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.cli.client._base import Client


@dataclass(frozen=True)
class SystemVar:
    name: str
    description: str
    value: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SystemVar":
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            value=str(d.get("value") or ""),
        )


async def get_system_vars(client: Client) -> list[SystemVar]:
    data = await client.get_json("/api/system-vars")
    if not isinstance(data, list):
        return []
    return [SystemVar.from_dict(d) for d in data if isinstance(d, dict)]
