"""`cremind tags host ...` — THIS computer as a gateway computer of a Cremind
server elsewhere.

A Cremind in a container or on another machine cannot see this computer's USB
ports. Set up here, this computer searches for, connects and drives the
profile's gateways plugged into it. The Cremind desktop app does all of this
itself; the CLI serves computers without it:

- ``enroll`` — complete the "Set up a gateway computer" link from the Cremind
  page (read from stdin or a file, never from the command line: it holds a
  secret);
- ``run`` — drive the gateways until stopped;
- ``status`` — the enrollment, the local components, whether it runs;
- ``prepare`` — install the gateway components here (the font bundle);
- ``forget`` — stop being a gateway computer (Cremind forgets it too).

Nothing here needs a Cremind sign-in: the computer uses its own credential,
which works only for this computer and the profile that set it up. Nothing
here imports the server at the top: the runtime is loaded inside each command.
"""

from __future__ import annotations

import json
import sys
from typing import Any, NoReturn, Optional

import typer

from app.cli.commands._helpers import graceful_errors

host_app = typer.Typer(
    name="host",
    help="This computer as a gateway computer of a Cremind server elsewhere: enroll, run, status, forget.",
    no_args_is_help=True,
)

SUPERVISED_ENV = "CREMIND_TAG_HOST_SUPERVISED"
EXIT_REVOKED = 3
EXIT_NOT_ENROLLED = 4


def _fail(message: str, code: int = 1) -> NoReturn:
    sys.stderr.write(message.rstrip("\n") + "\n")
    raise typer.Exit(code=code)


def _paths() -> Any:
    from app.tags.hosting.paths import default_paths

    return default_paths()


def _running(paths: Any) -> tuple[bool, Optional[str]]:
    """Whether gateway support runs on this computer now, and the font pack it draws with (when it says)."""
    from app.tags.runtime.connect.instance import is_locked, read_info

    if not is_locked(paths.host_lock):
        return False, None
    pack = read_info(paths.host_lock_info).get("fonts_pack")
    return True, pack if isinstance(pack, str) and pack else None


