"""`cremind auth ...` — inspect, rotate, and revoke a profile's CLI/API token.

Rotation *is* revocation: the server keeps a per-profile ``token_serial``
mirrored into every JWT as the ``tsr`` claim, so minting a new token
invalidates every token issued to that profile before it.

Two execution paths:

* default — an authenticated ``POST /api/auth/regenerate``.
* ``--local`` — talks straight to the database and the token file on the server
  host, so a user whose token expired, leaked, or was deleted can recover with
  no working credential. Follows the ``app/cli/commands/serve.py`` precedent:
  the ``app.*`` server imports happen inside function bodies, never at module
  top level (see the import-discipline docstring in ``app/cli/main.py``).
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


auth_app = typer.Typer(
    name="auth",
    help="Inspect, rotate, and revoke this profile's CLI/API token.",
    no_args_is_help=True,
)


class _LocalError(Exception):
    """A ``--local`` failure already phrased for the user (never a traceback).

    ``graceful_errors`` only catches ``ConfigError`` / ``APIError`` /
    ``httpx.RequestError``, none of which the direct-DB path raises.
    """


_ROTATE_CAVEAT = (
    "Rotating profile '{profile}' invalidates EVERY token issued to it: other\n"
    "terminals, the web UI session, running agent shells, and any A2A/MCP\n"
    "client. New agent shells pick up the new token automatically."
)


# ── local (direct-DB) backend ──────────────────────────────────────────────


def _load_local_backend():
    """Import the server-side rotation helpers, bootstrapping storage first.

    Returns ``(rotate_profile_token, current_serial, list_profile_names)``.

    Import discipline: everything under ``app.`` is imported HERE, inside a
    function, never at module top level.
    """
    try:
        from app.config.bootstrap import bootstrap_exists
    except ImportError as e:
        raise _LocalError(
            "`--local` needs the Cremind server package, which isn't importable "
            "here. Run it on the machine where the Cremind server is installed."
        ) from e

    if not bootstrap_exists():
        raise _LocalError(
            "no database is configured on this host (bootstrap.toml is missing), "
            "so there is nothing to rotate against. Run `cremind setup` first."
        )

    try:
        from app.databases import create_database_provider, set_database_provider

        # Eager, though get_database_provider() would build lazily: it makes the
        # Kubernetes/unknown-provider guards fire here, with their real message,
        # instead of surfacing as a traceback from inside a query.
        set_database_provider(create_database_provider())

        from app.auth import (
            current_serial,
            ensure_local_config_storage,
            rotate_profile_token,
        )

        # MANDATORY: without it BaseConfig.get_jwt_secret() falls back to TOML,
        # returns "", and we would silently mint a token signed with an empty
        # key that no server will ever accept.
        ensure_local_config_storage()
    except ImportError as e:
        raise _LocalError(
            f"`--local` could not load the server storage layer ({e}). If this "
            "installation uses PostgreSQL, install the driver extra: "
            "`pip install 'cremind[postgres]'`."
        ) from e
    except ValueError as e:  # provider guards
        raise _LocalError(str(e)) from e

    def _list_profile_names() -> list[str]:
        from sqlalchemy import text

        from app.databases import get_database_provider

        engine = get_database_provider().sync_engine()
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT name FROM profiles"))]

    return rotate_profile_token, current_serial, _list_profile_names


def _guard_db(fn, *args, **kwargs):
    """Run a direct-DB call, mapping storage failures to phrased messages."""
    from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError

    try:
        return fn(*args, **kwargs)
    except (OperationalError, ProgrammingError) as e:
        text = str(e).lower()
        if "token_serial" in text and ("no such column" in text or "does not exist" in text):
            raise _LocalError(
                "this database predates the token-revocation column. Start the "
                "Cremind server once to migrate it, or run `cremind db upgrade` "
                "with the service stopped, then retry."
            ) from e
        if "database is locked" in text:
            raise _LocalError(
                "the database is locked — the Cremind server is mid-write. "
                "Retry in a moment."
            ) from e
        raise _LocalError(f"database error: {e}") from e
    except SQLAlchemyError as e:
        raise _LocalError(f"database error: {e}") from e
    except LookupError as e:
        raise _LocalError(
            f"no such profile in the database: {e} — "
            "run `cremind profile list` to see the valid names."
        ) from e
    except (ValueError, RuntimeError) as e:
        raise _LocalError(str(e)) from e


# ── shared helpers ─────────────────────────────────────────────────────────


def _resolve_target_profile(
    ctx: typer.Context,
    explicit: Optional[str],
    *,
    local: bool,
    list_db_profiles=None,
) -> str:
    """Which profile the command targets.

    Precedence: the subcommand's ``--profile`` → the root ``--profile`` /
    ``CREMIND_PROFILE`` → this terminal's remembered profile → in local mode
    only, the DB's profile list (sole profile auto-selected; several plus a TTY
    → the picker).

    The DB fallback is the whole point of local mode: ``session.list_profiles()``
    enumerates profiles that *have a token file*, and the recovery case is
    precisely the one where that file is gone.

    Returns ``""`` on the authenticated path when nothing is named — the server
    then derives the target from the caller's own JWT.
    """
    from app.cli import session

    if explicit:
        return explicit
    root_profile = (ctx.obj or {}).get("profile")
    if root_profile:
        return str(root_profile)
    remembered = session.get_session_profile()
    if remembered:
        return remembered
    if not local:
        return ""

    names = [n for n in (_guard_db(list_db_profiles) if list_db_profiles else []) if not n.startswith("__")]
    if len(names) == 1:
        return names[0]
    if not names:
        raise _LocalError("no profiles exist in the database. Run `cremind setup` first.")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise _LocalError(
            "several profiles exist and this is not an interactive terminal — "
            f"pass `--profile <name>` (one of: {', '.join(sorted(names))})."
        )

    from app.cli.tui.profile_picker import pick_profile

    try:
        chosen = pick_profile(sorted(names))
    except (KeyboardInterrupt, EOFError):
        raise typer.Exit(code=130)
    if not chosen:
        raise typer.Exit(code=130)
    return chosen


def _explain_api_error(e, target: str) -> None:
    """Surface the server's ``message`` field, plus the recovery hint on 401.

    ``_check_response`` keeps only ``error`` from the body, so the useful text
    has to come out of ``e.raw`` — same treatment as ``calendar google connect``.
    """
    import json as _json

    detail: Any = None
    if getattr(e, "raw", None):
        try:
            detail = _json.loads(e.raw)
        except (ValueError, TypeError):
            detail = None
    if isinstance(detail, dict) and detail.get("message"):
        sys.stderr.write(str(detail["message"]) + "\n")

    if getattr(e, "status", None) == 401:
        suffix = f" --profile {target}" if target else ""
        sys.stderr.write(
            "your token is expired or already revoked, so it can't authorize its own\n"
            f"rotation — run `cremind auth regenerate --local{suffix}` on the server host instead.\n"
        )


# ── commands ───────────────────────────────────────────────────────────────


@auth_app.command("status")
@graceful_errors
def auth_status(
    ctx: typer.Context,
    local: bool = typer.Option(
        False,
        "--local",
        help="Read the serial straight from the database instead of the server.",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Profile to report on (default: your own).",
    ),
) -> None:
    """Show whether this profile's token is still valid, and when it expires.

    Exits 1 when the token has been revoked — `cremind me` can't detect that,
    because it reads the token's own claims without comparing them to the
    server's current serial.
    """
    from app.cli.client._base import APIError, Client
    from app.cli.client.auth import AuthStatus, get_auth_status
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    try:
        if local:
            _rotate, current_serial, list_names = _load_local_backend()
            target = _resolve_target_profile(ctx, profile, local=True, list_db_profiles=list_names)
            serial = _guard_db(current_serial, target)
            status = _make_local_status(target, serial)
        else:
            import asyncio

            cfg.require_token()
            target = _resolve_target_profile(ctx, profile, local=False)

            async def _run() -> AuthStatus:
                async with Client(cfg) as client:
                    return await get_auth_status(client, profile=target or None)

            try:
                status = asyncio.run(_run())
            except APIError as e:
                _explain_api_error(e, target)
                raise
    except _LocalError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    if mode.json:
        print_json(status.to_dict())
    else:
        print_kv([
            ("profile", status.profile),
            ("valid", "yes" if status.valid else "no"),
            ("token_serial", str(status.token_serial)),
            ("current_serial", str(status.current_serial)),
            ("expires_at", _fmt_epoch(status.expires_at)),
            ("token_file", status.token_file),
        ])

    if not status.valid:
        sys.stderr.write(
            f"\ntoken for profile '{status.profile}' is revoked "
            f"(serial {status.token_serial}, current {status.current_serial}) — run "
            "`cremind auth regenerate` (add --local if you have no working token).\n"
        )
        raise typer.Exit(code=1)


def _make_local_status(profile: str, serial: int):
    """Build an :class:`AuthStatus` for the ``--local`` path.

    Local mode has no presented token to compare against, so it reports the
    stored serial and whatever the token file happens to say.
    """
    import jwt

    from app.cli import session
    from app.cli.client.auth import AuthStatus

    token = session.read_token(profile) or ""
    claims: dict[str, Any] = {}
    if token:
        try:
            # Signature verification needs the secret; we only want to *report*
            # what the on-disk token claims, and the serial comparison below is
            # what actually decides validity.
            claims = jwt.decode(token, options={"verify_signature": False})
        except Exception:  # noqa: BLE001
            claims = {}

    return AuthStatus(
        profile=profile,
        subject=str(claims.get("sub") or ""),
        issued_at=int(claims.get("iat") or 0),
        expires_at=int(claims.get("exp") or 0),
        token_serial=int(claims.get("tsr") or 0),
        current_serial=serial,
        valid=bool(token) and int(claims.get("tsr") or 0) == serial,
        token_file=str(session.tokens_dir() / f"{profile}.token") if token else "",
    )


def _fmt_epoch(value: int) -> str:
    if not value:
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


@auth_app.command("regenerate")
@graceful_errors
def auth_regenerate(
    ctx: typer.Context,
    local: bool = typer.Option(
        False,
        "--local",
        help="Rotate directly against the database and token file on this host "
        "(works with no valid token — use this to recover).",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Profile to rotate (default: your own). Rotating another profile "
        "requires admin on the server path.",
    ),
    expires_hours: Optional[int] = typer.Option(
        None,
        "--expires-hours",
        min=1,
        max=8760,
        help="Token lifetime in hours (default: the server's setting).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    show_token: bool = typer.Option(
        False,
        "--show-token",
        help="Print the JWT itself (default: only the token-file path).",
    ),
    no_write_file: bool = typer.Option(
        False,
        "--no-write-file",
        help="Do not update this host's tokens/<profile>.token.",
    ),
) -> None:
    """Mint a new token and revoke every token issued to the profile before it.

    Use this the moment a token is exposed — overwriting the token file alone
    does not invalidate the leaked copy, but this does.
    """
    import os

    from app.cli import session
    from app.cli.client._base import APIError, Client
    from app.cli.client.auth import RotatedToken, regenerate_token
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    try:
        if local:
            rotate, _current, list_names = _load_local_backend()
            target = _resolve_target_profile(ctx, profile, local=True, list_db_profiles=list_names)
            _confirm(target, yes)
            result = _guard_db(rotate, target, hours=expires_hours)
            rotated = RotatedToken.from_dict(result)
        else:
            import asyncio

            cfg.require_token()
            target = _resolve_target_profile(ctx, profile, local=False)
            _confirm(target or "the current profile", yes)

            async def _run() -> RotatedToken:
                async with Client(cfg) as client:
                    return await regenerate_token(
                        client, profile=target or None, expires_hours=expires_hours
                    )

            try:
                rotated = asyncio.run(_run())
            except APIError as e:
                _explain_api_error(e, target)
                raise
    except _LocalError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    # Keep THIS host's credential store in step with what was just minted. On
    # the REST path the server wrote its own copy; if the CLI is pointed at a
    # remote server, or the service runs under a different HOME, that copy isn't
    # the one this CLI reads — so write ours too and show both when they differ.
    local_path = ""
    if not no_write_file and rotated.token:
        try:
            local_path = str(session.write_token(rotated.profile, rotated.token))
        except (OSError, ValueError) as e:
            typer.echo(f"warning: could not update the local token file: {e}", err=True)
            if not show_token:
                sys.stderr.write(
                    "The new token was printed nowhere — re-run with --show-token "
                    "to capture it.\n"
                )
    if local_path:
        session.set_session_profile(rotated.profile)

    if mode.json:
        payload = rotated.to_dict(include_token=show_token)
        payload["local_token_file"] = local_path
        print_json(payload)
    else:
        rows = [
            ("profile", rotated.profile),
            ("serial", str(rotated.serial)),
            ("expires_at", rotated.expires_at),
        ]
        if rotated.token_file:
            rows.append(("token_file", rotated.token_file))
        if local_path and local_path != rotated.token_file:
            rows.append(("local_token_file", local_path))
        if show_token:
            rows.append(("token", rotated.token))
        print_kv(rows)

    sys.stderr.write("\nEvery token issued to this profile before now is revoked.\n")
    if show_token:
        sys.stderr.write(f"  export CREMIND_TOKEN={rotated.token}\n")
    elif os.environ.get("CREMIND_TOKEN"):
        # exec_shell injects CREMIND_TOKEN at spawn time and the env var wins
        # over the token file, so the rest of THIS shell now holds a dead token
        # even though the file on disk is fine.
        sys.stderr.write(
            "CREMIND_TOKEN is set in this environment and now holds a REVOKED token.\n"
            "Re-export it (`export CREMIND_TOKEN=$(cremind auth show)`) or start a new shell.\n"
        )


def _confirm(profile: str, yes: bool) -> None:
    """Prompt before rotating. Caveat to stderr so stdout stays clean."""
    if yes:
        return
    sys.stderr.write(_ROTATE_CAVEAT.format(profile=profile) + "\n")
    if not typer.confirm(f"Rotate the token for profile '{profile}' now?", default=False):
        raise typer.Exit(code=1)


@auth_app.command("show")
def auth_show(
    ctx: typer.Context,
    profile: Optional[str] = typer.Option(
        None, "--profile", help="Profile whose token file to read (default: your own).",
    ),
    path: bool = typer.Option(
        False, "--path", help="Print the token file's path instead of its contents.",
    ),
) -> None:
    """Print this host's stored token for a profile.

    Inherently local — never contacts the server and never needs a token, so it
    works after a rotation has locked the current shell out. Handy as
    ``export CREMIND_TOKEN=$(cremind auth show)``.
    """
    from app.cli import session

    target = profile or (ctx.obj or {}).get("profile") or session.get_session_profile()
    if not target:
        names = session.list_profiles()
        if len(names) == 1:
            target = names[0]
    if not target:
        typer.echo(
            "no profile selected — pass `--profile <name>` "
            f"(token files live under {session.tokens_dir()}).",
            err=True,
        )
        raise typer.Exit(code=1)

    token_path = session.tokens_dir() / f"{target}.token"
    if path:
        typer.echo(str(token_path))
        return

    token = session.read_token(str(target))
    if not token:
        typer.echo(
            f"no token file for profile '{target}' under {session.tokens_dir()} — "
            f"run `cremind auth regenerate --local --profile {target}`.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(token)
