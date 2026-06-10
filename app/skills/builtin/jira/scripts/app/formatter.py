"""Parse Jira issue resources and render list rows / event markdown."""
from __future__ import annotations

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


def received_at_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def adf_to_text(node: Any) -> str:
    """Flatten an ADF document/node to plain text (best effort)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out: list[str] = []
    if isinstance(node, dict):
        ntype = node.get("type")
        if ntype == "text":
            return node.get("text", "")
        if ntype == "hardBreak":
            return "\n"
        children = node.get("content", []) or []
        text = "".join(adf_to_text(c) for c in children)
        if ntype in ("paragraph", "heading", "blockquote", "listItem", "codeBlock"):
            return text + "\n"
        return text
    if isinstance(node, list):
        out = [adf_to_text(c) for c in node]
    return "".join(out)


def _name(obj: Any, field: str = "name") -> str:
    return obj.get(field, "") if isinstance(obj, dict) else ""


def parse_issue(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields", {}) or {}
    return {
        "key": issue.get("key", ""),
        "summary": f.get("summary", ""),
        "status": _name(f.get("status")),
        "type": _name(f.get("issuetype")),
        "assignee": _name(f.get("assignee"), "displayName"),
        "reporter": _name(f.get("reporter"), "displayName"),
        "priority": _name(f.get("priority")),
        "updated": f.get("updated", ""),
        "created": f.get("created", ""),
        "description": adf_to_text(f.get("description")).rstrip(),
    }


def issue_url(site_url: str, key: str) -> str:
    if not site_url or not key:
        return ""
    return f"{site_url.rstrip('/')}/browse/{key}"


def format_issue_markdown(issue: dict[str, Any], *, event_type: str = "issue_changed", site_url: str = "") -> str:
    m = parse_issue(issue)
    lines = [
        "---",
        f"key: {_yaml_quote(m['key'])}",
        f"summary: {_yaml_quote(m['summary'])}",
        f"status: {_yaml_quote(m['status'])}",
        f"type: {_yaml_quote(m['type'])}",
        f"assignee: {_yaml_quote(m['assignee'])}",
        f"reporter: {_yaml_quote(m['reporter'])}",
        f"priority: {_yaml_quote(m['priority'])}",
        f"updated: {_yaml_quote(m['updated'])}",
        f"url: {_yaml_quote(issue_url(site_url, m['key']))}",
        f"event_type: {_yaml_quote(event_type)}",
        f"received_at: {_yaml_quote(received_at_iso())}",
        "---",
        "",
        m["description"],
        "",
    ]
    return "\n".join(lines)


def format_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no issues)"
    parts = []
    for r in rows:
        block = [f"- [{r.get('key', '')}] {r.get('summary') or '(no summary)'}"]
        meta = []
        if r.get("status"):
            meta.append(f"status={r['status']}")
        if r.get("type"):
            meta.append(f"type={r['type']}")
        if r.get("assignee"):
            meta.append(f"assignee={r['assignee']}")
        if meta:
            block.append("  " + ", ".join(meta))
        parts.append("\n".join(block))
    return "\n".join(parts)
