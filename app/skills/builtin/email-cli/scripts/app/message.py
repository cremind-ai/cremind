import email
import email.policy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional


@dataclass
class ParsedMessage:
    message_id: str = ""
    gm_msgid: str = ""
    from_: str = ""
    to: str = ""
    cc: str = ""
    subject: str = ""
    date: Optional[datetime] = None
    body_text: str = ""
    body_html: str = ""
    labels: list[str] = field(default_factory=list)
    raw_headers: dict[str, str] = field(default_factory=dict)

    @property
    def snippet(self) -> str:
        text = self.body_text or self.body_html or ""
        return " ".join(text.split())[:200]


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag in {"p", "br", "div", "tr", "li"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [line.strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def _safe_get_content(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, AssertionError):
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload or "")


def _extract_bodies(msg: EmailMessage) -> tuple[str, str]:
    text_body = ""
    html_body = ""
    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        text_body = _safe_get_content(plain_part)
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        html_body = _safe_get_content(html_part)
    return text_body, html_body


def _strip_html(html: str) -> str:
    if not html:
        return ""
    stripper = _HTMLStripper()
    try:
        stripper.feed(html)
        stripper.close()
    except Exception:
        return html
    return stripper.text()


def parse(raw_bytes: bytes, gm_msgid: str = "", labels: Optional[list[str]] = None) -> ParsedMessage:
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    headers = {}
    for key, value in msg.items():
        headers.setdefault(key, str(value))

    date_obj: Optional[datetime] = None
    date_header = msg["Date"]
    if date_header:
        try:
            date_obj = parsedate_to_datetime(str(date_header))
        except (TypeError, ValueError):
            date_obj = None

    text_body, html_body = _extract_bodies(msg)
    if not text_body and html_body:
        text_body = _strip_html(html_body)

    return ParsedMessage(
        message_id=str(msg["Message-ID"] or "").strip(),
        gm_msgid=gm_msgid,
        from_=str(msg["From"] or ""),
        to=str(msg["To"] or ""),
        cc=str(msg["Cc"] or ""),
        subject=str(msg["Subject"] or ""),
        date=date_obj,
        body_text=text_body,
        body_html=html_body,
        labels=labels or [],
        raw_headers=headers,
    )


def build_outgoing(
    *,
    from_addr: str,
    to: list[str],
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    in_reply_to: str = "",
    references: str = "",
) -> EmailMessage:
    msg = EmailMessage(policy=email.policy.default)
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=from_addr.split("@")[-1] if "@" in from_addr else "localhost")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    return msg


def build_reply(
    original: ParsedMessage,
    body: str,
    *,
    from_addr: str,
    extra_cc: Optional[list[str]] = None,
    extra_bcc: Optional[list[str]] = None,
) -> EmailMessage:
    subject = original.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}".strip()

    reply_to = original.raw_headers.get("Reply-To") or original.from_
    to = [reply_to] if reply_to else []

    references = original.raw_headers.get("References", "")
    if references and original.message_id:
        references = f"{references} {original.message_id}".strip()
    else:
        references = original.message_id

    return build_outgoing(
        from_addr=from_addr,
        to=to,
        subject=subject,
        body=body,
        cc=extra_cc,
        bcc=extra_bcc,
        in_reply_to=original.message_id,
        references=references,
    )


def received_at_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
