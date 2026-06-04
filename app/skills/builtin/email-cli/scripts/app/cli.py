import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from . import config, formatter, operations


def _warn_if_listener_not_running() -> None:
    stale_threshold = config.POLL_INTERVAL * 4

    if not config.HEARTBEAT_FILE.exists():
        print(
            "warning: event listener is not running. "
            "Run `uv run scripts/event_listener.py` to begin capturing incoming emails to events/.",
            file=sys.stderr,
        )
        return

    try:
        age = time.time() - config.HEARTBEAT_FILE.stat().st_mtime
    except OSError:
        return

    if age > stale_threshold:
        print(
            f"warning: event listener heartbeat is stale ({int(age)}s old). "
            "The listener may have stopped. Run `uv run scripts/event_listener.py` to restart it.",
            file=sys.stderr,
        )


def _read_body(args) -> str:
    if getattr(args, "body", None):
        return args.body
    body_file = getattr(args, "body_file", None)
    if body_file:
        return Path(body_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("error: provide --body STRING, --body-file PATH, or pipe body on stdin")


def _emit(data, as_json: bool, table_mode: bool = False) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if table_mode and isinstance(data, list):
        print(formatter.format_table(data))
        return
    if isinstance(data, dict) and "body" in data:
        print(f"Subject: {data.get('subject','')}")
        print(f"From: {data.get('from','')}")
        print(f"To: {data.get('to','')}")
        if data.get("cc"):
            print(f"Cc: {data['cc']}")
        print(f"Date: {data.get('date','')}")
        print(f"Message-ID: {data.get('message_id','')}")
        if data.get("labels"):
            print(f"Labels: {', '.join(data['labels'])}")
        print("---")
        print(data.get("body", ""))
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="email-cli",
        description="Email CLI for any IMAP/SMTP provider (stdlib only). "
        "Works with Gmail, Outlook, Yahoo, iCloud, Fastmail, or any custom IMAP server.",
    )
    p.add_argument("--json", action="store_true", help="force JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    # list
    sp = sub.add_parser("list", help="list emails from inbox")
    sp.add_argument("--max-results", type=int, default=10)
    sp.add_argument(
        "--query",
        default=None,
        help="Free-text search. On Gmail: full Gmail syntax (e.g. 'from:alice has:attachment'). "
        "On standard IMAP: each whitespace-separated term is matched against headers + body.",
    )
    sp.add_argument("--detail", choices=["title_only", "summary", "full"], default="summary")
    sp.add_argument(
        "--category",
        choices=["primary", "promotions", "social", "updates", "forums", "spam", "all"],
        default="primary",
        help="Folder/category selector. Gmail uses this to filter by category tab. "
        "Other providers: 'spam' selects the Junk folder, 'all' selects the All Mail "
        "folder if available, all other values select INBOX.",
    )
    sp.add_argument("--since", default=None, help="YYYY-MM-DD")
    sp.add_argument("--before", default=None, help="YYYY-MM-DD")

    # list-sent
    sp = sub.add_parser("list-sent", help="list emails from sent folder")
    sp.add_argument("--max-results", type=int, default=10)
    sp.add_argument("--since", default=None, help="YYYY-MM-DD")
    sp.add_argument("--before", default=None, help="YYYY-MM-DD")

    # send
    sp = sub.add_parser("send", help="send a new email")
    sp.add_argument("--to", action="append", required=True, default=[])
    sp.add_argument("--cc", action="append", default=[])
    sp.add_argument("--bcc", action="append", default=[])
    sp.add_argument("--subject", required=True)
    sp.add_argument("--body", default=None)
    sp.add_argument("--body-file", default=None)

    # reply
    sp = sub.add_parser("reply", help="reply to an existing email")
    sp.add_argument("--message-id", required=True, help="RFC822 Message-ID, e.g. <abc@mail.gmail.com>")
    sp.add_argument("--cc", action="append", default=[])
    sp.add_argument("--bcc", action="append", default=[])
    sp.add_argument("--body", default=None)
    sp.add_argument("--body-file", default=None)

    # trash
    sp = sub.add_parser("trash", help="move an email to Trash")
    sp.add_argument("--message-id", required=True)

    # get
    sp = sub.add_parser("get", help="fetch full content of an email")
    sp.add_argument("--message-id", required=True)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    as_json = args.json or (not sys.stdout.isatty())

    _warn_if_listener_not_running()

    try:
        if args.command == "list":
            rows = operations.list_emails(
                max_results=args.max_results,
                query=args.query,
                detail=args.detail,
                category=args.category,
                since=args.since,
                before=args.before,
            )
            _emit(rows, as_json=as_json, table_mode=True)

        elif args.command == "list-sent":
            rows = operations.list_sent_emails(
                max_results=args.max_results,
                since=args.since,
                before=args.before,
            )
            _emit(rows, as_json=as_json, table_mode=True)

        elif args.command == "send":
            body = _read_body(args)
            result = operations.send_email(
                to=args.to,
                subject=args.subject,
                body=body,
                cc=args.cc or None,
                bcc=args.bcc or None,
            )
            _emit(result, as_json=True)

        elif args.command == "reply":
            body = _read_body(args)
            result = operations.reply_to_email(
                message_id=args.message_id,
                body=body,
                cc=args.cc or None,
                bcc=args.bcc or None,
            )
            _emit(result, as_json=True)

        elif args.command == "trash":
            result = operations.trash_email(message_id=args.message_id)
            _emit(result, as_json=True)

        elif args.command == "get":
            result = operations.get_email_details(message_id=args.message_id)
            _emit(result, as_json=as_json)

        else:
            parser.error(f"unknown command: {args.command}")
    except Exception as e:
        err = {"ok": False, "error": str(e), "error_type": type(e).__name__}
        print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0
