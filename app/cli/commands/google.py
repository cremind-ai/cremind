"""`cremind google ...` — inspect and unlink the Google accounts behind the skills.

*Linking* a Google account happens in chat, through the skill that needs it
(``gmail``, ``gcalendar``, ``gdrive``, ``gsheets``, ``gdocs``) — each owns its own
OAuth token. These commands are the other half: show what is linked, and take a
link apart again, revoking Cremind's access at Google. The terminal counterpart of
the **Settings -> GSuite** page.

Distinct from ``cremind calendar google disconnect``, which clears the credential
connected on the Calendar & Schedule *page* rather than the gcalendar skill's.

Two things shape the output. Google treats Cremind as **one app**, so revoking one
skill's grant can end the others' for the same address — hence the shared-grant
warning, and hence a plain per-skill ``unlink`` declining to revoke while a sibling
still needs the grant. And the consequence sentences come from the server, so this,
the settings page and the API cannot describe the same account differently.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors

google_app = typer.Typer(
    name="google",
    help="Inspect and unlink the Google accounts behind the Google Suite skills.",
    no_args_is_help=True,
)

_CONNECTIONS_URL = "https://myaccount.google.com/connections"


def _detail(exc: Any) -> dict[str, Any]:
    """The server's full error body.

    ``_check_response`` keeps only the machine ``error`` code, so every human
    sentence has to be dug out of ``raw`` here — same treatment as
    ``cremind calendar google connect``.
    """
    import json

    try:
        payload = json.loads(getattr(exc, "raw", b"") or b"{}")
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _shared_grant_note(payload: dict[str, Any]) -> Optional[str]:
    groups = [
        group
        for group in payload.get("accounts") or []
        if group.get("shared_grant") and group.get("skills")
    ]
    if not groups:
        return None
    parts = [
        f"{', '.join(group['skills'])} share the grant for {group.get('email') or 'one account'}"
        for group in groups
    ]
    return (
        "Note: Google lists Cremind as one app, so " + "; ".join(parts) + ". "
        "Revoking any one of them may end the others."
    )


@google_app.command("status")
@graceful_errors
def google_status(ctx: typer.Context) -> None:
    """Show which Google account each Google Suite skill is linked to."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.google import get_accounts
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_accounts(client)

    payload = asyncio.run(_run())
    if mode.json:
        print_json(payload)
        return

    rows = payload.get("skills") or []
    if not any(row.get("installed") for row in rows):
        sys.stdout.write("No Google Suite skills are installed for this profile.\n")
        return

    table = Table(mode, "SKILL", "LINKED AS", "ENABLED", "LISTENER", "WATCH", "SUBS")
    for row in rows:
        if not row.get("installed"):
            continue
        listener = row.get("listener") or {}
        watch = row.get("watch") or {}
        table.add_row(
            row.get("skill", ""),
            row.get("email") or "—",
            "yes" if row.get("enabled") else "no",
            ("registered" if listener.get("autostart_rows") else "not started")
            if listener.get("declared")
            else "—",
            "active" if watch.get("active") else "—",
            (row.get("subscriptions") or {}).get("idle_after_unlink") or 0,
        )
    table.render()

    note = _shared_grant_note(payload)
    if note:
        sys.stderr.write(f"\n{note}\n")


def _caveat(row: dict[str, Any], *, revoke: bool, profile: str) -> str:
    """The blast radius, phrased from the server's own inventory."""
    lines = [
        f"Unlinking {row.get('label')} ({row.get('skill')}) for profile {profile!r}, "
        f"linked as {row.get('email') or 'an unknown account'}:",
        "  - deletes this machine's copy of the Google credentials",
    ]
    lines.append(
        "  - revokes Cremind's access at Google"
        if revoke
        else "  - leaves the grant live at Google (--no-revoke)"
    )
    if row.get("consequence"):
        lines.append(f"  - {row['consequence']}")
    listener = row.get("listener") or {}
    if listener.get("declared"):
        lines.append(
            f"  - the {row.get('skill')} listener stops and its autostart registration is "
            "removed — register it again after re-linking (Settings -> Tools & Skills)"
        )
    shared = row.get("siblings_sharing_grant") or []
    if shared and revoke:
        lines.append(
            "  - the same account is still linked in "
            f"{', '.join(shared)}, so Google will NOT be told unless you also unlink "
            "those (or use --all)"
        )
    idle = (row.get("subscriptions") or {}).get("idle_after_unlink") or 0
    if idle:
        lines.append(
            f"  - {idle} event automation(s) on this skill stop firing until you re-link "
            "and register its listener again"
        )
    lines.append("Re-linking needs another Google consent, which runs from chat.")
    return "\n".join(lines)


