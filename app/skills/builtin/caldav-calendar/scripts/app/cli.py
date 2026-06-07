import argparse
import json
import sys
import time
from typing import Optional

from . import config, formatter, operations


def _warn_if_listener_not_running() -> None:
    stale_threshold = config.POLL_INTERVAL * 4

    if not config.HEARTBEAT_FILE.exists():
        print(
            "warning: event listener is not running. "
            "Run `uv run scripts/event_listener.py` to begin capturing calendar changes to events/.",
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


def _emit(data, as_json: bool, table: Optional[str] = None) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if table == "events" and isinstance(data, list):
        print(formatter.format_table(data))
        return
    if table == "calendars" and isinstance(data, list):
        print(formatter.format_calendars_table(data))
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="caldav-calendar",
        description="Calendar CLI over CalDAV (iCloud, Fastmail, Nextcloud, Posteo, Radicale, "
        "generic CalDAV servers). Uses username + password — no OAuth2. "
        "Google Calendar and Microsoft are NOT supported.",
    )
    p.add_argument("--json", action="store_true", help="force JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list-calendars", help="list calendars available on the account")

    sp = sub.add_parser("list", help="list events in a date range (defaults to today -> +30 days)")
    sp.add_argument("--calendar", default=None, help="calendar name (default: CALDAV_CALENDAR env or first calendar)")
    sp.add_argument("--since", default=None, help="YYYY-MM-DD or ISO 8601 (inclusive)")
    sp.add_argument("--before", default=None, help="YYYY-MM-DD or ISO 8601 (exclusive)")
    sp.add_argument("--query", default=None, help="case-insensitive substring match against summary/location/description")
    sp.add_argument("--max-results", type=int, default=50)
    sp.add_argument("--detail", choices=["summary", "full"], default="summary")

    sp = sub.add_parser("get", help="get full details of one event")
    sp.add_argument("--uid", required=True, help="iCalendar UID (from `list` output)")
    sp.add_argument("--calendar", default=None)

    sp = sub.add_parser("create", help="create a new event")
    sp.add_argument("--summary", required=True)
    sp.add_argument("--start", required=True, help="ISO 8601 datetime, or YYYY-MM-DD with --all-day")
    sp.add_argument("--end", required=True, help="ISO 8601 datetime, or YYYY-MM-DD (inclusive) with --all-day")
    sp.add_argument("--all-day", action="store_true")
    sp.add_argument("--location", default=None)
    sp.add_argument("--description", default=None)
    sp.add_argument("--attendees", action="append", default=[],
                    help="attendee email (repeatable). Note: NO invitation email is sent.")
    sp.add_argument("--calendar", default=None)

    sp = sub.add_parser("update", help="update an existing event; omitted flags are preserved")
    sp.add_argument("--uid", required=True)
    sp.add_argument("--summary", default=None, help='pass "" (empty string) to clear')
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.add_argument("--all-day", dest="all_day", action="store_true", default=None)
    sp.add_argument("--location", default=None)
    sp.add_argument("--description", default=None)
    sp.add_argument("--attendees", action="append", default=None,
                    help="if provided, REPLACES the existing attendee list")
    sp.add_argument("--calendar", default=None)

    sp = sub.add_parser("delete", help="permanently delete an event (no Trash equivalent in CalDAV)")
    sp.add_argument("--uid", required=True)
    sp.add_argument("--calendar", default=None)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    as_json = args.json or (not sys.stdout.isatty())

    _warn_if_listener_not_running()

    try:
        if args.command == "list-calendars":
            rows = operations.list_calendars()
            _emit(rows, as_json=as_json, table="calendars")

        elif args.command == "list":
            rows = operations.list_events(
                calendar=args.calendar,
                since=args.since,
                before=args.before,
                query=args.query,
                max_results=args.max_results,
                detail=args.detail,
            )
            _emit(rows, as_json=as_json, table="events")

        elif args.command == "get":
            result = operations.get_event(uid=args.uid, calendar=args.calendar)
            _emit(result, as_json=as_json)

        elif args.command == "create":
            result = operations.create_event(
                summary=args.summary,
                start=args.start,
                end=args.end,
                all_day=args.all_day,
                location=args.location,
                description=args.description,
                attendees=args.attendees or None,
                calendar=args.calendar,
            )
            _emit(result, as_json=True)

        elif args.command == "update":
            result = operations.update_event(
                uid=args.uid,
                summary=args.summary,
                start=args.start,
                end=args.end,
                all_day=args.all_day,
                location=args.location,
                description=args.description,
                attendees=args.attendees,
                calendar=args.calendar,
            )
            _emit(result, as_json=True)

        elif args.command == "delete":
            result = operations.delete_event(uid=args.uid, calendar=args.calendar)
            _emit(result, as_json=True)

        else:
            parser.error(f"unknown command: {args.command}")
    except Exception as e:
        err = {"ok": False, "error": str(e), "error_type": type(e).__name__}
        print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0
