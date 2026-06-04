import imaplib
import re
import ssl
import time
from typing import Optional

from . import config


_LIST_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>"(?:[^"\\]|\\.)*"|[^\s]+)\s*$'
)


def _parse_list_line(line: bytes) -> tuple[list[str], str, str]:
    m = _LIST_RE.match(line)
    if not m:
        return [], "", ""
    flags = m.group("flags").decode("utf-8", errors="replace").split()
    delim = m.group("delim").decode("utf-8", errors="replace")
    name = m.group("name").decode("utf-8", errors="replace")
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return flags, delim, name


def _quote_mailbox(name: str) -> str:
    if any(c.isspace() for c in name) or '"' in name or "[" in name or "/" in name:
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return name


class ImapError(RuntimeError):
    pass


class ImapClient:
    def __init__(self) -> None:
        self.conn: Optional[imaplib.IMAP4_SSL] = None
        self.capabilities: tuple[str, ...] = ()
        self.is_gmail: bool = False
        self.sent_folder: Optional[str] = None
        self.trash_folder: Optional[str] = None
        self.all_mail_folder: Optional[str] = None
        self.spam_folder: Optional[str] = None
        self._selected: Optional[str] = None
        self._uidvalidity: Optional[int] = None

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        user, pwd = config.require_credentials()
        ctx = ssl.create_default_context()
        self.conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, ssl_context=ctx)
        try:
            self.conn.login(user, pwd)
        except imaplib.IMAP4.error as e:
            raise ImapError(
                f"IMAP login failed for {user}@{config.IMAP_HOST}: {e}. "
                "Verify USERNAME and PASSWORD. Most providers require an 'App Password' "
                "(not your regular login password) when 2-Factor Authentication is enabled."
            ) from e
        self._detect_capabilities()
        self._discover_folders()

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            if self._selected is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn.logout()
        except Exception:
            pass
        finally:
            self.conn = None
            self._selected = None

    def _detect_capabilities(self) -> None:
        assert self.conn is not None
        typ, data = self.conn.capability()
        if typ == "OK" and data and data[0]:
            caps_text = data[0].decode("utf-8", errors="replace")
            self.capabilities = tuple(c.upper() for c in caps_text.split())
        else:
            self.capabilities = tuple(c.upper() for c in getattr(self.conn, "capabilities", ()))
        self.is_gmail = "X-GM-EXT-1" in self.capabilities

    def _discover_folders(self) -> None:
        assert self.conn is not None
        typ, data = self.conn.list()
        if typ != "OK" or not data:
            return
        for raw in data:
            if raw is None:
                continue
            if isinstance(raw, tuple):
                raw = raw[0] + b" " + (raw[1] or b"")
            flags, _delim, name = _parse_list_line(raw)
            if not name:
                continue
            if r"\Sent" in flags:
                self.sent_folder = name
            if r"\Trash" in flags:
                self.trash_folder = name
            if r"\All" in flags:
                self.all_mail_folder = name
            if r"\Junk" in flags:
                self.spam_folder = name

    def select(self, mailbox: str, readonly: bool = False) -> None:
        assert self.conn is not None
        typ, data = self.conn.select(_quote_mailbox(mailbox), readonly=readonly)
        if typ != "OK":
            raise ImapError(f"SELECT {mailbox} failed: {data!r}")
        self._selected = mailbox
        resp = self.conn.response("UIDVALIDITY")
        try:
            self._uidvalidity = int(resp[1][0])  # type: ignore[index,arg-type]
        except (TypeError, ValueError, IndexError):
            self._uidvalidity = None

    @property
    def uidvalidity(self) -> Optional[int]:
        return self._uidvalidity

    def uidnext(self) -> Optional[int]:
        assert self.conn is not None
        if self._selected is None:
            return None
        typ, data = self.conn.status(_quote_mailbox(self._selected), "(UIDNEXT)")
        if typ != "OK" or not data:
            return None
        try:
            raw = data[0].decode("utf-8", errors="replace")
            m = re.search(r"UIDNEXT\s+(\d+)", raw)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return None

    def uid_search(self, *criteria: str) -> list[int]:
        assert self.conn is not None
        typ, data = self.conn.uid("SEARCH", None, *criteria)
        if typ != "OK":
            raise ImapError(f"UID SEARCH failed: {data!r}")
        if not data or not data[0]:
            return []
        return [int(tok) for tok in data[0].split()]

    def fetch_raw(self, uid: int) -> tuple[bytes, str, list[str]]:
        """Fetch the full RFC822 body plus provider-specific metadata for one UID.

        On Gmail, returns (raw_bytes, X-GM-MSGID, X-GM-LABELS).
        On other providers, returns (raw_bytes, "", [flags]) with standard IMAP flags
        (e.g. \\Seen, \\Flagged) in place of Gmail labels.
        """
        assert self.conn is not None
        if self.is_gmail:
            parts = "(BODY.PEEK[] X-GM-MSGID X-GM-LABELS)"
        else:
            parts = "(BODY.PEEK[] FLAGS)"
        typ, data = self.conn.uid("FETCH", str(uid), parts)
        if typ != "OK":
            raise ImapError(f"UID FETCH {uid} failed: {data!r}")
        raw, gm_msgid, labels = _extract_rfc822_and_meta(data, gmail=self.is_gmail)
        return raw, gm_msgid, labels

    def move_to_trash(self, uid: int) -> None:
        """Move a message to Trash. Branches on Gmail vs standard IMAP."""
        assert self.conn is not None
        if self.is_gmail:
            label_str = r"\Trash"
            typ, data = self.conn.uid(
                "STORE", str(uid), "+X-GM-LABELS", f"({label_str})"
            )
            if typ != "OK":
                raise ImapError(f"UID STORE +X-GM-LABELS failed: {data!r}")
            return
        if not self.trash_folder:
            raise ImapError(
                "No Trash folder advertised by this IMAP server. "
                "Set the trash folder via LIST special-use flags or configure your provider."
            )
        typ, data = self.conn.uid("COPY", str(uid), _quote_mailbox(self.trash_folder))
        if typ != "OK":
            raise ImapError(f"UID COPY to {self.trash_folder} failed: {data!r}")
        typ, data = self.conn.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
        if typ != "OK":
            raise ImapError(f"UID STORE +FLAGS \\Deleted failed: {data!r}")
        self.conn.expunge()

    def append_to_sent(self, raw_message: bytes) -> Optional[str]:
        """Save a just-sent message to the Sent folder.

        No-op on Gmail (which auto-saves SMTP-sent messages to [Gmail]/Sent Mail).
        On other providers, APPENDs the raw RFC822 bytes with the \\Seen flag.
        Returns the folder name used, or None if skipped.
        """
        assert self.conn is not None
        if self.is_gmail:
            return None
        folder = self.sent_folder
        if not folder:
            return None
        flags = r"(\Seen)"
        date_time = imaplib.Time2Internaldate(time.time())
        typ, data = self.conn.append(
            _quote_mailbox(folder), flags, date_time, raw_message
        )
        if typ != "OK":
            raise ImapError(f"APPEND to {folder} failed: {data!r}")
        return folder

    def find_uid_by_message_id(
        self, message_id: str, folders: Optional[list[str]] = None
    ) -> tuple[int, str]:
        """Search folders in order for the message with this RFC822 Message-ID.
        Returns (uid, folder)."""
        assert self.conn is not None
        if not message_id:
            raise ImapError("message_id is required")
        if folders is None:
            folders = ["INBOX"]
            if self.all_mail_folder and self.all_mail_folder not in folders:
                folders.append(self.all_mail_folder)
        mid = message_id.strip()
        inner = mid[1:-1] if mid.startswith("<") and mid.endswith(">") else mid
        quoted = f'"{inner}"'
        last_err: Optional[str] = None
        for folder in folders:
            try:
                self.select(folder, readonly=False)
            except ImapError as e:
                last_err = str(e)
                continue
            uids = self.uid_search("HEADER", "Message-ID", quoted)
            if uids:
                return uids[-1], folder
        raise ImapError(
            f"Message-ID {message_id} not found in {folders}"
            + (f" (last error: {last_err})" if last_err else "")
        )

    def noop(self) -> None:
        assert self.conn is not None
        self.conn.noop()


