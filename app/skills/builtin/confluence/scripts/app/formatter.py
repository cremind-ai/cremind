"""Parse Confluence resources and render list rows."""
from __future__ import annotations

from typing import Any

from .confluence_api import storage_to_text


def parse_page(page: dict[str, Any]) -> dict[str, Any]:
    body = ""
    b = page.get("body") or {}
    storage = b.get("storage") or {}
    if storage.get("value"):
        body = storage_to_text(storage["value"])
    return {
        "id": page.get("id", ""),
        "title": page.get("title", ""),
        "space_id": page.get("spaceId", ""),
        "status": page.get("status", ""),
        "version": (page.get("version") or {}).get("number", ""),
        "body": body,
    }


def page_url(site_url: str, page: dict[str, Any]) -> str:
    links = page.get("_links") or {}
    webui = links.get("webui") or ""
    if site_url and webui:
        return f"{site_url.rstrip('/')}/wiki{webui}"
    return ""


def format_space_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no spaces)"
    return "\n".join(f"- [{r.get('key', '')}] {r.get('name') or '(unnamed)'} (id={r.get('id', '')})" for r in rows)


def format_page_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no pages)"
    return "\n".join(f"- {r.get('title') or '(untitled)'} (id={r.get('id', '')})" for r in rows)


def format_search_results(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no results)"
    parts = []
    for r in rows:
        content = r.get("content") or {}
        title = content.get("title") or r.get("title") or "(untitled)"
        cid = content.get("id") or ""
        ctype = content.get("type") or ""
        parts.append(f"- {title} (id={cid}, type={ctype})")
    return "\n".join(parts)
