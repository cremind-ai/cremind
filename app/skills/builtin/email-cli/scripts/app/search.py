from datetime import date
from typing import Optional


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_imap_date(value: str) -> str:
    """Convert YYYY-MM-DD to IMAP date format DD-Mon-YYYY."""
    d = date.fromisoformat(value)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _escape_quoted(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_list_criteria(
    *,
    query: Optional[str] = None,
    category: Optional[str] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    is_gmail: bool = False,
) -> list[str]:
    """Build IMAP SEARCH criteria tokens for a list operation.

    On Gmail servers (is_gmail=True), query + category are passed through the
    Gmail-specific X-GM-RAW extension, giving users full Gmail search grammar
    (e.g. 'from:alice has:attachment', 'category:primary').

    On standard IMAP servers, the query is split into whitespace-separated terms
    and each term is matched against the full message via TEXT (the IMAP primitive
    that searches headers + body). Category is ignored as it's Gmail-specific.

    Returns a flat list of strings suitable for
    `imaplib.IMAP4.uid("SEARCH", None, *tokens)`. Empty list becomes "ALL".
    """
    tokens: list[str] = []

    if is_gmail:
        gm_raw_parts: list[str] = []
        if category and category not in {"spam", "all"}:
            gm_raw_parts.append(f"category:{category}")
        if query:
            gm_raw_parts.append(query.strip())
        if gm_raw_parts:
            joined = " ".join(gm_raw_parts)
            tokens.extend(["X-GM-RAW", f'"{_escape_quoted(joined)}"'])
    else:
        if query:
            for term in query.strip().split():
                if term:
                    tokens.extend(["TEXT", f'"{_escape_quoted(term)}"'])

    if since:
        tokens.extend(["SINCE", _fmt_imap_date(since)])
    if before:
        tokens.extend(["BEFORE", _fmt_imap_date(before)])

    if not tokens:
        tokens.append("ALL")
    return tokens
