"""What may reach a tag: allowlisted payload builders, redaction, excerpts.

Every journal entry is built here, from named fields only — never a row diff,
never reasoning, tool output or terminal output. Free text passes through
:func:`redact`, which removes:

- one-time codes: a 4–8 digit number next to ``otp`` / ``code`` / ``passcode``
  / ``pin`` / ``verification`` (either side);
- bearer / API tokens (``Bearer …``, ``CremindTag …``, ``sk-…``, ``ghp_…``,
  ``xox…``, ``AIza…``, ``tagc_…``, JWTs);
- ``key=…`` / ``password: …`` style pairs;
- base64 runs ≥ 24 characters (mixed case and digits) and hex runs ≥ 24.

A notification that carries an OTP is never journalled at all
(:func:`notification_entry`), and the REST ``display`` path refuses text that
:func:`contains_otp` flags (422 ``otp_refused``).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.tags.journal import JournalEntry, TurnContext

# ── redaction ────────────────────────────────────────────────────────────────

_OTP_WORDS = (
    r"(?:otp|one[\s-]?time(?:\s+(?:code|password|passcode|pin))?|pass\s?code|pin|"
    r"verification(?:\s+code)?|security\s+code|login\s+code|auth(?:entication)?\s+code|"
    r"2fa|mfa|code|m[aã]\s+(?:x[aá]c\s+(?:nh[aậ]n|th[uự]c)|otp))"
)
_OTP_DIGITS = r"(\d{4,8}|\d{3}[\s-]\d{3})"
_OTP_AFTER = re.compile(rf"(?i)\b{_OTP_WORDS}\b[^\d\n]{{0,24}}?{_OTP_DIGITS}(?!\d)")
_OTP_BEFORE = re.compile(rf"(?i)(?<!\d){_OTP_DIGITS}\b[^\d\n]{{0,24}}?\b{_OTP_WORDS}\b")

_SCHEME_TOKEN = re.compile(r"(?i)\b(bearer|cremindtag|basic|token)\s+[A-Za-z0-9._~+/=:-]{8,}")
_KNOWN_TOKENS = re.compile(
    r"\b(?:"
    r"(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|tagc_[a-z0-9]{10,}(?:\.[A-Za-z0-9_-]+)?"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)
_KEY_VALUE = re.compile(
    r"(?i)\b([\w.-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|token|secret|password|passwd|pwd|"
    r"passcode|key))\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{24,}\b")
_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")

REDACTED = "[redacted]"
_OTP_MASK = "••••"


def contains_otp(text: Any) -> bool:
    """Whether ``text`` looks like it carries a one-time code."""
    if not isinstance(text, str) or not text:
        return False
    return bool(_OTP_AFTER.search(text) or _OTP_BEFORE.search(text))


def _mask_otp(match: re.Match) -> str:
    whole = match.group(0)
    digits = match.group(1)
    return whole.replace(digits, _OTP_MASK)


def _b64_sub(match: re.Match) -> str:
    run = match.group(0)
    upper = sum(c.isupper() for c in run)
    lower = sum(c.islower() for c in run)
    digits = sum(c.isdigit() for c in run)
    if upper >= 2 and lower >= 2 and digits >= 2:
        return REDACTED
    return run


def redact(text: Any) -> str:
    """Remove codes, tokens, secrets and long opaque runs from ``text``."""
    if not isinstance(text, str) or not text:
        return ""
    out = _OTP_AFTER.sub(_mask_otp, text)
    out = _OTP_BEFORE.sub(_mask_otp, out)
    out = _SCHEME_TOKEN.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _KNOWN_TOKENS.sub(REDACTED, out)
    out = _KEY_VALUE.sub(lambda m: f"{m.group(1)}={REDACTED}", out)
    out = _HEX_RUN.sub(REDACTED, out)
    out = _B64_RUN.sub(_b64_sub, out)
    return out


# ── excerpts ─────────────────────────────────────────────────────────────────

_CODE_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_CITATION = re.compile(r"\[(?:doc|ud):[^\]\s]{1,40}\]")
_MD_MARKERS = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_WS = re.compile(r"\s+")


def clean_text(text: Any, limit: int) -> str:
    """One line of plain, redacted text of at most ``limit`` characters."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = _WS.sub(" ", text).strip()
    text = redact(text)
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


_HSPACE = re.compile(r"[ \t\f\v ]+")


