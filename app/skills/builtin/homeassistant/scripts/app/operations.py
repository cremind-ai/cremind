"""CLI verb implementations over the Home Assistant REST API."""
from __future__ import annotations

import fnmatch
from typing import Any, Optional

from . import auth, config, devices
from .homeassistant_api import HaRestClient


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _matches_filter(entity_id: str) -> bool:
    """True if the entity matches HA_ENTITY_FILTER (empty filter = all entities)."""
    if not config.HA_ENTITY_FILTER:
        return True
    return any(fnmatch.fnmatch(entity_id, p) for p in config.HA_ENTITY_FILTER)


def _friendly_name(state: dict) -> str:
    attrs = state.get("attributes") or {}
    return attrs.get("friendly_name") or state.get("entity_id", "")


def _entity_row(state: dict) -> dict:
    return {
        "entity_id": state.get("entity_id", ""),
        "state": state.get("state", ""),
        "friendly_name": _friendly_name(state),
        "domain": _domain(state.get("entity_id", "")),
    }


def _full_row(state: dict) -> dict:
    return {
        "entity_id": state.get("entity_id", ""),
        "state": state.get("state", ""),
        "attributes": state.get("attributes") or {},
        "last_changed": state.get("last_changed", ""),
        "last_updated": state.get("last_updated", ""),
    }


def _matches(entity_id: str, friendly: str, *, domain: Optional[str], query: Optional[str]) -> bool:
    if domain and _domain(entity_id) != domain:
        return False
    if query:
        q = query.lower()
        if q not in entity_id.lower() and q not in (friendly or "").lower():
            return False
    return True


def check() -> dict:
    with HaRestClient() as c:
        cfg = c.get_config()
        states = c.get_states()
    return {
        "ok": True,
        "ha_url": config.HA_URL,
        "auth": auth.auth_mode(),
        "version": cfg.get("version"),
        "location_name": cfg.get("location_name"),
        "entity_count": len(states),
    }


def list_entities(domain: Optional[str] = None, query: Optional[str] = None, max_results: int = 200) -> list[dict]:
    with HaRestClient() as c:
        states = c.get_states()
    rows = [_entity_row(s) for s in states]
    rows = [r for r in rows if _matches(r["entity_id"], r["friendly_name"], domain=domain, query=query)]
    rows.sort(key=lambda r: r["entity_id"])
    if max_results and max_results > 0:
        rows = rows[:max_results]
    return rows


def get_state(entity_id: str) -> dict:
    with HaRestClient() as c:
        s = c.get_state(entity_id)
    return _full_row(s)


def states(domain: Optional[str] = None, query: Optional[str] = None) -> list[dict]:
    with HaRestClient() as c:
        raw = c.get_states()
    rows = [_full_row(s) for s in raw]
    rows = [
        r for r in rows
        if _matches(r["entity_id"], _friendly_name({"attributes": r["attributes"], "entity_id": r["entity_id"]}),
                    domain=domain, query=query)
    ]
    rows.sort(key=lambda r: r["entity_id"])
    return rows


def sync_devices() -> dict:
    """Rebuild references/devices.md from current states (filtered by HA_ENTITY_FILTER).

    Lets the inventory be populated/repaired on demand, without the listener running."""
    with HaRestClient() as c:
        raw = c.get_states()
    rows = [devices.row_from_state(s) for s in raw if _matches_filter(s.get("entity_id") or "")]
    devices.full_sync(rows)
    return {"ok": True, "count": len(rows), "path": str(config.DEVICES_FILE)}


def call_service(domain: str, service: str, data: Optional[dict] = None, entity: Optional[str] = None) -> dict:
    payload: dict[str, Any] = dict(data or {})
    if entity:
        payload.setdefault("entity_id", entity)
    with HaRestClient() as c:
        result = c.call_service(domain, service, payload)
    return {
        "ok": True,
        "domain": domain,
        "service": service,
        "data": payload,
        "result": result,
    }