def _extract_rfc822_and_meta(data, *, gmail: bool) -> tuple[bytes, str, list[str]]:
    """Pull the RFC822 literal body and provider metadata out of a FETCH response."""
    raw_body = b""
    metadata_text = ""
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            prefix, literal = item[0], item[1]
            if isinstance(prefix, (bytes, bytearray)):
                metadata_text += " " + prefix.decode("utf-8", errors="replace")
            if isinstance(literal, (bytes, bytearray)) and literal:
                if not raw_body:
                    raw_body = bytes(literal)
        elif isinstance(item, (bytes, bytearray)):
            metadata_text += " " + item.decode("utf-8", errors="replace")

    gm_msgid = ""
    labels: list[str] = []
    if gmail:
        m = re.search(r"X-GM-MSGID\s+(\d+)", metadata_text)
        if m:
            gm_msgid = m.group(1)
        lm = re.search(r"X-GM-LABELS\s+\(([^)]*)\)", metadata_text)
        if lm:
            labels = _parse_labels_blob(lm.group(1))
    else:
        fm = re.search(r"FLAGS\s+\(([^)]*)\)", metadata_text)
        if fm:
            labels = _parse_labels_blob(fm.group(1))
    return raw_body, gm_msgid, labels


def _parse_labels_blob(s: str) -> list[str]:
    labels: list[str] = []
    i = 0
    s = s.strip()
    while i < len(s):
        while i < len(s) and s[i].isspace():
            i += 1
        if i >= len(s):
            break
        if s[i] == '"':
            start = i + 1
            i += 1
            while i < len(s):
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == '"':
                    break
                i += 1
            labels.append(s[start:i])
            if i < len(s):
                i += 1
        else:
            start = i
            while i < len(s) and not s[i].isspace():
                i += 1
            labels.append(s[start:i])
    return labels
