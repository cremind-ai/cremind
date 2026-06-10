"""Thin wrapper over the Gmail API (googleapiclient).

Only the verbs the skill needs: profile/watch (event plane), and
list/get/send/reply/trash/modify/history (actions + incremental sync). All calls
use the local user's own access token — the relay is never involved here.
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

GMAIL_SCOPE_HINT = "https://www.googleapis.com/auth/gmail.modify (+ gmail.send)"


def build_service(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --- event plane ---

def get_profile(svc) -> dict[str, Any]:
    return svc.users().getProfile(userId="me").execute()


def watch(svc, topic_name: str) -> dict[str, Any]:
    body = {
        "topicName": topic_name,
        "labelIds": ["INBOX"],
        "labelFilterBehavior": "INCLUDE",
    }
    return svc.users().watch(userId="me", body=body).execute()


def stop_watch(svc) -> None:
    svc.users().stop(userId="me").execute()


def list_history(svc, start_history_id: str) -> list[dict[str, Any]]:
    """Return history records (messageAdded in INBOX) since start_history_id."""
    records: list[dict[str, Any]] = []
    page_token = None
    while True:
        resp = (
            svc.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
                pageToken=page_token,
            )
            .execute()
        )
        records.extend(resp.get("history", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return records


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


def trash_message(svc, message_id: str) -> dict[str, Any]:
    return svc.users().messages().trash(userId="me", id=message_id).execute()


def modify_message(svc, message_id: str, *, add: list[str] | None = None, remove: list[str] | None = None) -> dict[str, Any]:
    body = {"addLabelIds": add or [], "removeLabelIds": remove or []}
    return svc.users().messages().modify(userId="me", id=message_id, body=body).execute()


def _mime_to_raw(msg: MIMEText) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


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
    """Reply in-thread to an existing message (looked up by Gmail message id)."""
    original = get_message(svc, message_id, fmt="metadata")
    headers = {h["name"].lower(): h["value"] for h in original.get("payload", {}).get("headers", [])}
    rfc_msg_id = headers.get("message-id", "")
    subject = headers.get("subject", "")
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
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