def _report(result: dict[str, Any]) -> None:
    """Print one skill's outcome: facts to stdout, warnings to stderr."""
    if not result.get("unlinked"):
        sys.stdout.write(f"{result.get('skill')}: not linked — nothing to unlink\n")
        return

    sys.stdout.write(f"unlinked {result.get('skill')} ({result.get('email') or 'unknown'})\n")
    sys.stdout.write(f"revoked at Google: {'yes' if result.get('revoked') else 'no'}\n")
    if result.get("autostart_removed"):
        sys.stdout.write("listener stopped; autostart registration removed\n")
    if result.get("cleaned"):
        sys.stdout.write(f"removed: {', '.join(result['cleaned'])}\n")

    if not result.get("revoked") and result.get("revoke_error"):
        # Loud, and deliberately never "try again": the token that could have
        # revoked it is gone for good.
        sys.stderr.write(
            f"\nLocal Google credentials for {result.get('skill')} removed. Google was "
            f"NOT told: {result['revoke_error']}. Cremind can no longer use the account, "
            "but the grant is still listed on your Google account — and the token needed "
            "to revoke it is gone, so re-running this will not help. Remove Cremind at "
            f"{_CONNECTIONS_URL}\n"
        )
    if result.get("watch_error"):
        sys.stderr.write(f"\n{result['watch_error']}\n")
    if result.get("app_credential_at_risk"):
        sys.stderr.write(
            "\nThe Calendar & Schedule page's own Google credential uses the same OAuth "
            "client, so this revoke may have ended it too. Reconnect it on that page if "
            "the calendar stops working.\n"
        )
    # A *suppressed* revoke (a sibling still shares the grant) is the one case where
    # the server's own sentence is the clearest thing to print — it names the
    # siblings and the way out. A *failed* revoke already got its block above, so
    # printing the message too would just say it twice.
    if result.get("revoke_status") == "skipped_shared_grant":
        sys.stderr.write(f"\n{result.get('message')}\n")

    if result.get("skill") == "gcalendar":
        after = result.get("calendar_source_after")
        sys.stdout.write(
            "the Calendar & Schedule page now uses the account connected on that page\n"
            if after == "app"
            else "the Calendar & Schedule page now uses the built-in system calendar\n"
        )


def _report_all(out: dict[str, Any], mode: Any) -> None:
    from app.cli.output import Table

    table = Table(mode, "SKILL", "RESULT", "REVOKED", "NOTE")
    for row in out.get("results") or []:
        if row.get("still_linked"):
            outcome = "failed"
        elif row.get("unlinked"):
            outcome = "unlinked"
        elif row.get("already"):
            outcome = "not linked"
        else:
            outcome = "unchanged"
        if row.get("revoked"):
            revoked = "already" if row.get("revoke_status") == "already_revoked" else "yes"
        else:
            revoked = "—"
        note = ""
        if row.get("still_linked"):
            note = ", ".join(row.get("failed_paths") or []) or "credential file survived"
        elif row.get("revoke_error"):
            note = str(row["revoke_error"])
        elif row.get("autostart_removed"):
            note = "listener stopped and deregistered"
        table.add_row(row.get("skill", ""), outcome, revoked, note)
    table.render()
    if out.get("message"):
        sys.stdout.write(f"\n{out['message']}\n")


