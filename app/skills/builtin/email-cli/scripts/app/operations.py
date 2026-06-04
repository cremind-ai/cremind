from typing import Any, Optional

from . import config, formatter, message, search
from .imap_client import ImapClient, ImapError
from .smtp_client import SmtpClient


CATEGORIES = {"primary", "promotions", "social", "updates", "forums", "spam", "all"}
DETAIL_LEVELS = {"title_only", "summary", "full"}


def _pick_list_folder(c: ImapClient, category: str) -> str:
    if category == "spam":
        return c.spam_folder or "INBOX"
    if category == "all":
        return c.all_mail_folder or "INBOX"
    return "INBOX"


def list_emails(
    *,
    max_results: int = 10,
    query: Optional[str] = None,
    detail: str = "summary",
    category: str = "primary",
    since: Optional[str] = None,
    before: Optional[str] = None,
) -> list[dict[str, Any]]:
    if detail not in DETAIL_LEVELS:
        raise ValueError(f"detail must be one of {sorted(DETAIL_LEVELS)}")
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {sorted(CATEGORIES)}")

    with ImapClient() as c:
        folder = _pick_list_folder(c, category)
        c.select(folder, readonly=True)

        criteria = search.build_list_criteria(
            query=query, category=category, since=since, before=before,
            is_gmail=c.is_gmail,
        )
        uids = c.uid_search(*criteria)

        # Gmail-only fallback: if the account has category tabs disabled,
        # 'category:primary' returns nothing. Retry without it.
        if not uids and category == "primary" and c.is_gmail:
            criteria = search.build_list_criteria(
                query=query, category=None, since=since, before=before,
                is_gmail=True,
            )
            uids = c.uid_search(*criteria)

        uids = list(reversed(uids))[:max_results]

        rows = []
        for uid in uids:
            try:
                raw, gm_msgid, labels = c.fetch_raw(uid)
            except ImapError:
                continue
            parsed = message.parse(raw, gm_msgid=gm_msgid, labels=labels)
            rows.append(formatter.format_list_row(parsed, detail))
        return rows


def list_sent_emails(
    *,
    max_results: int = 10,
    since: Optional[str] = None,
    before: Optional[str] = None,
) -> list[dict[str, Any]]:
    with ImapClient() as c:
        if not c.sent_folder:
            raise ImapError(
                "No Sent folder advertised by this IMAP server. "
                "Ensure your provider exposes a \\Sent special-use mailbox via LIST."
            )
        c.select(c.sent_folder, readonly=True)
        criteria = search.build_list_criteria(
            since=since, before=before, is_gmail=c.is_gmail
        )
        uids = c.uid_search(*criteria)
        uids = list(reversed(uids))[:max_results]
        rows = []
        for uid in uids:
            try:
                raw, gm_msgid, labels = c.fetch_raw(uid)
            except ImapError:
                continue
            parsed = message.parse(raw, gm_msgid=gm_msgid, labels=labels)
            rows.append(formatter.format_list_row(parsed, "summary"))
        return rows


def send_email(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
) -> dict[str, Any]:
    if not to:
        raise ValueError("At least one recipient (--to) is required")
    user, _ = config.require_credentials()
    msg = message.build_outgoing(
        from_addr=user,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
    )
    with SmtpClient() as s:
        s.send(msg)

    saved_folder: Optional[str] = None
    try:
        with ImapClient() as c:
            saved_folder = c.append_to_sent(msg.as_bytes())
    except Exception:
        saved_folder = None

    return {
        "ok": True,
        "message_id": msg["Message-ID"],
        "to": to,
        "subject": subject,
        "saved_to_sent": saved_folder,
    }


def reply_to_email(
    *,
    message_id: str,
    body: str,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
) -> dict[str, Any]:
    user, _ = config.require_credentials()
    saved_folder: Optional[str] = None
    with ImapClient() as c:
        uid, folder = c.find_uid_by_message_id(message_id)
        raw, gm_msgid, labels = c.fetch_raw(uid)
        parsed = message.parse(raw, gm_msgid=gm_msgid, labels=labels)
        reply_msg = message.build_reply(
            parsed, body, from_addr=user, extra_cc=cc, extra_bcc=bcc
        )
        with SmtpClient() as s:
            s.send(reply_msg)
        try:
            saved_folder = c.append_to_sent(reply_msg.as_bytes())
        except Exception:
            saved_folder = None

    return {
        "ok": True,
        "message_id": reply_msg["Message-ID"],
        "in_reply_to": parsed.message_id,
        "subject": reply_msg["Subject"],
        "folder_found": folder,
        "saved_to_sent": saved_folder,
    }


def trash_email(*, message_id: str) -> dict[str, Any]:
    with ImapClient() as c:
        uid, folder = c.find_uid_by_message_id(message_id)
        c.move_to_trash(uid)
    return {"ok": True, "message_id": message_id, "folder": folder}


def get_email_details(*, message_id: str) -> dict[str, Any]:
    with ImapClient() as c:
        uid, folder = c.find_uid_by_message_id(message_id)
        raw, gm_msgid, labels = c.fetch_raw(uid)
    parsed = message.parse(raw, gm_msgid=gm_msgid, labels=labels)
    out = formatter.format_detail(parsed)
    out["folder"] = folder
    return out
