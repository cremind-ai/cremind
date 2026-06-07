from typing import Any

from .message import ParsedMessage, received_at_iso


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


def format_list_row(msg: ParsedMessage, detail: str) -> dict[str, Any]:
    base = {
        "message_id": msg.message_id,
        "subject": msg.subject,
        "from": msg.from_,
        "date": _iso(msg.date),
    }
    if detail == "title_only":
        return base
    base["snippet"] = msg.snippet
    base["to"] = msg.to
    if detail == "summary":
        return base
    base["cc"] = msg.cc
    base["body"] = msg.body_text
    base["labels"] = msg.labels
    if msg.gm_msgid:
        base["gm_msgid"] = msg.gm_msgid
    return base


def format_detail(msg: ParsedMessage) -> dict[str, Any]:
    return {
        "message_id": msg.message_id,
        "gm_msgid": msg.gm_msgid,
        "subject": msg.subject,
        "from": msg.from_,
        "to": msg.to,
        "cc": msg.cc,
        "date": _iso(msg.date),
        "labels": msg.labels,
        "body": msg.body_text,
        "body_html": msg.body_html,
    }


def _yaml_quote(value: str) -> str:
    if value is None or value == "":
        return '""'
    needs_quote = any(c in value for c in ":#&*!|>'\"%@`\\") or value.startswith(("-", "?", "!"))
    needs_quote = needs_quote or "\n" in value or value.strip() != value
    if not needs_quote:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def format_event_markdown(msg: ParsedMessage) -> str:
    labels_json = "[" + ", ".join(_yaml_quote(lbl) for lbl in msg.labels) + "]"
    lines = [
        "---",
        f"message_id: {_yaml_quote(msg.message_id)}",
        f"gm_msgid: {_yaml_quote(msg.gm_msgid)}",
        f"from: {_yaml_quote(msg.from_)}",
        f"to: {_yaml_quote(msg.to)}",
        f"cc: {_yaml_quote(msg.cc)}",
        f"subject: {_yaml_quote(msg.subject)}",
        f"date: {_yaml_quote(_iso(msg.date))}",
        f"received_at: {_yaml_quote(received_at_iso())}",
        f"labels: {labels_json}",
        "---",
        "",
        msg.body_text.rstrip() if msg.body_text else "",
        "",
    ]
    return "\n".join(lines)


def format_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no results)"
    lines = []
    for r in rows:
        parts = [
            f"- {r.get('subject') or '(no subject)'}",
            f"  From: {r.get('from','')}",
        ]
        if r.get("date"):
            parts.append(f"  Date: {r['date']}")
        if r.get("snippet"):
            parts.append(f"  Preview: {r['snippet'][:120]}")
        parts.append(f"  Message-ID: {r.get('message_id','')}")
        lines.append("\n".join(parts))
    return "\n".join(lines)
