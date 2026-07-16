"""prompt_toolkit-based installer TUI.

Walks the same questions install.sh / install.ps1 used to ask via
numbered ``read -p`` prompts, plus a new channel + release picker so
users can pin a specific version without remembering the ``--version``
flag. Returns a populated :class:`TuiResult` (or ``None`` on cancel)
that ``__main__.py`` serialises for the shell to source.

Design notes:
  - Each screen is a small function returning ``(value, action)`` where
    ``action`` is one of ``advance`` / ``back`` / ``cancel``. The main
    :func:`run` loop walks a screens list with a cursor so ``back`` is
    a single decrement — no recursion, no nested dialogs.
  - Screens short-circuit (auto-``advance`` with the supplied value)
    when their slot in :class:`TuiResult` was pre-populated by a flag.
  - The version picker is the only screen that builds a full
    ``Application`` itself; everything else uses
    ``radiolist_dialog`` / ``input_dialog`` from ``prompt_toolkit.shortcuts``.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, replace
from typing import Callable, Literal

from prompt_toolkit.shortcuts import input_dialog, message_dialog, radiolist_dialog

from app.installer.catalog import Catalog, CustomField
from app.installer.output import TuiResult
from app.installer.releases import (
    RateLimitExceeded,
    ReleaseSummary,
    list_releases,
)


Action = Literal["advance", "back", "cancel"]
ScreenResult = tuple[TuiResult, Action]


# ── helpers ──────────────────────────────────────────────────────────────


def _radio(
    title: str,
    text: str,
    values: list[tuple[str, str]],
    *,
    default: str | None,
) -> tuple[str | None, Action]:
    """``radiolist_dialog`` wrapper that returns our (value, action) shape.

    The Cancel button (and Esc / Ctrl-C, which return ``None`` the same
    way) is treated as "abort the installer" — with a confirm step so a
    stray keystroke doesn't lose work. If the user declines to cancel,
    the dialog re-opens so they can keep going.
    """
    while True:
        result = radiolist_dialog(
            title=title,
            text=text,
            values=values,
            default=default if default and any(v[0] == default for v in values) else values[0][0],
            ok_text="Continue",
            cancel_text="Cancel install",
        ).run()
        if result is not None:
            return result, "advance"
        if _confirm_cancel():
            return None, "cancel"
        # User chose not to cancel — re-show this screen.


def _text(
    title: str,
    text: str,
    *,
    default: str = "",
    validator: Callable[[str], str | None] | None = None,
) -> tuple[str | None, Action]:
    """``input_dialog`` wrapper. Loops until validator accepts or user cancels."""
    while True:
        answer = input_dialog(
            title=title,
            text=text,
            default=default,
            ok_text="Continue",
            cancel_text="Cancel install",
        ).run()
        if answer is None:
            if _confirm_cancel():
                return None, "cancel"
            continue
        if validator is not None:
            err = validator(answer)
            if err is not None:
                message_dialog(title="Invalid input", text=err).run()
                default = answer
                continue
        return answer, "advance"


def _confirm_cancel() -> bool:
    """Show a quick confirm before fully aborting the installer.

    ``Yes`` aborts; ``No`` (including Esc / Ctrl-C on this dialog) keeps
    the installer running — picking the safer side when the user is
    already in a "I want to cancel" state but hits the wrong key.
    """
    answer = radiolist_dialog(
        title="Cancel install?",
        text="Are you sure? The installer will exit without making changes.",
        values=[("no", "Keep installing"), ("yes", "Yes, exit installer")],
        default="no",
        ok_text="OK",
        cancel_text="Keep installing",
    ).run()
    return answer == "yes"


# ── individual screens ──────────────────────────────────────────────────


def screen_channel(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.channel:
        return state, "advance"
    value, action = _radio(
        title="Cremind · Channel",
        text=(
            "Which release channel do you want to install from?\n\n"
            "  production — stable releases from PyPI (recommended)\n"
            "  test       — release-candidate prereleases from Test PyPI\n"
            "  dev        — install from this local checkout (developers)"
        ),
        values=[
            ("production", "production — stable"),
            ("test", "test — release candidates"),
            ("dev", "dev — local checkout"),
        ],
        default=state.channel or "production",
    )
    if action != "advance":
        return state, action
    return replace(state, channel=value or ""), "advance"


def screen_version_mode(state: TuiResult, ctx: "Context") -> ScreenResult:
    # Dev channel: no upstream releases to choose from.
    if state.channel == "dev":
        return state, "advance"
    # Production + electron-version pin: already locked, no choice to make.
    if state.channel == "production" and ctx.electron_version:
        return replace(state, version_spec=ctx.electron_version), "advance"
    # Explicit --version supplied: treat as already-specific, skip both screens.
    if state.version_spec:
        return state, "advance"

    value, action = _radio(
        title="Cremind · Version",
        text="Install the latest version on this channel, or pick a specific release?",
        values=[
            ("latest", "Latest — auto-resolve the newest release"),
            ("specific", "Pick a specific version from the release list"),
        ],
        default="latest",
    )
    if action != "advance":
        return state, action
    # Stash the choice on the context for the picker screen.
    ctx.version_mode = value or "latest"
    return state, "advance"


def _fmt_release_row(rel: ReleaseSummary) -> str:
    date = ""
    if rel.published_at:
        # GitHub uses ISO-8601 with a trailing Z; show the date portion only.
        date = rel.published_at[:10]
    suffix = "  [pre]" if rel.prerelease else ""
    return f"{rel.tag_name:<20} {rel.name[:40]:<40} {date}{suffix}"


def screen_version_picker(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.channel == "dev":
        return state, "advance"
    if state.version_spec:
        return state, "advance"
    if ctx.version_mode != "specific":
        return state, "advance"

    try:
        releases = list_releases(channel=state.channel, limit=30)  # type: ignore[arg-type]
    except RateLimitExceeded as exc:
        when = ""
        if exc.reset_at:
            when = _dt.datetime.fromtimestamp(exc.reset_at).strftime(" (resets at %H:%M)")
        message_dialog(
            title="GitHub rate limit",
            text=(
                f"GitHub's unauthenticated rate limit (60/hour/IP) is exhausted{when}.\n\n"
                "Re-run the installer with --version <spec> to skip the picker, or "
                "wait until the limit resets."
            ),
        ).run()
        return state, "back"
    except Exception as exc:  # noqa: BLE001 — surface any network error politely
        message_dialog(
            title="Couldn't fetch releases",
            text=f"Failed to query GitHub: {exc}\n\nReturning to the previous screen.",
        ).run()
        return state, "back"

    if not releases:
        message_dialog(
            title="No releases found",
            text=(
                f"GitHub returned no matching releases for the {state.channel} channel.\n\n"
                "Falling back to 'Latest'."
            ),
        ).run()
        return state, "advance"

    values = [(rel.version, _fmt_release_row(rel)) for rel in releases]
    chosen, action = _radio(
        title=f"Cremind · Pick a {state.channel} release",
        text=f"{len(releases)} releases available — newest first. Use ↑/↓ then Enter.",
        values=values,
        default=releases[0].version,
    )
    if action != "advance":
        return state, action
    return replace(state, version_spec=chosen or ""), "advance"


def screen_deployment(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.deployment:
        return state, "advance"
    default = "custom" if ctx.in_container else "local"
    values: list[tuple[str, str]] = []
    for dep in ctx.catalog.deployments:
        label = f"{dep.label} — {dep.description}"
        values.append((dep.id, label))
    text = "How will you run Cremind?"
    if ctx.in_container:
        text += (
            "\n\nDetected: running inside a container. 'local' would bind to "
            "127.0.0.1 inside the container, unreachable from the host browser; "
            "'custom' is pre-selected."
        )
    value, action = _radio(
        title="Cremind · Deployment",
        text=text,
        values=values,
        default=default,
    )
    if action != "advance":
        return state, action
    return replace(state, deployment=value or ""), "advance"


def screen_server_host(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.deployment != "server":
        return state, "advance"
    if state.app_host:
        return state, "advance"

    def _validate(value: str) -> str | None:
        if not value.strip():
            return "Required for server deployment."
        bad = [c for c in value if not (c.isalnum() or c in ".:-")]
        if bad:
            return "Use letters, digits, dot, colon, hyphen."
        return None

    value, action = _text(
        title="Cremind · Server host",
        text="Public IP or domain (e.g. 100.120.175.90 or cremind.example.com)",
        default=state.app_host,
        validator=_validate,
    )
    if action != "advance":
        return state, action
    return replace(state, app_host=(value or "").strip()), "advance"


def _custom_field_screen(field_def: CustomField, current: str) -> tuple[str | None, Action]:
    if field_def.choices:
        return _radio(
            title=f"Cremind · {field_def.key}",
            text=f"{field_def.prompt}\n\n{field_def.hint}",
            values=[(c, c) for c in field_def.choices],
            default=current or field_def.default,
        )

    def _validate(value: str) -> str | None:
        # Free-text fields can be empty (e.g. allowed_origins); the
        # shell falls back to a sensible default at line 655.
        return None

    return _text(
        title=f"Cremind · {field_def.key}",
        text=f"{field_def.prompt}\n\n{field_def.hint}",
        default=current or field_def.default,
        validator=_validate,
    )


def screen_custom_fields(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.deployment != "custom":
        return state, "advance"
    deployment = ctx.catalog.deployment("custom")
    if deployment is None:
        return state, "advance"

    new_state = state
    for field_def in deployment.advanced_fields:
        slot = f"custom_{field_def.key}"
        current = getattr(new_state, slot, "")
        if current:
            continue  # pre-populated by flag
        value, action = _custom_field_screen(field_def, current)
        if action != "advance":
            return new_state, action
        new_state = replace(new_state, **{slot: value or ""})
    return new_state, "advance"


def screen_mode(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.mode:
        return state, "advance"
    if not ctx.has_docker:
        # Docker not available — default to native without asking.
        return replace(state, mode="native"), "advance"

    values: list[tuple[str, str]] = []
    for mode in ctx.catalog.modes:
        badge = f" [{mode.badge}]" if mode.badge else ""
        values.append((mode.id, f"{mode.label}{badge} — {mode.description}"))
    value, action = _radio(
        title="Cremind · Mode",
        text="How do you want to run Cremind?",
        values=values,
        default=state.mode or "docker",
    )
    if action != "advance":
        return state, action
    return replace(state, mode=value or ""), "advance"


def screen_desktop(state: TuiResult, ctx: "Context") -> ScreenResult:
    # Only relevant for Docker installs — the desktop UI is a container image
    # flavor. Native installs share the host desktop and skip this.
    if state.mode != "docker":
        return state, "advance"
    if state.desktop:
        return state, "advance"

    dd = ctx.catalog.docker_desktop
    text = dd.prompt
    if dd.hint:
        text += f"\n\n{dd.hint}"
    value, action = _radio(
        title="Cremind · Desktop UI",
        text=text,
        values=[
            ("1", "Yes — include the VNC Desktop UI (recommended)"),
            ("0", "No — headless basic image (cremind/cremind)"),
        ],
        default="1" if dd.default else "0",
    )
    if action != "advance":
        return state, action
    return replace(state, desktop=value or ""), "advance"


def screen_confirm(state: TuiResult, ctx: "Context") -> ScreenResult:
    version_label = state.version_spec or "(latest on channel)"
    if state.channel == "dev":
        version_label = "(local checkout)"

    rows = [
        ("Channel", state.channel or "production"),
        ("Version", version_label),
        ("Deployment", state.deployment),
        ("Mode", state.mode),
    ]
    if state.mode == "docker":
        rows.append(("Desktop UI", "yes" if state.desktop != "0" else "no (basic image)"))
    if state.deployment == "server" and state.app_host:
        rows.append(("Host", state.app_host))
    if state.deployment == "custom":
        rows.append(("Listen host", state.custom_listen_host or "(catalog default)"))
        rows.append(("Public URL", state.custom_public_url or "(catalog default)"))
        rows.append(("Allowed origins", state.custom_allowed_origins or "(public URL + localhost)"))
        rows.append(("Wizard preset", state.custom_wizard_preset or "(catalog default)"))

    summary = "\n".join(f"  {k:<16} {v}" for k, v in rows)
    value, action = _radio(
        title="Cremind · Confirm",
        text=f"Review your selections:\n\n{summary}\n",
        values=[
            ("ok", "Install with these settings"),
            ("back", "Go back and change a value"),
        ],
        default="ok",
    )
    if action == "cancel":
        return state, "cancel"
    if value == "back":
        return state, "back"
    return state, "advance"


# ── runner ──────────────────────────────────────────────────────────────


@dataclass
class Context:
    """Per-run context the screens share (not written to the output file)."""

    catalog: Catalog
    in_container: bool
    has_docker: bool
    electron_version: str
    version_mode: str = "latest"


# Order matters: each screen advances or rewinds the cursor by 1.
_SCREENS: list[Callable[[TuiResult, Context], ScreenResult]] = [
    screen_channel,
    screen_version_mode,
    screen_version_picker,
    screen_deployment,
    screen_server_host,
    screen_custom_fields,
    screen_mode,
    screen_desktop,
    screen_confirm,
]


def run(
    *,
    catalog: Catalog,
    initial: TuiResult,
    in_container: bool,
    has_docker: bool,
    electron_version: str,
) -> TuiResult | None:
    """Drive the screen list; return the final TuiResult or ``None`` on cancel."""
    ctx = Context(
        catalog=catalog,
        in_container=in_container,
        has_docker=has_docker,
        electron_version=electron_version,
    )
    state = initial
    cursor = 0
    while 0 <= cursor < len(_SCREENS):
        screen = _SCREENS[cursor]
        new_state, action = screen(state, ctx)
        if action == "cancel":
            return None
        if action == "back":
            # Only the confirm screen returns "back". Wipe the values that
            # came from this run so the user is re-prompted; pre-populated
            # flag values stay as defaults via the initial state.
            state = TuiResult(
                channel=initial.channel,
                version_spec=initial.version_spec,
                deployment=initial.deployment,
                app_host=initial.app_host,
                mode=initial.mode,
                desktop=initial.desktop,
                custom_listen_host=initial.custom_listen_host,
                custom_public_url=initial.custom_public_url,
                custom_allowed_origins=initial.custom_allowed_origins,
                custom_wizard_preset=initial.custom_wizard_preset,
            )
            cursor = 0
            continue
        state = new_state
        cursor += 1

    return state


__all__ = ["Context", "run"]
