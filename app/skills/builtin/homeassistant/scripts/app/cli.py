import argparse
import json
import sys
import time
from typing import Optional

from . import auth, config, formatter, operations


def _warn_if_listener_not_running() -> None:
    stale_threshold = config.WS_RECV_TIMEOUT * 4

    if not config.HEARTBEAT_FILE.exists():
        print(
            "warning: event listener is not running. "
            "Run `uv run scripts/event_listener.py` to begin capturing Home Assistant "
            "state changes to events/.",
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
    if table == "entities" and isinstance(data, list):
        print(formatter.format_entities_table(data))
        return
    if table == "state" and isinstance(data, dict):
        print(formatter.format_state_table(data))
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="homeassistant",
        description="Home Assistant CLI over the REST API. Reads entity states and calls "
        "services using a Long-Lived Access Token. Run the event listener "
        "(`uv run scripts/event_listener.py`) separately for real-time state-change events.",
    )
    p.add_argument("--json", action="store_true", help="force JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate auth + connectivity and show instance info")

    sp = sub.add_parser(
        "link",
        help="authorize via the OAuth 2.0 browser flow (only needed when HA_TOKEN is not set)",
    )
    sp.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    sp.add_argument("--timeout", type=int, default=300, help="seconds to wait for consent (default 300)")

    sub.add_parser("unlink", help="revoke and remove the stored OAuth tokens")

    sp = sub.add_parser("list-entities", help="list entities (id, state, friendly name)")
    sp.add_argument("--domain", default=None, help="filter by domain (e.g. light, switch, sensor)")
    sp.add_argument("--query", default=None, help="case-insensitive substring match on entity_id/friendly_name")
    sp.add_argument("--max-results", type=int, default=200)

    sp = sub.add_parser("get-state", help="get the full state + attributes of one entity")
    sp.add_argument("--entity", required=True, help="entity_id, e.g. light.kitchen")

    sp = sub.add_parser("states", help="dump full state + attributes for entities")
    sp.add_argument("--domain", default=None)
    sp.add_argument("--query", default=None)

    sp = sub.add_parser("call-service", help="call a service to control devices, e.g. light.turn_on")
    sp.add_argument("--domain", required=True, help="service domain, e.g. light")
    sp.add_argument("--service", required=True, help="service name, e.g. turn_on")
    sp.add_argument("--entity", default=None, help='target entity_id (sugar for --data \'{"entity_id": ...}\')')
    sp.add_argument("--data", default=None, help="service data as a JSON object string")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    as_json = args.json or (not sys.stdout.isatty())

    _warn_if_listener_not_running()

    try:
        if args.command == "check":
            _emit(operations.check(), as_json=as_json)

        elif args.command == "link":
            _emit(auth.link(open_browser=not args.no_browser, timeout=args.timeout), as_json=True)

        elif args.command == "unlink":
            _emit(auth.unlink(), as_json=True)

        elif args.command == "list-entities":
            rows = operations.list_entities(
                domain=args.domain, query=args.query, max_results=args.max_results
            )
            _emit(rows, as_json=as_json, table="entities")

        elif args.command == "get-state":
            _emit(operations.get_state(args.entity), as_json=as_json, table="state")

        elif args.command == "states":
            rows = operations.states(domain=args.domain, query=args.query)
            _emit(rows, as_json=as_json)

        elif args.command == "call-service":
            data = None
            if args.data:
                try:
                    data = json.loads(args.data)
                except json.JSONDecodeError as e:
                    raise ValueError(f"--data must be a valid JSON object: {e}")
                if not isinstance(data, dict):
                    raise ValueError('--data must be a JSON object (e.g. \'{"entity_id": "light.kitchen"}\')')
            if not args.entity and not data:
                print(
                    "warning: call-service called with no --entity and no --data; this may affect "
                    "ALL matching devices (e.g. every light). Pass --entity to target one device.",
                    file=sys.stderr,
                )
            result = operations.call_service(args.domain, args.service, data=data, entity=args.entity)
            _emit(result, as_json=True)

        else:
            parser.error(f"unknown command: {args.command}")
    except Exception as e:
        err = {"ok": False, "error": str(e), "error_type": type(e).__name__}
        print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0
