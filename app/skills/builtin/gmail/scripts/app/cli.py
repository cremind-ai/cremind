"""argparse CLI for the gmail skill: link + message verbs + watch helpers."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config, formatter, gmail_api
from .google import auth
from .google.discovery import Discovery


def _resolve_client() -> tuple[str, str, list[str]]:
    disc = Discovery(config.CREMIND_CONNECT_URL)
    client_id = config.GOOGLE_CLIENT_ID or disc.client_id()
    client_secret = config.GOOGLE_CLIENT_SECRET
    scopes = disc.scopes()
    if not client_id:
        raise SystemExit("No GOOGLE_CLIENT_ID (set it in scripts/.env or ensure discovery is reachable).")
    if not scopes:
        scopes = [
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ]
    return client_id, client_secret, scopes


def _svc():
    creds, _ = auth.get_credentials(config.TOKEN_PATH)
    return gmail_api.build_service(creds)


def _emit(result: Any, args) -> None:
    as_json = getattr(args, "json", False) or not sys.stdout.isatty()
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif isinstance(result, list):
        print(formatter.format_list(result))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


# --- commands ---

def cmd_link(args) -> Any:
    client_id, client_secret, scopes = _resolve_client()
    if not client_secret:
        raise SystemExit(
            "GOOGLE_CLIENT_SECRET missing in scripts/.env. The org provides the "
            "(non-confidential) Desktop client secret used for the loopback PKCE flow."
        )
    data = auth.link(
        token_path=config.TOKEN_PATH,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        open_browser=not args.no_browser,
    )
    return {"linked": True, "email": data["email"], "account_key": data["account_key"]}


def cmd_status(_args) -> Any:
    try:
        data = auth.load_account(config.TOKEN_PATH)
    except auth.AuthError:
        return {"linked": False}
    return {"linked": True, "email": data.get("email"), "account_key": data.get("account_key"), "scopes": data.get("scopes")}


def _rows_for_ids(svc, ids: list[str], detail: str) -> list[dict[str, Any]]:
    rows = []
    fmt = "full" if detail == "full" else "metadata"
    for m in ids:
        msg = gmail_api.get_message(svc, m["id"], fmt=fmt)
        rows.append(formatter.parse_message(msg))
    return rows


def cmd_list(args) -> Any:
    svc = _svc()
    ids = gmail_api.list_messages(svc, query=args.query, max_results=args.max_results, label_ids=["INBOX"])
    return _rows_for_ids(svc, ids, args.detail)


def cmd_search(args) -> Any:
    svc = _svc()
    ids = gmail_api.list_messages(svc, query=args.query, max_results=args.max_results)
    return _rows_for_ids(svc, ids, args.detail)


def cmd_get(args) -> Any:
    svc = _svc()
    return formatter.parse_message(gmail_api.get_message(svc, args.id, fmt="full"))


def _read_body(args) -> str:
    if args.body is not None:
        return args.body
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def cmd_send(args) -> Any:
    svc = _svc()
    res = gmail_api.send_message(
        svc, to=args.to, subject=args.subject, body=_read_body(args), cc=args.cc, bcc=args.bcc
    )
    return {"sent": True, "id": res.get("id"), "thread_id": res.get("threadId")}


def cmd_reply(args) -> Any:
    svc = _svc()
    res = gmail_api.reply_message(svc, message_id=args.id, body=_read_body(args), cc=args.cc, bcc=args.bcc)
    return {"sent": True, "id": res.get("id"), "thread_id": res.get("threadId")}


def cmd_trash(args) -> Any:
    svc = _svc()
    gmail_api.trash_message(svc, args.id)
    return {"trashed": True, "id": args.id}


def cmd_watch(_args) -> Any:
    disc = Discovery(config.CREMIND_CONNECT_URL)
    creds, _ = auth.get_credentials(config.TOKEN_PATH)
    svc = gmail_api.build_service(creds)
    res = gmail_api.watch(svc, disc.gmail_topic())
    return {"watching": True, "history_id": res.get("historyId"), "expiration": res.get("expiration")}


def cmd_unwatch(_args) -> Any:
    svc = _svc()
    gmail_api.stop_watch(svc)
    return {"watching": False}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gmail", description="Gmail via OAuth (cremind-connect).")
    p.add_argument("--json", action="store_true", help="force JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("link", help="link a Google account (loopback PKCE)")
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_link)

    sub.add_parser("status", help="show link status").set_defaults(func=cmd_status)

    sp = sub.add_parser("list", help="list INBOX messages")
    sp.add_argument("--query")
    sp.add_argument("--max-results", type=int, default=10, dest="max_results")
    sp.add_argument("--detail", choices=["summary", "full"], default="summary")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("search", help="search all mail")
    sp.add_argument("--query", required=True)
    sp.add_argument("--max-results", type=int, default=10, dest="max_results")
    sp.add_argument("--detail", choices=["summary", "full"], default="summary")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("get", help="get a message by id")
    sp.add_argument("--id", required=True)
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("send", help="send an email")
    sp.add_argument("--to", action="append", required=True)
    sp.add_argument("--subject", required=True)
    sp.add_argument("--cc", action="append")
    sp.add_argument("--bcc", action="append")
    sp.add_argument("--body")
    sp.add_argument("--body-file", dest="body_file")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("reply", help="reply in-thread to a message id")
    sp.add_argument("--id", required=True)
    sp.add_argument("--cc", action="append")
    sp.add_argument("--bcc", action="append")
    sp.add_argument("--body")
    sp.add_argument("--body-file", dest="body_file")
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("trash", help="move a message to trash")
    sp.add_argument("--id", required=True)
    sp.set_defaults(func=cmd_trash)

    sub.add_parser("watch", help="establish the Gmail watch once").set_defaults(func=cmd_watch)
    sub.add_parser("unwatch", help="stop the Gmail watch").set_defaults(func=cmd_unwatch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except auth.AuthError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    _emit(result, args)
    return 0
