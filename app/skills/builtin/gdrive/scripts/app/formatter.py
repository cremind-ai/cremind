"""Parse Google Drive file resources / change entries; render list rows and the
`file_changed` event markdown."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_MIME_LABELS = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.drawing": "Google Drawing",
    "application/vnd.google-apps.form": "Google Form",
    "application/vnd.google-apps.folder": "folder",
    "application/pdf": "PDF",
}


def mime_label(mime_type: str) -> str:
    return _MIME_LABELS.get(mime_type or "", (mime_type or "file"))


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


def parse_file(f: dict[str, Any]) -> dict[str, Any]:
    user = f.get("lastModifyingUser", {}) or {}
    last_by = ""
    if user:
        name = user.get("displayName", "")
        email = user.get("emailAddress", "")
        last_by = f"{name} ({email})".strip() if (name or email) else ""
    return {
        "id": f.get("id", ""),
        "name": f.get("name", ""),
        "mime_type": f.get("mimeType", ""),
        "parents": f.get("parents", []) or [],
        "created_time": f.get("createdTime", ""),
        "modified_time": f.get("modifiedTime", ""),
        "trashed": bool(f.get("trashed", False)),
        "size": f.get("size", ""),
        "web_view_link": f.get("webViewLink", ""),
        "last_modified_by": last_by,
    }


def classify_change(change: dict[str, Any], *, created_window_seconds: int = 300) -> str:
    """Stateless created/updated/trashed/removed hint for a change entry."""
    if change.get("removed"):
        return "removed"
    f = change.get("file", {}) or {}
    if f.get("trashed"):
        return "trashed"
    created = _parse_rfc3339(f.get("createdTime", ""))
    changed = _parse_rfc3339(change.get("time", ""))
    if created is not None and changed is not None and abs((changed - created).total_seconds()) <= created_window_seconds:
        return "created"
    return "updated"


def _parse_rfc3339(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_file_event_markdown(change: dict[str, Any]) -> str:
    kind = classify_change(change)
    f = change.get("file", {}) or {}
    m = parse_file(f)
    file_id = m["id"] or change.get("fileId", "")
    m["id"] = file_id

    if kind == "removed":
        name = m["name"] or file_id
        label = mime_label(m["mime_type"])
        body = f'File "{name}" ({label}) was removed from your Drive (deleted or access lost).'
    else:
        name = m["name"] or "(unknown)"
        label = mime_label(m["mime_type"])
        by = f" by {m['last_modified_by']}" if m["last_modified_by"] else ""
        verb = {"created": "created", "trashed": "moved to trash", "updated": "updated"}.get(kind, "changed")
        body = f'File "{name}" ({label}) was {verb}{by}.'
        if m["web_view_link"]:
            body += f"\nOpen: {m['web_view_link']}"

    lines = [
        "---",
        f"id: {_yaml_quote(file_id)}",
        f"name: {_yaml_quote(m['name'])}",
        f"mime_type: {_yaml_quote(m['mime_type'])}",
        f"change: {_yaml_quote(kind)}",
        f"parents: {_yaml_list(m['parents'])}",
        f"created_time: {_yaml_quote(m['created_time'])}",
        f"modified_time: {_yaml_quote(m['modified_time'])}",
        f"trashed: {'true' if m['trashed'] else 'false'}",
        f"removed: {'true' if kind == 'removed' else 'false'}",
        f"size: {_yaml_quote(m['size'])}",
        f"web_view_link: {_yaml_quote(m['web_view_link'])}",
        f"last_modified_by: {_yaml_quote(m['last_modified_by'])}",
        f"event_type: {_yaml_quote('file_changed')}",
        f"received_at: {_yaml_quote(received_at_iso())}",
        "---",
        "",
        body.rstrip(),
        "",
    ]
    return "\n".join(lines)


def event_title(change: dict[str, Any]) -> str:
    f = change.get("file", {}) or {}
    return f.get("name") or change.get("fileId", "") or "file"