@google_app.command("unlink")
@graceful_errors
def google_unlink(
    ctx: typer.Context,
    skill: Optional[str] = typer.Argument(
        None,
        help="Google skill to unlink: gcalendar, gdocs, gdrive, gmail, gsheets.",
    ),
    unlink_all_skills: bool = typer.Option(
        False, "--all", help="unlink every Google Suite skill for this profile"
    ),
    no_revoke: bool = typer.Option(
        False,
        "--no-revoke",
        help="wipe local credentials only; leave the grant live at Google",
    ),
    force_revoke: bool = typer.Option(
        False,
        "--force-revoke",
        help="revoke even when another skill still shares the grant (breaks it too)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Revoke Cremind's Google access and delete the stored credentials."""
    import asyncio

    from app.cli.client._base import APIError, Client
    from app.cli.client.google import get_accounts, unlink_all, unlink_skill
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    if unlink_all_skills and skill:
        typer.echo("pass either a skill name or --all, not both", err=True)
        raise typer.Exit(code=1)
    if not unlink_all_skills and not skill:
        typer.echo(
            "name a Google skill to unlink (gcalendar, gdocs, gdrive, gmail, gsheets) "
            "or pass --all",
            err=True,
        )
        raise typer.Exit(code=1)

    revoke = not no_revoke
    profile = ctx.obj.get("profile") or "this profile"

    async def _inventory() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_accounts(client)

    # The caveat is built from the server's inventory, so the CLI cannot phrase a
    # consequence differently from the settings page. Skipped under --yes, which
    # also keeps the non-interactive agent path down to a single request.
    if not yes:
        payload = asyncio.run(_inventory())
        rows = payload.get("skills") or []
        if unlink_all_skills:
            linked = [row for row in rows if row.get("linked")]
            if not linked:
                sys.stdout.write("No Google accounts are linked for this profile.\n")
                return
            who = "; ".join(
                f"{group.get('email')} ({', '.join(group.get('skills') or [])})"
                for group in payload.get("accounts") or []
            )
            sys.stderr.write(
                f"Unlinking every Google link for profile {profile!r}: {who}.\n"
                "Cremind loses every Google capability. Drive file grants are lost "
                "permanently, any listeners stop and are deregistered, and the Calendar "
                "page falls back to its own credential or the built-in calendar.\n"
            )
            if not typer.confirm("Unlink all Google accounts now?", default=False):
                raise typer.Exit(code=1)
        else:
            row = next((r for r in rows if r.get("skill") == skill), None)
            if row is None:
                typer.echo(
                    f"{skill!r} is not a Google Suite skill. Choose one of: "
                    "gcalendar, gdocs, gdrive, gmail, gsheets.",
                    err=True,
                )
                raise typer.Exit(code=1)
            if not row.get("linked"):
                sys.stdout.write(f"{skill}: not linked — nothing to unlink\n")
                return
            sys.stderr.write(_caveat(row, revoke=revoke, profile=str(profile)) + "\n")
            if not typer.confirm(f"Unlink {skill} now?", default=False):
                raise typer.Exit(code=1)

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            if unlink_all_skills:
                return await unlink_all(client, revoke=revoke)
            return await unlink_skill(
                client, str(skill), revoke=revoke, force_revoke=force_revoke
            )

    try:
        out = asyncio.run(_run())
    except APIError as exc:
        detail = _detail(exc)
        if not detail.get("message") and not detail.get("results"):
            raise
        # A credential file survived the wipe (HTTP 500 wipe_failed) — the one
        # outcome that is a genuine failure, because a usable token is still on
        # disk. Render it fully, then exit non-zero.
        if mode.json:
            print_json(detail)
        elif detail.get("results"):
            _report_all(detail, mode)
        if detail.get("message"):
            sys.stderr.write(f"{detail['message']}\n")
        raise typer.Exit(code=1) from exc

    if mode.json:
        print_json(out)
        return
    if unlink_all_skills:
        _report_all(out, mode)
        note = None
        for row in out.get("results") or []:
            if row.get("app_credential_at_risk"):
                note = (
                    "The Calendar & Schedule page's own Google credential shares the "
                    "OAuth client and may have been revoked too."
                )
        if note:
            sys.stderr.write(f"\n{note}\n")
        return
    _report(out)
