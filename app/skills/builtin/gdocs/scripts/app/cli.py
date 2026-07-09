"""argparse CLI for the gdocs skill: link + document read/create/edit verbs.

Execution-only (no events): Google has no push API for document content, so to
list/search documents or watch for changes, use the gdrive skill.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from . import config, docs_api, extract
from .google import auth
from .google.discovery import Discovery, DiscoveryError

_FALLBACK_SCOPES = ["openid", "email", "https://www.googleapis.com/auth/documents"]

# Match a document id in a full URL (…/document/d/<id>/…) or accept a bare id.
_URL_ID = re.compile(r"/document/d/([a-zA-Z0-9-_]+)")


def _resolve_client() -> tuple[str, str, list[str]]:
    disc = Discovery(config.CREMIND_CONNECT_URL)
    try:
        creds = disc.credentials()
    except DiscoveryError as e:
        raise SystemExit(f"Could not reach cremind-connect at {config.CREMIND_CONNECT_URL}: {e}")
    try:
        scopes = disc.scopes("docs")
    except DiscoveryError:
        scopes = []
    client_id = config.GOOGLE_CLIENT_ID or creds.get("clientId", "")
    client_secret = config.GOOGLE_CLIENT_SECRET or creds.get("clientSecret", "")
    if not client_id:
        raise SystemExit("No GOOGLE_CLIENT_ID (set it in scripts/.env or ensure cremind-connect is reachable).")
    if not scopes:
        scopes = list(_FALLBACK_SCOPES)
    return client_id, client_secret, scopes


def _svc():
    creds, _ = auth.get_credentials(config.TOKEN_PATH)
    return docs_api.build_service(creds)


def _extract_id(value: str) -> str:
    m = _URL_ID.search(value or "")
    return m.group(1) if m else (value or "").strip()


def _read_text(args) -> str:
    if getattr(args, "text", None) is not None:
        return args.text
    if getattr(args, "file", None):
        with open(args.file, encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _doc_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _emit(result: Any, args) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False))


# --- commands ---

def cmd_link(args) -> Any:
    client_id, client_secret, scopes = _resolve_client()
    if not client_secret:
        raise SystemExit(
            "No GOOGLE_CLIENT_SECRET available. It is normally provided by "
            "cremind-connect; set it in scripts/.env to override, or ensure "
            "cremind-connect is reachable at CREMIND_CONNECT_URL."
        )
    data = auth.link(
        token_path=config.TOKEN_PATH,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        open_browser=not args.no_browser,
        redirect_uri=config.OAUTH_REDIRECT_URI,
    )
    return {"linked": True, "email": data["email"], "account_key": data["account_key"]}


def cmd_complete_link(args) -> Any:
    auth.submit_callback(args.response)
    return {
        "submitted": True,
        "note": "Linking will complete in the running 'link' command; run 'status' to confirm.",
    }


def cmd_status(_args) -> Any:
    try:
        data = auth.load_account(config.TOKEN_PATH)
    except auth.AuthError:
        return {"linked": False}
    return {"linked": True, "email": data.get("email"), "account_key": data.get("account_key"), "scopes": data.get("scopes")}


def cmd_create(args) -> Any:
    svc = _svc()
    doc = docs_api.create_document(svc, title=args.title)
    doc_id = doc.get("documentId")
    text = _read_text(args)
    if text:
        docs_api.append_text(svc, document_id=doc_id, text=text)
    return {"created": True, "id": doc_id, "title": doc.get("title"), "url": _doc_url(doc_id)}


def cmd_read(args) -> Any:
    svc = _svc()
    doc = docs_api.get_document(svc, document_id=_extract_id(args.id))
    if args.format == "json":
        return doc
    if args.format == "text":
        content = extract.to_text(doc)
    else:
        content = extract.to_markdown(doc)
    return {
        "id": doc.get("documentId"),
        "title": doc.get("title"),
        "format": args.format,
        "content": content,
    }


def cmd_info(args) -> Any:
    svc = _svc()
    doc = docs_api.get_document(svc, document_id=_extract_id(args.id))
    return {
        "id": doc.get("documentId"),
        "title": doc.get("title"),
        "revision_id": doc.get("revisionId"),
        "url": _doc_url(doc.get("documentId")),
    }


def cmd_append(args) -> Any:
    svc = _svc()
    doc_id = _extract_id(args.id)
    text = _read_text(args)
    if not text:
        raise SystemExit("No text to append (pass --text, --file PATH, or pipe on stdin).")
    # Prefix a newline when the document already has body content so the appended
    # text starts on its own line rather than joining the last paragraph.
    doc = docs_api.get_document(svc, document_id=doc_id)
    body = (doc.get("body", {}) or {}).get("content", []) or []
    has_text = any((el.get("paragraph", {}) or {}).get("elements") for el in body)
    if has_text and not text.startswith("\n"):
        text = "\n" + text
    docs_api.append_text(svc, document_id=doc_id, text=text)
    return {"appended": True, "id": doc_id, "url": _doc_url(doc_id)}


def cmd_replace(args) -> Any:
    svc = _svc()
    doc_id = _extract_id(args.id)
    result = docs_api.replace_all(
        svc,
        document_id=doc_id,
        find=args.find,
        replace_with=args.replace_with,
        match_case=args.match_case,
    )
    return {"replaced": True, "id": doc_id, "occurrences_changed": result["occurrences_changed"]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gdocs", description="Google Docs via OAuth (cremind-connect).")
    p.add_argument("--json", action="store_true", help="force JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("link", help="link a Google account (loopback PKCE)")
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_link)

    sp = sub.add_parser(
        "complete-link",
        help="finish linking by pasting the URL Google redirected you to (remote/Ingress)",
    )
    sp.add_argument("--response", required=True, help="the full redirect URL (or its code=...&state=... query)")
    sp.set_defaults(func=cmd_complete_link)

    sub.add_parser("status", help="show link status").set_defaults(func=cmd_status)

    sp = sub.add_parser("create", help="create a new document (optional initial text)")
    sp.add_argument("--title", required=True)
    sp.add_argument("--text", help="initial body text")
    sp.add_argument("--file", help="read initial body text from a file")
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("read", help="read a document as markdown/text/json")
    sp.add_argument("--id", required=True, help="document id or URL")
    sp.add_argument("--format", choices=["markdown", "text", "json"], default="markdown")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("info", help="document metadata (title, revision, url)")
    sp.add_argument("--id", required=True, help="document id or URL")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("append", help="append text to the end of a document")
    sp.add_argument("--id", required=True, help="document id or URL")
    sp.add_argument("--text", help="text to append")
    sp.add_argument("--file", help="read text to append from a file")
    sp.set_defaults(func=cmd_append)

    sp = sub.add_parser("replace", help="find-and-replace all occurrences of a string")
    sp.add_argument("--id", required=True, help="document id or URL")
    sp.add_argument("--find", required=True)
    sp.add_argument("--replace-with", dest="replace_with", required=True)
    sp.add_argument("--match-case", action="store_true", dest="match_case")
    sp.set_defaults(func=cmd_replace)

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
