"""argparse CLI for the gdrive skill: link + grant + file listing/download/upload/
organize verbs. The persistent listener (event_listener.py) establishes the
changes.watch channel automatically; there is no manual watch verb here.

Access is per-file by default: Cremind reaches only files it created and files
the user picked via ``grant`` (see grant.py), and there is no whole-Drive search.
An account linked with bring-your-own credentials at the wider
``.../auth/drive`` scope reaches the whole Drive instead — ``status`` reports
which of the two applies (``access_model``)."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from typing import Any

from . import config, drive_api, errors, formatter, grant
from .google import auth
from .google.discovery import Discovery, DiscoveryError

_FALLBACK_SCOPES = ["openid", "email", "https://www.googleapis.com/auth/drive.file"]

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
    creds: dict[str, Any] = {}
    disc_error: DiscoveryError | None = None
    try:
        creds = disc.credentials()
    except DiscoveryError as e:
        # Not fatal on its own: a bring-your-own-credentials user supplies the
        # client themselves and never needs cremind-connect for this. Only report
        # it if we actually end up without a client id.
        disc_error = e
    try:
        scopes = disc.scopes("drive")
    except DiscoveryError:
        scopes = []
    # GOOGLE_SCOPES lets a bring-your-own-credentials user request scopes their own
    # OAuth client is allowed to ask for — notably whole-Drive, which the shared
    # client cannot request (Google classes it restricted). It wins over discovery,
    # which only ever advertises the shared client's per-file set.
    if config.GOOGLE_SCOPES:
        scopes = config.GOOGLE_SCOPES.split()
    client_id = config.GOOGLE_CLIENT_ID or creds.get("clientId", "")
    client_secret = config.GOOGLE_CLIENT_SECRET or creds.get("clientSecret", "")
    if not client_id:
        if disc_error is not None:
            raise SystemExit(
                f"Could not reach cremind-connect at {config.CREMIND_CONNECT_URL}: {disc_error}"
            )
        raise SystemExit("No GOOGLE_CLIENT_ID (set it in scripts/.env or ensure cremind-connect is reachable).")
    if not scopes:
        scopes = list(_FALLBACK_SCOPES)
    return client_id, client_secret, scopes


def _expected_scopes() -> tuple[list[str], bool]:
    """``(scopes, resolved)`` — what the next ``link`` would request.

    ``resolved`` is False when cremind-connect could not be asked and the fallback
    is a guess. A guess must never drive the staleness warning: its remedy is
    re-linking, which permanently narrows a whole-Drive account, so a broker
    outage would otherwise talk users into an irreversible downgrade.
    """
    if config.GOOGLE_SCOPES:
        return config.GOOGLE_SCOPES.split(), True
    try:
        scopes = Discovery(config.CREMIND_CONNECT_URL).scopes("drive")
    except DiscoveryError:
        return list(_FALLBACK_SCOPES), False
    if not scopes:
        return list(_FALLBACK_SCOPES), False
    return list(scopes), True


def _uses_own_client(token_client_id: str) -> bool:
    """Whether this account was linked with the user's own OAuth client.

    Mirrors the backend's ``skill_token.uses_own_client``. An env-supplied client
    id proves bring-your-own outright; otherwise the token's client is compared
    against the one cremind-connect advertises. An unreachable broker proves
    nothing, so it never claims bring-your-own it cannot demonstrate — the wrong
    attribution would tell a user they configured something they did not.
    """
    if config.GOOGLE_CLIENT_ID:
        return True
    if not token_client_id:
        return False
    try:
        disc = Discovery(config.CREMIND_CONNECT_URL)
        shared = str(disc.credentials().get("clientId") or "") or disc.client_id()
    except DiscoveryError:
        return False
    return bool(shared) and token_client_id != shared


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
    granted = data.get("scopes") or []
    # What this account can reach is decided by the scopes it was GRANTED, never
    # by the scopes a future `link` would request: between setting GOOGLE_SCOPES
    # and re-linking, the two disagree, and trusting the latter would tell the
    # agent it has whole-Drive while every call still 404s.
    whole_drive = errors.LEGACY_DRIVE_SCOPE in set(granted)
    if whole_drive:
        why = (
            "bring-your-own credentials"
            if _uses_own_client(str(data.get("client_id") or ""))
            else "the shared Cremind client still requests it"
        )
        access_model = f"whole-Drive ({why})"
    else:
        access_model = "per-file (drive.file): granted files + files Cremind created"
    out: dict[str, Any] = {
        "linked": True,
        "email": data.get("email"),
        "account_key": data.get("account_key"),
        "scopes": granted,
        "access_model": access_model,
    }
    expected, resolved = _expected_scopes()
    out["expected_scopes"] = expected
    if not resolved:
        # Say so rather than reasoning from a guess — see _expected_scopes.
        out["expected_unresolved"] = True
        return out
    if errors.scopes_are_stale(granted, expected):
        out["scopes_stale"] = True
        out["hint"] = (
            "This account is linked with the old whole-Drive scope, which is no longer "
            "issued. Re-running `link` moves it to per-file access — one-way on the "
            "shared Cremind client, which can never be granted whole-Drive again. To "
            "keep whole-Drive, set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / "
            "GOOGLE_SCOPES in scripts/.env (bring your own client) before re-linking. "
            "After re-linking, use `grant` to pick the files Cremind should reach; the "
            "old broad access can be revoked at https://myaccount.google.com/connections"
        )
    elif errors.LEGACY_DRIVE_SCOPE in expected and not whole_drive:
        # GOOGLE_SCOPES asks for whole-Drive but this token predates that: the
        # widening only takes effect at the next consent.
        out["hint"] = (
            "This install requests whole-Drive, but the linked token is still per-file. "
            "Re-run `link` to re-consent at the wider scope; until then use `grant` for "
            "any file Cremind cannot reach."
        )
    return out


def cmd_grant(args) -> Any:
    client_id, client_secret, _scopes = _resolve_client()
    file_ids = [_extract_id(v) for v in (args.file or [])]
    return grant.run_grant(
        token_path=config.TOKEN_PATH,
        grants_path=config.GRANTS_PATH,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=config.OAUTH_REDIRECT_URI,
        build_service=drive_api.build_service,
        get_file=drive_api.get_file,
        list_files=drive_api.list_files,
        file_ids=file_ids or None,
        allow_multiple=not args.single,
        allow_folders=not args.no_folders,
        mime_types=[m.strip() for m in (args.mime_types or "").split(",") if m.strip()] or None,
        wait=not args.no_wait,
        timeout=args.timeout,
    )


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


def _compact_file(f: dict[str, Any]) -> dict[str, Any]:
    """One listed file as 4 fields instead of 11.

    A full page of ``parse_file`` output is large enough that the agent runtime
    clamps it mid-payload, so an overview listing ("what can I reach?") is best
    served by ids, names and types only. ``info --id`` gets the rest.
    """
    return {
        "id": f.get("id", ""),
        "name": f.get("name", ""),
        "type": formatter.mime_label(f.get("mime_type", "")),
        "modified_time": f.get("modified_time", ""),
    }


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
    if getattr(args, "compact", False):
        files = [_compact_file(f) for f in files]
    out: dict[str, Any] = {"count": len(files), "files": files}
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


def _listener_state() -> dict[str, Any]:
    """The listener's saved cursor + push-channel ids, or {} when absent.

    ``TokenStore.load`` is just a tolerant "read this JSON or give me nothing",
    which is exactly what is wanted here — reusing it avoids a third copy of
    ``listener._load_state`` and, more importantly, avoids importing
    ``app.listener`` (which would drag the websocket client into every CLI run).
    """
    return auth.TokenStore(config.STATE_FILE).load() or {}


def _stop_watch(_data: dict[str, Any]) -> dict[str, Any] | None:
    """Close the Google push channel while the credential still works.

    Passed to ``auth.unlink`` as ``before_revoke``: ``channels.stop`` needs a live
    token, so it has to happen before the revoke. If it raises, ``unlink`` records
    the failure and wipes anyway — a channel we could not close expires on its own
    within about a week, whereas leaving credentials on disk does not.
    """
    state = _listener_state()
    channel_id = str(state.get("channel_id") or "")
    resource_id = str(state.get("resource_id") or "")
    if not (channel_id and resource_id):
        return {"watch_stopped": False, "reason": "no_channel"}
    creds, _ = auth.get_credentials(config.TOKEN_PATH)
    drive_api.stop_channel(
        drive_api.build_service(creds), channel_id=channel_id, resource_id=resource_id
    )
    return {"watch_stopped": True, "channel_id": channel_id}


def _confirm_unlink(preview: dict[str, Any], *, revoke: bool) -> bool:
    """Ask before destroying a link — but only when a human can answer.

    Requires **both** streams to be terminals: a tty stdout with piped stdin makes
    ``input()`` raise ``EOFError``. Under the agent both are pipes, so this returns
    True and the caller proceeds — ``--yes`` is implied for non-interactive runs.
    The prompt goes to stderr so stdout stays pure JSON.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    lines = [f"Unlink {preview.get('email') or 'the linked account'} from this skill?"]
    lines.append(
        "  - revokes Cremind's access at Google"
        if revoke
        else "  - leaves the grant live at Google (--no-revoke)"
    )
    lines.append("  - deletes the local credentials; re-linking needs a fresh Google consent")
    siblings = [str(s.get("skill")) for s in preview.get("siblings") or []]
    if siblings and revoke:
        lines.append(
            "  - Google lists Cremind as ONE app, so this also ends the grant for: "
            + ", ".join(siblings)
        )
    if preview.get("listener_running"):
        lines.append(
            "  - a listener is running for this skill; stop it first, or it may rewrite "
            "the token file after this deletes it"
        )
    if preview.get("will_remove"):
        lines.append("  - removes: " + ", ".join(preview["will_remove"]))
    print("\n".join(lines), file=sys.stderr, flush=True)
    print("Type 'y' to continue: ", end="", file=sys.stderr, flush=True)
    try:
        return input().strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_unlink(args) -> Any:
    """Revoke Cremind's Google access and delete the stored credentials.

    Succeeds when nothing is linked, so it is safe to repeat.
    """
    preview = auth.unlink_preview(config.TOKEN_PATH, lock_path=config.LOCK_FILE)
    if args.dry_run:
        return preview
    if not preview["linked"]:
        return {"ok": True, "unlinked": False, "reason": "not_linked"}
    if not args.yes and not _confirm_unlink(preview, revoke=not args.no_revoke):
        return {"ok": True, "unlinked": False, "reason": "cancelled"}
    return auth.unlink(
        token_path=config.TOKEN_PATH,
        revoke_at_google=not args.no_revoke,
        clean_siblings=not args.keep_siblings,
        before_revoke=_stop_watch,
        lock_path=config.LOCK_FILE,
    )


