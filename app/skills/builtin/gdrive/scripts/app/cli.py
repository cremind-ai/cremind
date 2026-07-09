"""argparse CLI for the gdrive skill: link + file search/download/upload/organize
verbs. The persistent listener (event_listener.py) establishes the changes.watch
channel automatically; there is no manual watch verb here."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from typing import Any

from . import config, drive_api, formatter
from .google import auth
from .google.discovery import Discovery, DiscoveryError

_FALLBACK_SCOPES = ["openid", "email", "https://www.googleapis.com/auth/drive"]

# Match a Drive file/folder id in common URL shapes, or accept a bare id.
_URL_PATTERNS = [
    re.compile(r"/file/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/folders/([a-zA-Z0-9-_]+)"),
    re.compile(r"/document/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/presentation/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9-_]+)"),
]

# Default export targets for Google-native types, with a file extension.
_EXPORT_DEFAULTS = {
    "application/vnd.google-apps.document": ("text/markdown", ".md"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}
_EXT_BY_MIME = {
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "image/png": ".png",
}


def _resolve_client() -> tuple[str, str, list[str]]:
    disc = Discovery(config.CREMIND_CONNECT_URL)
    try:
        creds = disc.credentials()
    except DiscoveryError as e:
        raise SystemExit(f"Could not reach cremind-connect at {config.CREMIND_CONNECT_URL}: {e}")
    try:
        scopes = disc.scopes("drive")
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
    return drive_api.build_service(creds)


def _extract_id(value: str) -> str:
    for pat in _URL_PATTERNS:
        m = pat.search(value or "")
        if m:
            return m.group(1)
    return (value or "").strip()


def _emit(result: Any, args) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False))


def _escape_q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


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


def _build_query(args) -> str | None:
    clauses: list[str] = []
    if getattr(args, "query", None):
        clauses.append(f"({args.query})")
    if getattr(args, "name", None):
        clauses.append(f"name contains '{_escape_q(args.name)}'")
    if getattr(args, "folder", None):
        clauses.append(f"'{_extract_id(args.folder)}' in parents")
    if getattr(args, "mime_type", None):
        clauses.append(f"mimeType = '{_escape_q(args.mime_type)}'")
    if not getattr(args, "trashed", False):
        clauses.append("trashed = false")
    return " and ".join(clauses) if clauses else None


def cmd_list(args) -> Any:
    svc = _svc()
    resp = drive_api.list_files(
        svc,
        query=_build_query(args),
        order_by=args.order_by,
        page_size=args.max_results,
        page_token=args.page_token,
    )
    files = [formatter.parse_file(f) for f in resp.get("files", []) or []]
    out: dict[str, Any] = {"files": files}
    if resp.get("nextPageToken"):
        out["next_page_token"] = resp["nextPageToken"]
    return out


def cmd_info(args) -> Any:
    svc = _svc()
    return formatter.parse_file(drive_api.get_file(svc, file_id=_extract_id(args.id)))


def _resolve_out_path(out: str, name: str, ext: str) -> str:
    if os.path.isdir(out):
        fname = name
        if ext and not fname.lower().endswith(ext.lower()):
            fname = f"{fname}{ext}"
        return os.path.join(out, fname)
    return out


def cmd_download(args) -> Any:
    from googleapiclient.errors import HttpError

    svc = _svc()
    file_id = _extract_id(args.id)
    meta = drive_api.get_file(svc, file_id=file_id)
    mime = meta.get("mimeType", "")
    name = meta.get("name", file_id)

    if mime.startswith("application/vnd.google-apps."):
        if args.mime:
            export_mime, ext = args.mime, _EXT_BY_MIME.get(args.mime, "")
        elif mime in _EXPORT_DEFAULTS:
            export_mime, ext = _EXPORT_DEFAULTS[mime]
        else:
            raise SystemExit(
                f"'{name}' is a Google-native file ({mime}) with no default export; "
                f"pass --mime <target-mime> (e.g. application/pdf)."
            )
        try:
            content = drive_api.export_file(svc, file_id=file_id, mime_type=export_mime)
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            # Docs markdown export is supported on Drive; fall back to plain text if
            # the target mime is rejected by this file type.
            if status == 400 and export_mime == "text/markdown":
                export_mime, ext = "text/plain", ".txt"
                content = drive_api.export_file(svc, file_id=file_id, mime_type=export_mime)
            else:
                raise
        out_path = _resolve_out_path(args.out, name, ext)
        exported = True
    else:
        content = drive_api.download_media(svc, file_id=file_id)
        ext = os.path.splitext(name)[1]
        out_path = _resolve_out_path(args.out, name, ext)
        export_mime = mime
        exported = False

    with open(out_path, "wb") as f:
        f.write(content)
    return {"downloaded": True, "id": file_id, "path": out_path, "bytes": len(content), "exported": exported, "mime_type": export_mime}


def cmd_upload(args) -> Any:
    svc = _svc()
    if not os.path.isfile(args.file):
        raise SystemExit(f"file not found: {args.file}")
    name = args.name or os.path.basename(args.file)
    mime = args.mime or mimetypes.guess_type(name)[0]
    f = drive_api.upload_file(
        svc,
        path=args.file,
        name=name,
        mime_type=mime,
        parent=_extract_id(args.parent) if args.parent else None,
    )
    return {"uploaded": True, **formatter.parse_file(f)}


def cmd_mkdir(args) -> Any:
    svc = _svc()
    f = drive_api.create_folder(svc, name=args.name, parent=_extract_id(args.parent) if args.parent else None)
    return {"created": True, "id": f.get("id"), "name": f.get("name"), "web_view_link": f.get("webViewLink")}


def cmd_move(args) -> Any:
    svc = _svc()
    f = drive_api.move_file(svc, file_id=_extract_id(args.id), add_parent=_extract_id(args.parent))
    return {"moved": True, **formatter.parse_file(f)}


def cmd_rename(args) -> Any:
    svc = _svc()
    f = drive_api.update_file(svc, file_id=_extract_id(args.id), body={"name": args.name})
    return {"renamed": True, **formatter.parse_file(f)}


def cmd_trash(args) -> Any:
    svc = _svc()
    f = drive_api.update_file(svc, file_id=_extract_id(args.id), body={"trashed": True})
    return {"trashed": True, "id": f.get("id"), "name": f.get("name")}


def cmd_restore(args) -> Any:
    svc = _svc()
    f = drive_api.update_file(svc, file_id=_extract_id(args.id), body={"trashed": False})
    return {"restored": True, "id": f.get("id"), "name": f.get("name")}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gdrive", description="Google Drive via OAuth (cremind-connect).")
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

    sp = sub.add_parser("list", help="list/search files")
    sp.add_argument("--query", help="raw Drive q= expression (combined with the other filters)")
    sp.add_argument("--name", help="name contains this substring")
    sp.add_argument("--folder", help="parent folder id or URL")
    sp.add_argument("--mime-type", dest="mime_type", help="exact mimeType filter")
    sp.add_argument("--trashed", action="store_true", help="include trashed files (default: exclude)")
    sp.add_argument("--max-results", type=int, default=50, dest="max_results")
    sp.add_argument("--page-token", dest="page_token")
    sp.add_argument("--order-by", dest="order_by", default="modifiedTime desc")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("info", help="file metadata")
    sp.add_argument("--id", required=True, help="file id or URL")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("download", help="download a file (Google-native types are exported)")
    sp.add_argument("--id", required=True, help="file id or URL")
    sp.add_argument("--out", required=True, help="output file path, or a directory")
    sp.add_argument("--mime", help="export MIME override for Google-native files")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("upload", help="upload a local file")
    sp.add_argument("--file", required=True, help="local file path")
    sp.add_argument("--name", help="name in Drive (default: basename)")
    sp.add_argument("--parent", help="destination folder id or URL")
    sp.add_argument("--mime", help="MIME type (default: guessed from name)")
    sp.set_defaults(func=cmd_upload)

    sp = sub.add_parser("mkdir", help="create a folder")
    sp.add_argument("--name", required=True)
    sp.add_argument("--parent", help="parent folder id or URL")
    sp.set_defaults(func=cmd_mkdir)

    sp = sub.add_parser("move", help="move a file into a folder")
    sp.add_argument("--id", required=True, help="file id or URL")
    sp.add_argument("--parent", required=True, help="destination folder id or URL")
    sp.set_defaults(func=cmd_move)

    sp = sub.add_parser("rename", help="rename a file")
    sp.add_argument("--id", required=True, help="file id or URL")
    sp.add_argument("--name", required=True)
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser("trash", help="move a file to trash (reversible)")
    sp.add_argument("--id", required=True, help="file id or URL")
    sp.set_defaults(func=cmd_trash)

    sp = sub.add_parser("restore", help="restore a file from trash")
    sp.add_argument("--id", required=True, help="file id or URL")
    sp.set_defaults(func=cmd_restore)

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
