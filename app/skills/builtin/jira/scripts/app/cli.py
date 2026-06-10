"""argparse CLI for the jira skill: link + issue verbs + webhook helpers."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config, formatter, jira_api
from .atlassian import auth
from .atlassian.discovery import Discovery, DiscoveryError

DEFAULT_EVENTS = ["jira:issue_created", "jira:issue_updated", "jira:issue_deleted", "comment_created"]
LIST_FIELDS = ["summary", "status", "issuetype", "assignee", "priority", "updated"]


def _disc() -> Discovery:
    return Discovery(config.CREMIND_CONNECT_URL)


def _resolve_client() -> tuple[str, list[str]]:
    disc = _disc()
    try:
        client_id = config.ATLASSIAN_CLIENT_ID or disc.client_id()
        scopes = disc.scopes("jira")
    except DiscoveryError as e:
        raise SystemExit(f"Could not reach cremind-connect at {config.CREMIND_CONNECT_URL}: {e}")
    if not client_id:
        raise SystemExit("No Atlassian client id (set ATLASSIAN_CLIENT_ID or ensure cremind-connect is reachable).")
    if not scopes:
        scopes = [
            "read:jira-work",
            "write:jira-work",
            "read:jira-user",
            "manage:jira-webhook",
            "read:me",
            "offline_access",
        ]
    return client_id, scopes


def _client() -> tuple[jira_api.JiraClient, dict[str, Any]]:
    access_token, data = auth.get_access_token(config.TOKEN_PATH, config.CREMIND_CONNECT_URL)
    cloud_id = data.get("cloud_id", "")
    if not cloud_id:
        raise SystemExit("No cloud id stored; re-run link.")
    return jira_api.JiraClient(access_token, cloud_id), data


def _emit(result: Any, args) -> None:
    as_json = getattr(args, "json", False) or not sys.stdout.isatty()
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif isinstance(result, list):
        print(formatter.format_list(result))
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
        port=config.OAUTH_CALLBACK_PORT,
        site_url_hint=config.JIRA_SITE_URL,
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


def cmd_myself(_args) -> Any:
    svc, _ = _client()
    me = svc.myself()
    return {"account_id": me.get("accountId"), "name": me.get("displayName"), "email": me.get("emailAddress")}


def cmd_projects(_args) -> Any:
    svc, _ = _client()
    return [{"key": p.get("key"), "name": p.get("name"), "id": p.get("id")} for p in svc.projects()]


def cmd_search(args) -> Any:
    svc, _ = _client()
    resp = svc.search(args.query, fields=LIST_FIELDS, max_results=args.max_results)
    return [formatter.parse_issue(i) for i in resp.get("issues", []) or []]


def cmd_get(args) -> Any:
    svc, data = _client()
    issue = svc.get_issue(args.key)
    parsed = formatter.parse_issue(issue)
    parsed["url"] = formatter.issue_url(data.get("site_url", ""), parsed["key"])
    return parsed


def cmd_create(args) -> Any:
    svc, data = _client()
    res = svc.create_issue(
        project_key=args.project,
        summary=args.summary,
        issue_type=args.type,
        description=_read_body(args),
    )
    key = res.get("key", "")
    return {"created": True, "key": key, "url": formatter.issue_url(data.get("site_url", ""), key)}


def cmd_comment(args) -> Any:
    svc, _ = _client()
    res = svc.add_comment(args.key, _read_body(args))
    return {"commented": True, "key": args.key, "id": res.get("id")}


def cmd_transitions(args) -> Any:
    svc, _ = _client()
    return [{"id": t.get("id"), "name": t.get("name"), "to": (t.get("to") or {}).get("name")} for t in svc.get_transitions(args.key)]


def cmd_transition(args) -> Any:
    svc, _ = _client()
    svc.transition(args.key, args.to)
    return {"transitioned": True, "key": args.key, "transition_id": args.to}


def _webhook_url(account_key: str) -> str:
    base = _disc().webhook_url("jira")
    import secrets

    return f"{base}?rk={account_key}&n={secrets.token_urlsafe(8)}"


def cmd_watch(_args) -> Any:
    svc, data = _client()
    url = _webhook_url(data["account_key"])
    wjql = jira_api.webhook_jql(config.JIRA_WEBHOOK_JQL, data.get("account_id", ""))
    res = svc.register_webhook(url=url, events=DEFAULT_EVENTS, jql=wjql)
    results = res.get("webhookRegistrationResult", []) or []
    ids = [r.get("createdWebhookId") for r in results if r.get("createdWebhookId")]
    errors = [r.get("errors") for r in results if r.get("errors")]
    return {"watching": bool(ids), "webhook_ids": ids, "webhook_jql": wjql, "errors": errors}


def cmd_unwatch(_args) -> Any:
    svc, _ = _client()
    existing = svc.list_webhooks()
    ids = [w.get("id") for w in existing if w.get("id")]
    if ids:
        svc.delete_webhooks(ids)
    return {"watching": False, "deleted": ids}


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
    p = argparse.ArgumentParser(prog="jira", description="Jira Cloud via OAuth (cremind-connect).")
    p.add_argument("--json", action="store_true", help="force JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("link", help="link an Atlassian account (backend-mediated 3LO)").set_defaults(func=cmd_link)
    sp = sub.add_parser(
        "complete-link",
        help="finish linking by pasting the URL Atlassian redirected you to (remote/Kubernetes)",
    )
    sp.add_argument("--response", required=True, help="the full redirect URL (or its code=...&state=... query)")
    sp.set_defaults(func=cmd_complete_link)
    sub.add_parser("status", help="show link status").set_defaults(func=cmd_status)
    sub.add_parser("myself", help="show the authenticated Jira user").set_defaults(func=cmd_myself)
    sub.add_parser("projects", help="list accessible projects").set_defaults(func=cmd_projects)

    sp = sub.add_parser("search", help="search issues with JQL")
    sp.add_argument("--query", required=True, help="JQL, e.g. 'assignee = currentUser() AND statusCategory != Done'")
    sp.add_argument("--max-results", type=int, default=25, dest="max_results")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("get", help="get an issue by key")
    sp.add_argument("--key", required=True)
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("create", help="create an issue")
    sp.add_argument("--project", required=True, help="project key, e.g. ABC")
    sp.add_argument("--summary", required=True)
    sp.add_argument("--type", default="Task", help="issue type name (default: Task)")
    sp.add_argument("--body", help="description (plain text); also --body-file or stdin")
    sp.add_argument("--body-file", dest="body_file")
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("comment", help="add a comment to an issue")
    sp.add_argument("--key", required=True)
    sp.add_argument("--body")
    sp.add_argument("--body-file", dest="body_file")
    sp.set_defaults(func=cmd_comment)

    sp = sub.add_parser("transitions", help="list available transitions for an issue")
    sp.add_argument("--key", required=True)
    sp.set_defaults(func=cmd_transitions)

    sp = sub.add_parser("transition", help="transition an issue (use 'transitions' for ids)")
    sp.add_argument("--key", required=True)
    sp.add_argument("--to", required=True, help="transition id")
    sp.set_defaults(func=cmd_transition)

    sub.add_parser("watch", help="register the Jira dynamic webhook once (the listener does this automatically)").set_defaults(func=cmd_watch)
    sub.add_parser("unwatch", help="delete this app's Jira dynamic webhooks").set_defaults(func=cmd_unwatch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except auth.AuthError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    except jira_api.ApiError as e:
        print(json.dumps({"error": str(e), "status": e.status}), file=sys.stderr)
        return 3
    _emit(result, args)
    return 0