def _add_unlink_parser(sub) -> None:
    sp = sub.add_parser(
        "unlink", help="revoke Cremind's Google access and delete the local token"
    )
    sp.add_argument(
        "--no-revoke",
        action="store_true",
        dest="no_revoke",
        help="wipe local credentials only; leave the grant live at Google",
    )
    sp.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt (implied when not a TTY)"
    )
    sp.add_argument(
        "--keep-siblings",
        action="store_true",
        dest="keep_siblings",
        help="do not clean up other skills' tokens for the same Google account",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="report what unlink would do, changing nothing",
    )
    sp.set_defaults(func=cmd_unlink)


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

    _add_unlink_parser(sub)

    sp = sub.add_parser(
        "grant",
        help="let the user pick Drive files to share with Cremind (Google file picker)",
    )
    sp.add_argument(
        "--file",
        action="append",
        help="pre-select a specific file id or URL (repeatable); use when the user "
             "already named a file Cremind cannot reach yet",
    )
    sp.add_argument("--single", action="store_true", help="allow only one file to be picked")
    sp.add_argument(
        "--no-folders", action="store_true", help="hide folders from the picker"
    )
    sp.add_argument("--mime-types", dest="mime_types", help="comma-separated mimeType filter")
    sp.add_argument(
        "--no-wait",
        action="store_true",
        help="print the picker URL and exit instead of waiting for the user to finish",
    )
    sp.add_argument("--timeout", type=float, default=600.0, help="seconds to wait (default 600)")
    sp.set_defaults(func=cmd_grant)

    sp = sub.add_parser(
        "list",
        help="list the files Cremind can reach (granted via `grant` + files it created)",
    )
    sp.add_argument("--query", help="raw Drive q= expression (combined with the other filters)")
    sp.add_argument("--name", help="name contains this substring")
    sp.add_argument("--folder", help="parent folder id or URL")
    sp.add_argument("--mime-type", dest="mime_type", help="exact mimeType filter")
    sp.add_argument("--trashed", action="store_true", help="include trashed files (default: exclude)")
    sp.add_argument("--max-results", type=int, default=50, dest="max_results")
    sp.add_argument("--page-token", dest="page_token")
    sp.add_argument("--order-by", dest="order_by", default="modifiedTime desc")
    sp.add_argument(
        "--compact",
        action="store_true",
        help="emit only id/name/type/modified_time per file (use for overviews)",
    )
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


def _target_file_id(args) -> str:
    """The file a failing command was aimed at, for the not-granted message."""
    for attr in ("id", "parent", "folder"):
        value = getattr(args, attr, None)
        if value:
            return _extract_id(value)
    return ""


def main(argv: list[str] | None = None) -> int:
    from googleapiclient.errors import HttpError

    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except auth.AuthError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    except HttpError as e:
        status = errors.http_status(e)
        if status not in (403, 404):
            raise
        stale = False
        expected, resolved = _expected_scopes()
        try:
            # An unresolved advertisement cannot prove staleness, and this message
            # goes straight to the agent — a wrong "re-link first" here sends it
            # down an irreversible path instead of the grant it actually needs.
            stale = resolved and errors.scopes_are_stale(
                auth.load_account(config.TOKEN_PATH).get("scopes"), expected
            )
        except auth.AuthError:
            pass
        return errors.emit(
            errors.not_granted_payload(
                file_id=_target_file_id(args), status=status, stale_scopes=stale
            )
        )
    _emit(result, args)
    return 0
