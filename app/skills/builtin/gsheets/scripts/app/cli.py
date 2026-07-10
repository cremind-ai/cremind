"""argparse CLI for the gsheets skill: link + spreadsheet value/metadata verbs.

Execution-only (no events): Google has no push API for spreadsheet content, so
for file-level change events on a spreadsheet, use the gdrive skill.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from . import config, sheets_api
from .google import auth
from .google.discovery import Discovery, DiscoveryError

_FALLBACK_SCOPES = ["openid", "email", "https://www.googleapis.com/auth/spreadsheets"]

# Match a spreadsheet id in a full URL (…/spreadsheets/d/<id>/…) or accept a bare id.
_URL_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def _resolve_client() -> tuple[str, str, list[str]]:
    disc = Discovery(config.CREMIND_CONNECT_URL)
    try:
        creds = disc.credentials()
    except DiscoveryError as e:
        raise SystemExit(f"Could not reach cremind-connect at {config.CREMIND_CONNECT_URL}: {e}")
    # Scopes come from the discovery doc when cremind-connect advertises the
    # "sheets" resource; older brokers won't, so fall back to the built-in list.
    try:
        scopes = disc.scopes("sheets")
    except DiscoveryError:
        scopes = []
    # Env (scripts/.env) overrides win; otherwise use the values cremind-connect
    # serves, so the org can rotate the client id/secret without a client update.
    client_id = config.GOOGLE_CLIENT_ID or creds.get("clientId", "")
    client_secret = config.GOOGLE_CLIENT_SECRET or creds.get("clientSecret", "")
    if not client_id:
        raise SystemExit("No GOOGLE_CLIENT_ID (set it in scripts/.env or ensure cremind-connect is reachable).")
    if not scopes:
        scopes = list(_FALLBACK_SCOPES)
    return client_id, client_secret, scopes


def _svc():
    creds, _ = auth.get_credentials(config.TOKEN_PATH)
    return sheets_api.build_service(creds)


def _extract_id(value: str) -> str:
    m = _URL_ID.search(value or "")
    return m.group(1) if m else (value or "").strip()


def _sheet_id(args) -> str:
    raw = getattr(args, "spreadsheet", None) or config.SPREADSHEET_ID
    if not raw:
        raise SystemExit("No spreadsheet specified (pass --spreadsheet or set SPREADSHEET_ID in scripts/.env).")
    return _extract_id(raw)


def _read_values(args) -> list[list[Any]]:
    if getattr(args, "values", None):
        raw = args.values
    elif getattr(args, "values_file", None):
        with open(args.values_file, encoding="utf-8") as f:
            raw = f.read()
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        raise SystemExit("No values provided (pass --values '<JSON 2D array>', --values-file PATH, or pipe JSON on stdin).")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"--values must be a JSON 2D array (e.g. '[[\"a\",\"b\"],[\"c\",\"d\"]]'): {e}. "
            "If the JSON contains quotes/apostrophes, write it to a file and pass "
            "--values-file PATH instead of inline --values (shell quoting mangles it)."
        )
    if not isinstance(data, list) or (data and not all(isinstance(row, list) for row in data)):
        raise SystemExit("--values must be a JSON 2D array (list of rows).")
    return data


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
    ss = sheets_api.create_spreadsheet(svc, title=args.title, tabs=args.tab)
    return {
        "created": True,
        "id": ss.get("spreadsheetId"),
        "url": ss.get("spreadsheetUrl"),
        "tabs": [s["properties"]["title"] for s in ss.get("sheets", [])],
    }


def cmd_info(args) -> Any:
    svc = _svc()
    meta = sheets_api.get_metadata(svc, spreadsheet_id=_sheet_id(args))
    props = meta.get("properties", {}) or {}
    return {
        "id": meta.get("spreadsheetId"),
        "url": meta.get("spreadsheetUrl"),
        "title": props.get("title"),
        "locale": props.get("locale"),
        "time_zone": props.get("timeZone"),
        "tabs": [
            {
                "sheet_id": s["properties"].get("sheetId"),
                "title": s["properties"].get("title"),
                "index": s["properties"].get("index"),
                "rows": (s["properties"].get("gridProperties", {}) or {}).get("rowCount"),
                "columns": (s["properties"].get("gridProperties", {}) or {}).get("columnCount"),
            }
            for s in meta.get("sheets", [])
        ],
    }


_RENDER = {"formatted": "FORMATTED_VALUE", "unformatted": "UNFORMATTED_VALUE", "formula": "FORMULA"}


def cmd_read(args) -> Any:
    svc = _svc()
    ranges = sheets_api.get_values(
        svc,
        spreadsheet_id=_sheet_id(args),
        ranges=args.range,
        value_render_option=_RENDER[args.render],
    )
    return [{"range": r.get("range"), "values": r.get("values", [])} for r in ranges]


def cmd_update(args) -> Any:
    svc = _svc()
    values = _read_values(args)
    resp = sheets_api.update_values(
        svc,
        spreadsheet_id=_sheet_id(args),
        range_a1=args.range,
        values=values,
        value_input_option="RAW" if args.raw else "USER_ENTERED",
    )
    return {
        "updated": True,
        "range": resp.get("updatedRange"),
        "updated_rows": resp.get("updatedRows"),
        "updated_cells": resp.get("updatedCells"),
    }


def cmd_append(args) -> Any:
    svc = _svc()
    values = _read_values(args)
    resp = sheets_api.append_values(
        svc,
        spreadsheet_id=_sheet_id(args),
        range_a1=args.range,
        values=values,
        value_input_option="RAW" if args.raw else "USER_ENTERED",
    )
    updates = resp.get("updates", {}) or {}
    return {
        "appended": True,
        "range": updates.get("updatedRange"),
        "updated_rows": updates.get("updatedRows"),
        "updated_cells": updates.get("updatedCells"),
    }


def cmd_clear(args) -> Any:
    svc = _svc()
    resp = sheets_api.clear_values(svc, spreadsheet_id=_sheet_id(args), range_a1=args.range)
    return {"cleared": True, "range": resp.get("clearedRange")}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gsheets", description="Google Sheets via OAuth (cremind-connect).")
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

    sp = sub.add_parser("create", help="create a new spreadsheet")
    sp.add_argument("--title", required=True)
    sp.add_argument("--tab", action="append", help="initial tab name (repeatable)")
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("info", help="spreadsheet metadata (title, tabs, dimensions)")
    sp.add_argument("--spreadsheet", help="spreadsheet id or URL (default: SPREADSHEET_ID)")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("read", help="read one or more A1 ranges")
    sp.add_argument("--spreadsheet", help="spreadsheet id or URL (default: SPREADSHEET_ID)")
    sp.add_argument("--range", action="append", required=True, help="A1 range, e.g. 'Sheet1!A1:D10' or whole tab 'Sheet1' (repeatable)")
    sp.add_argument("--render", choices=list(_RENDER), default="formatted")
    sp.set_defaults(func=cmd_read)

    def _values_args(sp_):
        sp_.add_argument("--values", help="JSON 2D array, e.g. '[[\"a\",1],[\"b\",2]]'")
        sp_.add_argument("--values-file", dest="values_file", help="path to a file containing the JSON 2D array")
        sp_.add_argument("--raw", action="store_true", help="write values as RAW (default USER_ENTERED)")

    sp = sub.add_parser("update", help="overwrite a range with values (values via --values/--values-file/stdin)")
    sp.add_argument("--spreadsheet", help="spreadsheet id or URL (default: SPREADSHEET_ID)")
    sp.add_argument("--range", required=True, help="A1 range to write, e.g. 'Sheet1!A1'")
    _values_args(sp)
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("append", help="append rows after the last row of a table")
    sp.add_argument("--spreadsheet", help="spreadsheet id or URL (default: SPREADSHEET_ID)")
    sp.add_argument("--range", required=True, help="table anchor range, e.g. 'Sheet1' or 'Sheet1!A1'")
    _values_args(sp)
    sp.set_defaults(func=cmd_append)

    sp = sub.add_parser("clear", help="clear values from a range (keeps formatting)")
    sp.add_argument("--spreadsheet", help="spreadsheet id or URL (default: SPREADSHEET_ID)")
    sp.add_argument("--range", required=True, help="A1 range to clear")
    sp.set_defaults(func=cmd_clear)

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
