"""argparse CLI for the gmail skill: link + send/reply.

Send-only by default: the shared Cremind OAuth client requests
``gmail.send`` and nothing that can read a mailbox. Reading, searching, and
new-mail events come from the **imap-email** skill instead.

``list``/``search``/``get`` remain implemented but are gated on the granted
scopes, so they light up for a user who brings their own Google OAuth client and
asks for read scopes via ``GOOGLE_SCOPES``.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config, formatter, gmail_api
from .google import auth
from .google.discovery import Discovery, DiscoveryError

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

_FALLBACK_SCOPES = ["openid", "email", SEND_SCOPE]


def _resolve_client() -> tuple[str, str, list[str]]:
    disc = Discovery(config.CREMIND_CONNECT_URL)
    creds: dict[str, Any] = {}
    scopes: list[str] = []
    disc_error: DiscoveryError | None = None
    try:
        creds = disc.credentials()
        scopes = disc.scopes("gmail")
    except DiscoveryError as e:
        # Not fatal on its own: a bring-your-own-credentials user supplies the
        # client themselves and never needs cremind-connect for this. Only report
        # it if we actually end up without a client id.
        disc_error = e
    # Env (scripts/.env) overrides win; otherwise use the values cremind-connect
    # serves, so the org can rotate the client id/secret without a client update.
    client_id = config.GOOGLE_CLIENT_ID or creds.get("clientId", "")
    client_secret = config.GOOGLE_CLIENT_SECRET or creds.get("clientSecret", "")
    if not client_id:
        if disc_error is not None:
            raise SystemExit(
                f"Could not reach cremind-connect at {config.CREMIND_CONNECT_URL}: {disc_error}"
            )
        raise SystemExit("No GOOGLE_CLIENT_ID (set it in scripts/.env or ensure cremind-connect is reachable).")
    # GOOGLE_SCOPES lets a bring-your-own-credentials user request read scopes
    # their own OAuth client is allowed to ask for. It wins over discovery, which
    # only ever advertises the shared client's send-only set.
    if config.GOOGLE_SCOPES:
        scopes = config.GOOGLE_SCOPES.split()
    if not scopes:
        scopes = list(_FALLBACK_SCOPES)
    return client_id, client_secret, scopes


def _expected_scopes() -> tuple[list[str], bool]:
    """``(scopes, resolved)`` — what the next ``link`` would request.

    ``resolved`` is False when cremind-connect could not be asked and the fallback
    is a guess. A guess must never drive the stale-scope warning: acting on it
    means re-linking, which drops the read scope for good on the shared client.
    """
    if config.GOOGLE_SCOPES:
        return config.GOOGLE_SCOPES.split(), True
    try:
        scopes = Discovery(config.CREMIND_CONNECT_URL).scopes("gmail")
    except DiscoveryError:
        return list(_FALLBACK_SCOPES), False
    if not scopes:
        return list(_FALLBACK_SCOPES), False
    return list(scopes), True


def _granted_scopes() -> list[str]:
    try:
        return list(auth.load_account(config.TOKEN_PATH).get("scopes") or [])
    except auth.AuthError:
        return []


def _require_scope(scope: str, verb: str) -> None:
    """Fail with a route to the alternative instead of a Google 403."""
    if scope in _granted_scopes():
        return
    print(
        json.dumps(
            {
                "error": "scope_not_granted",
                "required_scope": scope,
                "verb": verb,
                "message": (
                    f"'{verb}' needs the {scope} scope, which Cremind's shared Google "
                    "client does not request: every Gmail scope that can read a mailbox "
                    "is classed 'restricted' by Google and requires a paid annual "
                    "security assessment."
                ),
                "use_instead": (
                    "Read, search, and receive email with the imap-email skill "
                    "(IMAP/SMTP with an app password). On Gmail accounts it accepts the "
                    "same search grammar, and its message_id values feed "
                    "'gmail reply --in-reply-to'."
                ),
                "or_bring_your_own": (
                    "To use this verb, supply your own Google OAuth client: set "
                    "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_SCOPES (including "
                    f"{scope}) in scripts/.env, then re-run link. See the Cremind docs: "
                    "Setup -> Bring your own Google credentials."
                ),
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    raise SystemExit(2)


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
    """Finish a link started in another (still-running) `link` by handing it the
    redirect URL the browser was sent to. For remote/Ingress deployments where
    the loopback redirect can't reach the backend; run `status` after to confirm.
    """
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
    granted = list(data.get("scopes") or [])
    out: dict[str, Any] = {
        "linked": True,
        "email": data.get("email"),
        "account_key": data.get("account_key"),
        "scopes": granted,
        "can_send": SEND_SCOPE in granted,
        "can_read": READ_SCOPE in granted,
    }
    expected, resolved = _expected_scopes()
    out["expected_scopes"] = expected
    if not resolved:
        # Say so rather than reasoning from a guess — see _expected_scopes.
        out["expected_unresolved"] = True
        return out
    if set(granted) - set(expected):
        out["stale_scopes"] = True
        out["hint"] = (
            "This account was linked with scopes the shared Cremind Google client no "
            "longer requests. It keeps working until Google retires the grant. "
            "Re-running `link` moves it to the current send-only set — one-way on the "
            "shared client — after which reading email needs the imap-email skill. To "
            "keep read scopes, set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / "
            "GOOGLE_SCOPES in scripts/.env (bring your own client) before re-linking."
        )
    return out


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
    preview = auth.unlink_preview(config.TOKEN_PATH)
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


def _rows_for_ids(svc, ids: list[str], detail: str) -> list[dict[str, Any]]:
    rows = []
    fmt = "full" if detail == "full" else "metadata"
    for m in ids:
        msg = gmail_api.get_message(svc, m["id"], fmt=fmt)
        rows.append(formatter.parse_message(msg))
    return rows


def cmd_list(args) -> Any:
    _require_scope(READ_SCOPE, "list")
    svc = _svc()
    ids = gmail_api.list_messages(svc, query=args.query, max_results=args.max_results, label_ids=["INBOX"])
    return _rows_for_ids(svc, ids, args.detail)


def cmd_search(args) -> Any:
    _require_scope(READ_SCOPE, "search")
    svc = _svc()
    ids = gmail_api.list_messages(svc, query=args.query, max_results=args.max_results)
    return _rows_for_ids(svc, ids, args.detail)


def cmd_get(args) -> Any:
    _require_scope(READ_SCOPE, "get")
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
    """Reply in-thread.

    Two modes. The default takes the threading headers from the caller, because
    ``gmail.send`` cannot look the original message up — every Gmail read scope is
    restricted. Mail clients thread on ``In-Reply-To``/``References`` plus a
    matching subject, which is exactly what the imap-email skill's ``message_id``
    supplies, so no Gmail thread id is needed.

    ``--id`` keeps the old lookup-based path for accounts that do hold a read
    scope (bring-your-own credentials).
    """
    svc = _svc()
    if args.id:
        _require_scope(READ_SCOPE, "reply --id")
        res = gmail_api.reply_message(
            svc, message_id=args.id, body=_read_body(args), cc=args.cc, bcc=args.bcc
        )
        return {"sent": True, "id": res.get("id"), "thread_id": res.get("threadId")}

    if not (args.to and args.subject and args.in_reply_to):
        raise SystemExit(
            "reply needs either --id (requires a Gmail read scope) or "
            "--to/--subject/--in-reply-to. Get the original's Message-ID from the "
            "imap-email skill (`get --message-id ...` or a new_email event's "
            "`message_id`)."
        )
    references = args.references or args.in_reply_to
    res = gmail_api.send_message(
        svc,
        to=args.to,
        subject=gmail_api.compose_reply_subject(args.subject),
        body=_read_body(args),
        cc=args.cc,
        bcc=args.bcc,
        thread_id=args.thread_id,
        headers={"In-Reply-To": args.in_reply_to, "References": references},
    )
    return {"sent": True, "id": res.get("id"), "thread_id": res.get("threadId")}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gmail", description="Gmail via OAuth (cremind-connect).")
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
        "list", help="list INBOX messages (needs a read scope: bring-your-own credentials)"
    )
    sp.add_argument("--query")
    sp.add_argument("--max-results", type=int, default=10, dest="max_results")
    sp.add_argument("--detail", choices=["summary", "full"], default="summary")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser(
        "search", help="search all mail (needs a read scope: bring-your-own credentials)"
    )
    sp.add_argument("--query", required=True)
    sp.add_argument("--max-results", type=int, default=10, dest="max_results")
    sp.add_argument("--detail", choices=["summary", "full"], default="summary")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser(
        "get", help="get a message by id (needs a read scope: bring-your-own credentials)"
    )
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

    sp = sub.add_parser(
        "reply",
        help="reply in-thread using the original's Message-ID (from the imap-email skill)",
    )
    sp.add_argument("--to", action="append", help="recipient(s) — the original sender")
    sp.add_argument("--subject", help="the original subject ('Re: ' is added if missing)")
    sp.add_argument(
        "--in-reply-to",
        dest="in_reply_to",
        help="the original RFC822 Message-ID, e.g. <abc@mail.example.com>",
    )
    sp.add_argument(
        "--references",
        help="References header (defaults to --in-reply-to)",
    )
    sp.add_argument(
        "--thread-id",
        dest="thread_id",
        help="Gmail thread id, if you already have one (optional; headers thread on their own)",
    )
    sp.add_argument(
        "--id",
        help="Gmail message id to reply to (needs a read scope: bring-your-own credentials)",
    )
    sp.add_argument("--cc", action="append")
    sp.add_argument("--bcc", action="append")
    sp.add_argument("--body")
    sp.add_argument("--body-file", dest="body_file")
    sp.set_defaults(func=cmd_reply)

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
