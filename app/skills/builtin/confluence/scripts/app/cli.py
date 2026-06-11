"""argparse CLI for the confluence skill: link + space/page verbs + CQL search."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config, confluence_api, formatter
from .atlassian import auth
from .atlassian.discovery import Discovery, DiscoveryError


def _disc() -> Discovery:
    return Discovery(config.CREMIND_CONNECT_URL)


def _resolve_client() -> tuple[str, list[str]]:
    disc = _disc()
    try:
        client_id = config.ATLASSIAN_CLIENT_ID or disc.client_id()
        scopes = disc.scopes("confluence")
    except DiscoveryError as e:
        raise SystemExit(f"Could not reach cremind-connect at {config.CREMIND_CONNECT_URL}: {e}")
    if not client_id:
        raise SystemExit("No Atlassian client id (set ATLASSIAN_CLIENT_ID or ensure cremind-connect is reachable).")
    if not scopes:
        scopes = [
            "read:confluence-content.all",
            "write:confluence-content",
            "read:confluence-space.summary",
            "search:confluence",
            "read:me",
            "offline_access",
        ]
    return client_id, scopes


def _client() -> tuple[confluence_api.ConfluenceClient, dict[str, Any]]:
    access_token, data = auth.get_access_token(config.TOKEN_PATH, config.CREMIND_CONNECT_URL)
    cloud_id = data.get("cloud_id", "")
    if not cloud_id:
        raise SystemExit("No cloud id stored; re-run link.")
    return confluence_api.ConfluenceClient(access_token, cloud_id), data


def _emit(result: Any, args, *, kind: str = "") -> None:
    as_json = getattr(args, "json", False) or not sys.stdout.isatty()
    if as_json or not isinstance(result, list):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if kind == "spaces":
        print(formatter.format_space_list(result))
    elif kind == "pages":
        print(formatter.format_page_list(result))
    elif kind == "search":
        print(formatter.format_search_results(result))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


# --- commands ---

def cmd_link(_args) -> Any:
    client_id, scopes = _resolve_client()
    data = auth.link(
        token_path=config.TOKEN_PATH,
        connect_url=config.CREMIND_CONNECT_URL,
        client_id=client_id,
        scopes=scopes,
        redirect_uri=config.OAUTH_REDIRECT_URI,
        site_url_hint=config.CONFLUENCE_SITE_URL,
    )
    return {"linked": True, "email": data["email"], "site_url": data.get("site_url"), "account_key": data["account_key"]}


def cmd_complete_link(args) -> Any:
    """Finish a link started in another (still-running) `link` by handing it the
    redirect URL the browser was sent to. For remote/Kubernetes deployments where
    the registered loopback callback can't reach the backend; run `status` after."""
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
    return {
        "linked": True,
        "email": data.get("email"),
        "site_url": data.get("site_url"),
        "cloud_id": data.get("cloud_id"),
        "account_key": data.get("account_key"),
        "scopes": data.get("scopes"),
    }


def cmd_spaces(args) -> Any:
    svc, _ = _client()
    return [{"id": s.get("id"), "key": s.get("key"), "name": s.get("name")} for s in svc.spaces(limit=args.limit)]


def cmd_pages(args) -> Any:
    svc, _ = _client()
    rows = svc.pages(space_id=args.space, title=args.title, limit=args.limit)
    return [{"id": p.get("id"), "title": p.get("title"), "space_id": p.get("spaceId")} for p in rows]


def cmd_get(args) -> Any:
    svc, data = _client()
    page = svc.get_page(args.id)
    parsed = formatter.parse_page(page)
    parsed["url"] = formatter.page_url(data.get("site_url", ""), page)
    return parsed


def cmd_create(args) -> Any:
    svc, data = _client()
    page = svc.create_page(space_id=args.space, title=args.title, body=_read_body(args))
    return {"created": True, "id": page.get("id"), "title": page.get("title"), "url": formatter.page_url(data.get("site_url", ""), page)}


def cmd_update(args) -> Any:
    svc, data = _client()
    page = svc.update_page(args.id, title=args.title, body=_read_body(args) if (args.body or args.body_file or not sys.stdin.isatty()) else None)
    return {"updated": True, "id": page.get("id"), "title": page.get("title"), "version": (page.get("version") or {}).get("number")}


def cmd_search(args) -> Any:
    svc, _ = _client()
    return svc.search(args.cql, limit=args.limit)


def _read_body(args) -> str:
    if getattr(args, "body", None) is not None:
        return args.body
    if getattr(args, "body_file", None):
        with open(args.body_file, "r", encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="confluence", description="Confluence Cloud via OAuth (cremind-connect).")
    p.add_argument("--json", action="store_true", help="force JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("link", help="link an Atlassian account (backend-mediated 3LO)").set_defaults(func=cmd_link, _kind="")
    sp = sub.add_parser(
        "complete-link",
        help="finish linking by pasting the URL Atlassian redirected you to (remote/Kubernetes)",
    )
    sp.add_argument("--response", required=True, help="the full redirect URL (or its code=...&state=... query)")
    sp.set_defaults(func=cmd_complete_link, _kind="")
    sub.add_parser("status", help="show link status").set_defaults(func=cmd_status, _kind="")

    sp = sub.add_parser("spaces", help="list spaces")
    sp.add_argument("--limit", type=int, default=25)
    sp.set_defaults(func=cmd_spaces, _kind="spaces")

    sp = sub.add_parser("pages", help="list pages (optionally by space/title)")
    sp.add_argument("--space", help="space id")
    sp.add_argument("--title", help="exact title filter")
    sp.add_argument("--limit", type=int, default=25)
    sp.set_defaults(func=cmd_pages, _kind="pages")

    sp = sub.add_parser("get", help="get a page by id (with body)")
    sp.add_argument("--id", required=True)
    sp.set_defaults(func=cmd_get, _kind="")

    sp = sub.add_parser("create", help="create a page")
    sp.add_argument("--space", required=True, help="space id")
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", help="body (plain text); also --body-file or stdin")
    sp.add_argument("--body-file", dest="body_file")
    sp.set_defaults(func=cmd_create, _kind="")

    sp = sub.add_parser("update", help="update a page (title and/or body)")
    sp.add_argument("--id", required=True)
    sp.add_argument("--title")
    sp.add_argument("--body")
    sp.add_argument("--body-file", dest="body_file")
    sp.set_defaults(func=cmd_update, _kind="")

    sp = sub.add_parser("search", help="search content with CQL")
    sp.add_argument("--cql", required=True, help='Confluence Query Language, e.g. \'text ~ "roadmap" AND type = page\'')
    sp.add_argument("--limit", type=int, default=25)
    sp.set_defaults(func=cmd_search, _kind="search")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except auth.AuthError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    except confluence_api.ApiError as e:
        print(json.dumps({"error": str(e), "status": e.status}), file=sys.stderr)
        return 3
    _emit(result, args, kind=getattr(args, "_kind", ""))
    return 0