def clean_multiline(text: Any, limit: int) -> str:
    """Body text that keeps its paragraphs: line breaks stay, a run of blank
    lines becomes one, trailing whitespace goes, runs of spaces inside a line
    become one (leading indentation is kept, tabs as two spaces), and every
    line is redacted on its own. At most ``limit`` characters."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        raw = raw.rstrip()
        body = raw.lstrip(" \t")
        indent = raw[: len(raw) - len(body)].replace("\t", "  ")
        line = redact(_HSPACE.sub(" ", body))
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(indent + line)
    while lines and lines[-1] == "":
        lines.pop()
    out = "\n".join(lines)
    if len(out) > limit:
        out = out[: max(0, limit - 1)].rstrip() + "…"
    return out


def excerpt(text: Any, limit: int = 280) -> str:
    """The excerpt policy: prose only — code blocks dropped, markdown and
    citation tokens stripped, secrets redacted, at most ``limit`` characters."""
    if not isinstance(text, str) or not text:
        return ""
    text = _CODE_FENCE.sub(" ", text)
    text = _CITATION.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MD_MARKERS.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    return clean_text(text, limit)


def mask_sender(sender_id: Any, display_name: Any = None) -> str:
    """``"J… (…4567)"`` — enough to recognise someone, not to contact them."""
    name = str(display_name or "").strip()
    sid = str(sender_id or "").strip()
    tail = sid[-4:] if len(sid) > 4 else ""
    head = f"{name[0]}…" if name else "Someone"
    return f"{head} (…{tail})" if tail else head


# ── builders ─────────────────────────────────────────────────────────────────

_NEEDS_INPUT_STAGES = ("awaiting_answers", "awaiting_approval")
_RESOLVING_STAGES = ("cancelled", "executing", "completed")


def _first_question(plan: dict[str, Any]) -> str:
    stage = plan.get("stage")
    if stage == "awaiting_approval":
        title = ((plan.get("plan") or {}).get("title") or "").strip()
        return f"Approve plan: {title}" if title else "A plan is waiting for approval"
    for q in plan.get("questions") or []:
        if isinstance(q, dict):
            text = q.get("question") or q.get("text") or q.get("prompt")
        else:
            text = q
        if isinstance(text, str) and text.strip():
            return text
    return "Questions are waiting for your answers"


def turn_entries(
    turn: TurnContext, *, conversation_id: str, message_id: str, role: str,
    content: str | None, metadata: dict[str, Any] | None,
) -> list[JournalEntry]:
    """Entries for one persisted message of a ``chat`` conversation.

    Event-run and group-chat seats never produce ``assistant.result`` (runs
    report through ``run.*``; a seat's answer is a room post)."""
    if turn.conversation_kind != "chat" or not turn.profile:
        return []
    title = clean_text(turn.conversation_title or "Untitled Chat", 120)
    plan = (metadata or {}).get("plan_mode")
    stage = plan.get("stage") if isinstance(plan, dict) else None
    key = f"chat:{conversation_id}"
    out: list[JournalEntry] = []
    if role == "agent" and turn.result:
        out.append(JournalEntry(
            kind="assistant.result",
            payload={
                "conversation_id": conversation_id,
                "title": title,
                "message_id": message_id,
                "errored": bool(turn.errored),
                "cancelled": bool(turn.cancelled),
                "excerpt": excerpt(content, 280),
            },
            source_type="conversation",
            source_id=conversation_id,
            replace_key=f"{key}:result",
        ))
    if role == "agent" and stage in _NEEDS_INPUT_STAGES and not turn.cancelled:
        out.append(JournalEntry(
            kind="chat.needs_input",
            payload={
                "conversation_id": conversation_id,
                "title": title,
                "stage": stage,
                "question": clean_text(_first_question(plan), 200),
            },
            source_type="conversation",
            source_id=conversation_id,
            replace_key=f"{key}:input",
        ))
    elif stage in _RESOLVING_STAGES:
        out.append(JournalEntry(
            kind="chat.needs_input_resolved",
            payload={"conversation_id": conversation_id, "title": title, "stage": stage},
            source_type="conversation",
            source_id=conversation_id,
            replace_key=f"{key}:input",
        ))
    return out


_TERMINAL_RUN = ("completed", "failed", "cancelled")


def run_entries(
    *, run_id: str, label: str, prior_status: str | None, new_status: str | None,
    pending_question: str | None = None, error: str | None = None,
) -> list[JournalEntry]:
    """Entries for one event-run status transition (``prior_status`` None =
    the run was just created)."""
    if not new_status or new_status == prior_status and new_status != "pending":
        return []
    title = clean_text(label or "Automation", 120)
    key = f"run:{run_id}"
    base = {"run_id": run_id, "title": title}
    out: list[JournalEntry] = []

    def entry(kind: str, payload: dict[str, Any], replace_key: str) -> JournalEntry:
        return JournalEntry(kind=kind, payload=payload, source_type="event_run",
                            source_id=run_id, replace_key=replace_key)

    if new_status == "running":
        if prior_status == "pending":
            out.append(entry("run.resumed", dict(base), f"{key}:input"))
        out.append(entry("run.started", dict(base), key))
    elif new_status == "pending":
        question = clean_text(pending_question or "", 200)
        if prior_status == "pending" and not question:
            return []
        out.append(entry("run.needs_input", {**base, "question": question or "Waiting for your reply"},
                         f"{key}:input"))
    elif new_status in _TERMINAL_RUN:
        kind = "run.failed" if new_status == "failed" else "run.completed"
        payload = {
            **base,
            "status": new_status,
            "error": clean_text(error, 200) if new_status == "failed" and error else None,
            "resolves_input": prior_status == "pending",
        }
        out.append(entry(kind, payload, key))
    return out


def channel_entry(kind: str, *, channel: dict[str, Any], error: Any = None) -> JournalEntry:
    """``channel.failed`` / ``channel.unlinked`` / ``channel.recovered``."""
    ctype = str(channel.get("channel_type") or "channel")
    cid = str(channel.get("id") or "")
    return JournalEntry(
        kind=kind,
        payload={
            "channel_id": cid,
            "name": ctype.replace("_", " ").title(),
            "type": ctype,
            "mode": str(channel.get("mode") or ""),
            "error": clean_text(error, 200) if error else None,
        },
        source_type="channel",
        source_id=cid,
        replace_key=f"channel:{cid}",
    )


def automation_failed_entry(*, automation_kind: str, name: Any, error: Any,
                            source_id: str | None) -> JournalEntry:
    return JournalEntry(
        kind="automation.failed",
        payload={
            "automation_kind": automation_kind,
            "name": clean_text(name or automation_kind, 120),
            "error": clean_text(error, 200) if error else None,
        },
        source_type=automation_kind,
        source_id=source_id,
        replace_key=f"automation:{automation_kind}:{source_id or 'unknown'}",
    )


def subscription_entry(*, channel_id: str, channel_type: str, sender_id: Any,
                       display_name: Any, subscribed: bool) -> JournalEntry:
    return JournalEntry(
        kind="subscription.changed",
        payload={
            "channel_id": channel_id,
            "channel_type": channel_type,
            "sender": mask_sender(sender_id, display_name),
            "subscribed": bool(subscribed),
        },
        source_type="channel",
        source_id=channel_id,
    )


def access_change_entries(*, channel_id: Any, channel_type: Any, sender: dict[str, Any] | None,
                          subscribed: bool) -> list[JournalEntry]:
    """``subscription.changed`` when a sender's ACCESS flag really flips;
    nothing when it already had that value (an OTP issue never calls this)."""
    sender = sender or {}
    if bool(sender.get("authenticated")) == bool(subscribed):
        return []
    return [subscription_entry(
        channel_id=str(channel_id or ""), channel_type=str(channel_type or "channel"),
        sender_id=sender.get("sender_id"), display_name=sender.get("display_name"),
        subscribed=subscribed,
    )]


# Notification kinds a dedicated journal kind already covers (the card would
# arrive twice), plus the OTP relay, which never reaches a tag.
_NOTIFICATION_SKIP = frozenset({
    "started", "completed", "error",
    "event_run_pending", "event_run_completed", "event_run_failed",
    "autostart_failed", "channel_disabled",
    "channel_otp",
})


def _strings(values: Iterable[Any]) -> Iterable[str]:
    for value in values:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            yield from _strings(value.values())
        elif isinstance(value, (list, tuple)):
            yield from _strings(value)


def notification_entry(entry: dict[str, Any]) -> JournalEntry | None:
    """The journal entry for a notifications-buffer push, or ``None`` when it
    must not reach a tag (an OTP, or a kind journalled on its own)."""
    kind = str(entry.get("kind") or "")
    if not kind or kind in _NOTIFICATION_SKIP:
        return None
    if any("otp" in str(k).lower() for k in entry):
        return None
    if any(contains_otp(s) for s in _strings(entry.values())):
        return None
    return JournalEntry(
        kind="notification",
        payload={
            "kind": kind,
            "title": clean_text(entry.get("conversation_title") or kind.replace("_", " "), 120),
            "preview": clean_text(entry.get("message_preview"), 280),
            "priority": "high" if entry.get("priority") == "high" else "normal",
            "conversation_id": str(entry.get("conversation_id") or "") or None,
        },
        source_type="notification",
        source_id=str(entry.get("id") or "") or None,
    )
