"""Parse Gmail message resources and render list rows / event markdown."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any


def _yaml_quote(value: Any) -> str:
    s = "" if value is None else str(value)
    if s == "":
        return '""'
    needs = any(c in s for c in ":#&*!|>'\"%@`\\") or s.startswith(("-", "?", "!"))
    needs = needs or "\n" in s or s.strip() != s
    if not needs:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _yaml_list(values) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_quote(v) for v in values) + "]"


def received_at_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _b64url_decode(data: str) -> bytes:
    data = data or ""
    data += "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data.encode("ascii"))


def _headers_map(payload: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}


_TAG_RE = re.compile(r"<[^>]+>")


def _extract_body(payload: dict[str, Any]) -> str:
    """Depth-first search for a text/plain part; fall back to stripped text/html."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")
    if mime == "text/plain" and data:
        return _b64url_decode(data).decode("utf-8", errors="replace")
    html_fallback = ""
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text and part.get("mimeType") == "text/plain":
            return text
        if text and part.get("mimeType") == "text/html" and not html_fallback:
            html_fallback = text
    if mime == "text/html" and data:
        html_fallback = _b64url_decode(data).decode("utf-8", errors="replace")
    if html_fallback:
        return _TAG_RE.sub("", html_fallback)
    return ""


def parse_message(msg: dict[str, Any]) -> dict[str, Any]:
    payload = msg.get("payload", {}) or {}
    h = _headers_map(payload)
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "message_id": h.get("message-id", ""),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "cc": h.get("cc", ""),
        "subject": h.get("subject", ""),
        "date": h.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "labels": msg.get("labelIds", []) or [],
        "body": _extract_body(payload),
    }


def format_email_markdown(m: dict[str, Any], *, event_type: str = "new_email") -> str:
    body = str(m.get("body") or m.get("snippet") or "")
    lines = [
        "---",
        f"id: {_yaml_quote(m.get('id', ''))}",
        f"thread_id: {_yaml_quote(m.get('thread_id', ''))}",
        f"message_id: {_yaml_quote(m.get('message_id', ''))}",
        f"from: {_yaml_quote(m.get('from', ''))}",
        f"to: {_yaml_quote(m.get('to', ''))}",
        f"cc: {_yaml_quote(m.get('cc', ''))}",
        f"subject: {_yaml_quote(m.get('subject', ''))}",
        f"date: {_yaml_quote(m.get('date', ''))}",
        f"labels: {_yaml_list(m.get('labels') or [])}",
        f"event_type: {_yaml_quote(event_type)}",
        f"received_at: {_yaml_quote(received_at_iso())}",
        "---",
        "",
        body.rstrip(),
        "",
    ]
    return "\n".join(lines)


def format_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no messages)"
    parts = []
    for r in rows:
        block = [f"- {r.get('subject') or '(no subject)'}"]
        if r.get("from"):
            block.append(f"  From: {r['from']}")
        if r.get("date"):
            block.append(f"  Date: {r['date']}")
        block.append(f"  Id: {r.get('id', '')}")
        if r.get("snippet"):
            block.append(f"  {r['snippet']}")
        parts.append("\n".join(block))
    return "\n".join(parts)