def _emit(event: str, **fields: Any) -> None:
    sys.stdout.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_link(link_file: Optional[str], events: bool) -> str:
    if events or link_file == "-":
        return sys.stdin.readline().strip()
    if link_file:
        try:
            with open(link_file, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            _fail(f"--link-file: {exc}")
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return typer.prompt("Paste the link from the Cremind page", hide_input=True).strip()


@host_app.command("enroll")
@graceful_errors
def host_enroll(
    link_file: Optional[str] = typer.Option(None, "--link-file", help="A file holding the link from the Cremind "
                                                                      "page (- reads one line from stdin)."),
    yes: bool = typer.Option(False, "--yes", help="Approve without asking (you compared the words)."),
    events: bool = typer.Option(False, "--events", help="JSON lines on stdin/stdout, for the Cremind app: the link "
                                                        "first, then 'approve' or 'decline' after the bound event."),
) -> None:
    """Set this computer up as a gateway computer, from the link on the Cremind page."""
    from app.__version__ import __version__
    from app.tags.runtime.connect.links import LinkError
    from app.tags.runtime.host.enroll import EnrollError, enroll

    link = _read_link(link_file, events)

    def approve(bound: Any) -> bool:
        if events:
            answer = sys.stdin.readline().strip().lower()
            return answer == "approve"
        sys.stdout.write(f"\nCremind:        {bound.server_origin}\nProfile:        {bound.profile_name}\n"
                         f"Words:          {bound.verification_phrase}\n\n")
        if yes:
            return True
        return typer.confirm("Does the Cremind page show the same four words? Set up this computer for this "
                             "profile?", default=False)

    def on_event(name: str, fields: dict[str, Any]) -> None:
        if events:
            _emit(name, **fields)
        elif name == "approved":
            sys.stdout.write("Approved. Now confirm the same words on the Cremind page…\n")
        elif name == "confirmed":
            sys.stdout.write("Confirmed.\n")

    try:
        enrollment = enroll(link, _paths(), approve=approve, on_event=on_event, version=__version__)
    except (EnrollError, LinkError) as exc:
        if events:
            _emit("failed", code=getattr(exc, "code", "failed"), message=str(exc))
            raise typer.Exit(code=1) from None
        _fail(str(exc))
    if not events:
        sys.stdout.write(f"This computer is now a gateway computer of {enrollment.server} for profile "
                         f"{enrollment.profile}. Start it with: cremind tags host run\n")


@host_app.command("run")
@graceful_errors
def host_run() -> None:
    """Drive the gateways plugged into this computer until stopped (Ctrl-C)."""
    import asyncio
    import os
    import signal
    import threading

    from app.tags.hosting.host import HardwareHost
    from app.tags.runtime.host.enroll import EnrollError, load_enrollment

    paths = _paths()
    try:
        enrollment = load_enrollment(paths)
    except EnrollError as exc:
        _fail(str(exc))
    if enrollment is None:
        _fail("This computer is not set up as a gateway computer. Set it up from the Cremind page "
              "(Settings → Tags → Gateway computers), or: cremind tags host enroll", EXIT_NOT_ENROLLED)
    host = HardwareHost(paths, remote=enrollment)

    async def main() -> int:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        if os.environ.get(SUPERVISED_ENV):
            # The Cremind app closes our stdin to ask us to stop (EOF also comes when it dies).
            def watch() -> None:
                try:
                    while sys.stdin.buffer.read(1):
                        pass
                except (OSError, ValueError):
                    pass
                loop.call_soon_threadsafe(stop.set)

            threading.Thread(target=watch, name="stdin watch", daemon=True).start()
        if not await asyncio.to_thread(host.start, loop):
            sys.stderr.write(f"Gateway support did not start: {host.reason or host.state}\n")
            return EXIT_REVOKED if host.state == "revoked" else 1
        sys.stdout.write(f"Driving gateways for {enrollment.profile} on {enrollment.server} "
                         f"(this computer: {enrollment.host_name or enrollment.host_id}). Ctrl-C stops.\n")
        sys.stdout.flush()
        while not stop.is_set() and host.running:
            try:
                await asyncio.wait_for(stop.wait(), 1.0)
            except TimeoutError:
                pass
        state, reason = host.state, host.reason
        await asyncio.to_thread(host.stop, "stopped")
        if state == "revoked":
            sys.stderr.write(f"{reason}\n")
            return EXIT_REVOKED
        if state not in ("running", "stopped"):
            sys.stderr.write(f"Gateway support stopped: {reason or state}\n")
            return 1
        return 0

    code = asyncio.run(main())
    if code:
        raise typer.Exit(code=code)


@host_app.command("status")
@graceful_errors
def host_status(ctx: typer.Context) -> None:
    """This computer's enrollment, its gateway components, and whether it is running."""
    from app.tags.hosting import fonts
    from app.tags.hosting.components import readiness
    from app.tags.hosting.migration import summary
    from app.tags.runtime.host.enroll import EnrollError, load_enrollment

    paths = _paths()
    try:
        enrollment = load_enrollment(paths)
        problem = None
    except EnrollError as exc:
        enrollment, problem = None, str(exc)
    running, loaded = _running(paths)
    doc = {
        "enrolled": enrollment is not None,
        "enrollment": enrollment.public_json() if enrollment is not None else None,
        "problem": problem,
        "running": running,
        # The font pack the running host draws with (when it says), and the components against this release's pin.
        "fonts_pack": loaded,
        "readiness": readiness(paths.assets_dir, fonts.pinned_pack(), loaded),
        # Gateways taken over from the older Cremind Connect on this computer.
        "migration": summary(paths),
    }
    if ctx.obj["mode"].json:
        sys.stdout.write(json.dumps(doc, indent=2) + "\n")
        return
    if enrollment is None:
        sys.stdout.write(problem or "Not set up as a gateway computer (cremind tags host enroll).")
        sys.stdout.write("\n")
    else:
        sys.stdout.write(f"Gateway computer of {enrollment.server} for profile {enrollment.profile} "
                         f"(host {enrollment.host_id}, since {enrollment.enrolled_at}).\n")
    sys.stdout.write(f"Running:    {'yes' if doc['running'] else 'no'}\n")
    ready = doc["readiness"]
    sys.stdout.write(f"Components: {ready.get('state')}\n")
    for component in ready.get("components") or []:
        detail = f" — {component.get('detail')}" if component.get("detail") else ""
        sys.stdout.write(f"  {component.get('key')}: {component.get('state')}{detail}\n")
    moved = doc["migration"]
    if any(moved.values()):
        sys.stdout.write(f"Moved in:   {moved['moved']} gateway(s) from Cremind Connect"
                         + (f", {moved['failed']} to retry at the next start" if moved["failed"] else "")
                         + (f", {moved['rolled_back']} left with Cremind Connect (see the log)"
                            if moved["rolled_back"] else "") + "\n")


@host_app.command("prepare")
@graceful_errors
def host_prepare() -> None:
    """Install the gateway components on this computer (the font bundle tag screens are drawn with)."""
    from app.tags.hosting import fonts
    from app.tags.hosting.components import packages, readiness

    paths = _paths()
    paths.ensure()
    package = packages()
    if package.state != "ready":
        _fail(f"The gateway packages are missing ({package.detail}). Install them with: "
              "pip install \"cremind[tags]\"")
    installed = fonts.ensure_installed(paths.assets_dir, lambda line: sys.stdout.write(line + "\n"))
    sys.stdout.write(installed.message + "\n")
    running, loaded = _running(paths)
    doc = readiness(paths.assets_dir, fonts.pinned_pack(), loaded)
    if doc.get("state") not in ("ready",):
        _fail(f"Components: {doc.get('state')}. See: cremind tags host status")
    if running and installed.pack_id and (installed.changed or loaded != installed.pack_id):
        # The running host loaded its fonts when it started: it keeps the older pack (or none) until then.
        sys.stdout.write(f"Gateway support is running here{f' with font pack {loaded}' if loaded else ''}: "
                         f"restart Cremind here to use pack {installed.pack_id}.\n")
        return
    sys.stdout.write("This computer is ready to drive gateways.\n")


@host_app.command("forget")
@graceful_errors
def host_forget(
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
    offline: bool = typer.Option(False, "--offline", help="Forget it here even when Cremind cannot be reached "
                                                          "(remove the computer on the Cremind page too)."),
) -> None:
    """Stop being a gateway computer: Cremind forgets this computer, and it forgets its credential."""
    import asyncio

    from app.tags.runtime.host.agent import HostError
    from app.tags.runtime.host.enroll import EnrollError, forget_enrollment, load_enrollment
    from app.tags.runtime.host.http_client import HttpHostClient

    paths = _paths()
    try:
        enrollment = load_enrollment(paths)
    except EnrollError as exc:
        enrollment = None
        sys.stderr.write(f"{exc}\n")
    if enrollment is None:
        forget_enrollment(paths)
        sys.stdout.write("This computer is not set up as a gateway computer.\n")
        return
    if not yes and not typer.confirm(f"Stop driving gateways for {enrollment.profile} on {enrollment.server}? Its "
                                     "gateways stay connected to that profile, offline until they move to another "
                                     "computer.", default=False):
        _fail("cancelled.")

    async def leave() -> None:
        client = HttpHostClient(enrollment.server, enrollment.authorization, ca_pem=enrollment.ca_pem)
        try:
            await client.leave()
        finally:
            await client.aclose()

    try:
        asyncio.run(leave())
    except HostError as exc:
        if exc.status != 401 and not offline:
            _fail(f"Cremind could not be told ({exc}). Try again, or forget it here only with --offline.")
    forget_enrollment(paths)
    sys.stdout.write("Forgotten: this computer is no longer a gateway computer.\n")


__all__ = ["host_app"]
