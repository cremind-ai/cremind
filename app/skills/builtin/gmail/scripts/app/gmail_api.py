"""Thin wrapper over the Gmail API (googleapiclient).

Sending is the only capability the shared Cremind OAuth client can offer: every
Gmail scope that reads a mailbox — including headers-only metadata — is
"restricted" by Google, as are the watch/history APIs behind push notifications.
So there is no event plane here, and ``list``/``get`` only work for a caller who
brought their own credentials (see cli.py).

All calls use the local user's own access token — the relay is never involved.
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any

GMAIL_SCOPE_HINT = "https://www.googleapis.com/auth/gmail.send"


def build_service(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --- actions ---

def list_messages(svc, *, query: str | None = None, max_results: int = 10, label_ids: list[str] | None = None) -> list[dict[str, Any]]:
    resp = (
        svc.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results, labelIds=label_ids)
        .execute()
    )
    return resp.get("messages", []) or []


def get_message(svc, message_id: str, *, fmt: str = "full") -> dict[str, Any]:
    return svc.users().messages().get(userId="me", id=message_id, format=fmt).execute()


def _mime_to_raw(msg: MIMEText) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def compose_reply_subject(subject: str) -> str:
    """Prefix ``Re: `` unless the subject already carries it.

    Matching subjects are part of how mail clients group a thread, so this runs on
    every reply — including the send-only path that has no original to copy from.
    """
    clean = (subject or "").strip()
    if not clean:
        return "Re:"
    return clean if clean.lower().startswith("re:") else f"Re: {clean}"


def send_message(
    svc,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    sender: str | None = None,
    thread_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    mime = MIMEText(body or "", _charset="utf-8")
    mime["To"] = ", ".join(to)
    mime["Subject"] = subject
    if cc:
        mime["Cc"] = ", ".join(cc)
    if bcc:
        mime["Bcc"] = ", ".join(bcc)
    if sender:
        mime["From"] = sender
    for k, v in (headers or {}).items():
        mime[k] = v
    request_body: dict[str, Any] = {"raw": _mime_to_raw(mime)}
    if thread_id:
        request_body["threadId"] = thread_id
    return svc.users().messages().send(userId="me", body=request_body).execute()


def reply_message(svc, *, message_id: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None) -> dict[str, Any]:
    """Reply in-thread to an existing message, looked up by Gmail message id.

    **Requires a Gmail read scope** for the lookup, so it only works with
    bring-your-own credentials. The send-only path builds the same headers from
    values the caller supplies (see ``cli.cmd_reply``).
    """
    original = get_message(svc, message_id, fmt="metadata")
    headers = {h["name"].lower(): h["value"] for h in original.get("payload", {}).get("headers", [])}
    rfc_msg_id = headers.get("message-id", "")
    subject = compose_reply_subject(headers.get("subject", ""))
    reply_to = headers.get("reply-to") or headers.get("from") or ""
    extra = {}
    if rfc_msg_id:
        extra["In-Reply-To"] = rfc_msg_id
        extra["References"] = rfc_msg_id
    return send_message(
        svc,
        to=[reply_to] if reply_to else [],
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        thread_id=original.get("threadId"),
        headers=extra,
    )
