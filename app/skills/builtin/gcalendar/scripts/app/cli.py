"""argparse CLI for the gcalendar skill: link + calendar/event verbs + watch helpers."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import config, formatter, gcal_api
from .google import auth
from .google.account_key import base32_encode
from .google.discovery import Discovery, DiscoveryError


def _resolve_client() -> tuple[str, str, list[str]]:
    disc = Discovery(config.CREMIND_CONNECT_URL)
    try:
        creds = disc.credentials()
        scopes = disc.scopes()
    except DiscoveryError as e:
        raise SystemExit(f"Could not reach cremind-connect at {config.CREMIND_CONNECT_URL}: {e}")
    # Env (scripts/.env) overrides win; otherwise use the values cremind-connect
    # serves, so the org can rotate the client id/secret without a client update.
    client_id = config.GOOGLE_CLIENT_ID or creds.get("clientId", "")
    client_secret = config.GOOGLE_CLIENT_SECRET or creds.get("clientSecret", "")
    if not client_id:
        raise SystemExit("No GOOGLE_CLIENT_ID (set it in scripts/.env or ensure cremind-connect is reachable).")
    if not scopes:
        scopes = ["openid", "email", "https://www.googleapis.com/auth/calendar"]
    return client_id, client_secret, scopes


def _svc():
    creds, _ = auth.get_credentials(config.TOKEN_PATH)
    return gcal_api.build_service(creds)


def _cal_id(args) -> str:
    return getattr(args, "calendar", None) or config.CALENDAR_ID


def _emit(result: Any, args) -> None:
    as_json = getattr(args, "json", False) or not sys.stdout.isatty()
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def _to_iso_min(day: str | None) -> str | None:
    return f"{day}T00:00:00Z" if day else None


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
        port=config.OAUTH_CALLBACK_PORT,
        bind_addr=config.OAUTH_BIND_ADDR,
    )
    return {"linked": True, "email": data["email"], "account_key": data["account_key"]}


def cmd_status(_args) -> Any:
    try:
        data = auth.load_account(config.TOKEN_PATH)
    except auth.AuthError:
        return {"linked": False}
    return {"linked": True, "email": data.get("email"), "account_key": data.get("account_key"), "scopes": data.get("scopes")}


def cmd_list_calendars(_args) -> Any:
    svc = _svc()
    return [
        {"id": c.get("id"), "summary": c.get("summary"), "primary": c.get("primary", False), "access": c.get("accessRole")}
        for c in gcal_api.list_calendars(svc)
    ]


def cmd_list(args) -> Any:
    svc = _svc()
    items = gcal_api.list_events(
        svc,
        calendar_id=_cal_id(args),
        time_min=_to_iso_min(args.since),
        time_max=_to_iso_min(args.before),
        query=args.query,
        max_results=args.max_results,
    )
    return [formatter.parse_event(ev, calendar=_cal_id(args)) for ev in items]


def cmd_get(args) -> Any:
    svc = _svc()
    return formatter.parse_event(gcal_api.get_event(svc, calendar_id=_cal_id(args), event_id=args.id), calendar=_cal_id(args))


def cmd_create(args) -> Any:
    svc = _svc()
    body = formatter.build_event_body(
        summary=args.summary,
        start=args.start,
        end=args.end,
        location=args.location,
        description=args.description,
        attendees=args.attendees,
        all_day=args.all_day,
    )
    ev = gcal_api.create_event(svc, calendar_id=_cal_id(args), body=body)
    return {"created": True, "id": ev.get("id"), "html_link": ev.get("htmlLink")}


def cmd_update(args) -> Any:
    svc = _svc()
    body = formatter.build_event_body(
        summary=args.summary,
        start=args.start,
        end=args.end,
        location=args.location,
        description=args.description,
        attendees=args.attendees,
        all_day=args.all_day,
    )
    ev = gcal_api.update_event(svc, calendar_id=_cal_id(args), event_id=args.id, body=body)
    return {"updated": True, "id": ev.get("id"), "html_link": ev.get("htmlLink")}


def cmd_delete(args) -> Any:
    svc = _svc()
    gcal_api.delete_event(svc, calendar_id=_cal_id(args), event_id=args.id)
    return {"deleted": True, "id": args.id}


def cmd_watch(args) -> Any:
    disc = Discovery(config.CREMIND_CONNECT_URL)
    creds, data = auth.get_credentials(config.TOKEN_PATH)
    svc = gcal_api.build_service(creds)
    channel_id = f"cm.{data['account_key']}.{base32_encode(os.urandom(16))[:16]}"
    res = gcal_api.watch(
        svc,
        calendar_id=_cal_id(args),
        channel_id=channel_id,
        address=disc.calendar_webhook_url(),
        token=base32_encode(os.urandom(16))[:24],
    )
    return {"watching": True, "channel_id": channel_id, "resource_id": res.get("resourceId"), "expiration": res.get("expiration")}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gcalendar", description="Google Calendar via OAuth (cremind-connect).")
    p.add_argument("--json", action="store_true", help="force JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("link", help="link a Google account (loopback PKCE)")
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_link)

    sub.add_parser("status", help="show link status").set_defaults(func=cmd_status)
    sub.add_parser("list-calendars", help="list calendars").set_defaults(func=cmd_list_calendars)

    sp = sub.add_parser("list", help="list events")
    sp.add_argument("--calendar")
    sp.add_argument("--since", help="YYYY-MM-DD")
    sp.add_argument("--before", help="YYYY-MM-DD")
    sp.add_argument("--query")
    sp.add_argument("--max-results", type=int, default=50, dest="max_results")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("get", help="get an event by id")
    sp.add_argument("--id", required=True)
    sp.add_argument("--calendar")
    sp.set_defaults(func=cmd_get)

    def _event_args(sp_, *, require_core: bool):
        sp_.add_argument("--summary", required=require_core)
        sp_.add_argument("--start", required=require_core, help="ISO 8601 or YYYY-MM-DD (all-day)")
        sp_.add_argument("--end", required=require_core, help="ISO 8601 or YYYY-MM-DD (inclusive for all-day)")
        sp_.add_argument("--location")
        sp_.add_argument("--description")
        sp_.add_argument("--attendees", action="append")
        sp_.add_argument("--all-day", action="store_true", dest="all_day")
        sp_.add_argument("--calendar")

    sp = sub.add_parser("create", help="create an event")
    _event_args(sp, require_core=True)
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("update", help="update an event (omitted fields preserved)")
    sp.add_argument("--id", required=True)
    _event_args(sp, require_core=False)
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("delete", help="delete an event")
    sp.add_argument("--id", required=True)
    sp.add_argument("--calendar")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("watch", help="establish the calendar watch once")
    sp.add_argument("--calendar")
    sp.set_defaults(func=cmd_watch)
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
