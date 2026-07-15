"""prompt_toolkit-based installer TUI.

Walks the same questions install.sh / install.ps1 used to ask via
numbered ``read -p`` prompts, plus a new channel + release picker so
users can pin a specific version without remembering the ``--version``
flag. Returns a populated :class:`TuiResult` (or ``None`` on cancel)
that ``__main__.py`` serialises for the shell to source.

Design notes:
  - Each screen is a small function returning ``(value, action)`` where
    ``action`` is one of ``advance`` / ``back`` / ``cancel`` / ``skip``.
    ``skip`` marks an *auto*-advance (the screen didn't prompt because a
    flag pre-populated it or it doesn't apply); ``advance`` marks a real
    user answer. The distinction is what lets :func:`run` implement a
    per-screen ``Back`` that steps over the auto-skipped screens.
  - The main :func:`run` loop walks a screens list with a cursor and a
    history stack of the screens that actually prompted, so ``back`` is a
    single pop — no recursion, no nested dialogs.
  - Every screen builds its own prompt_toolkit ``Application`` via the
    helpers below (``_radio`` / ``_text`` / ``_choice``) rather than the
    ``radiolist_dialog`` / ``input_dialog`` shortcuts. That is what buys
    us Enter-to-advance, a ``Back`` button, and uniform Esc / Ctrl-C
    handling (see :func:`_base_bindings`).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, replace
from typing import Callable, Literal

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList, TextArea

from app.installer.catalog import Catalog, CustomField
from app.installer.output import TuiResult
from app.installer.releases import (
    RateLimitExceeded,
    ReleaseSummary,
    list_releases,
)


Action = Literal["advance", "back", "cancel", "skip"]
ScreenResult = tuple[TuiResult, Action]


# ── dialog primitives ────────────────────────────────────────────────────
#
# We build a prompt_toolkit ``Application`` per screen instead of the
# ``radiolist_dialog`` / ``input_dialog`` shortcuts. That buys three things
# the closed shortcuts can't: Enter on a highlighted radio row advances
# immediately (no separate "Continue" button), a per-screen ``Back`` button,
# and uniform Esc / Ctrl-C handling. All keyboard behaviour lives in
# :func:`_base_bindings` so it stays identical on every screen.

# Sentinels returned by the key bindings and interpreted by :func:`_handle_common`.
_FORCE_QUIT = object()  # Ctrl-C: quit now, but let prompt_toolkit restore the terminal first.
_ESCAPE = object()      # Esc: run the cancel-confirm dialog.

_HINT = "↵ Enter to continue    ·    Esc to cancel"


def _base_bindings(
    *, radio: "RadioList | None" = None, escape_action: str = "confirm"
) -> KeyBindings:
    """Key bindings shared by every dialog.

    - ``Ctrl-C`` force-quits everywhere. It exits via ``app.exit`` (not
      ``sys.exit`` / ``os._exit`` inside the handler) so prompt_toolkit's
      ``.run()`` teardown still runs — leaving the alternate screen and
      restoring the terminal — before the process exits 1. Binding both
      ``ControlC`` (the key, delivered in raw mode / on Windows where
      ``handle_sigint`` is off) and ``SIGINT`` (the signal, on POSIX)
      covers every path.
    - ``Esc`` exits with the ``_ESCAPE`` sentinel so the caller can show
      the confirm dialog — except inside that confirm dialog
      (``escape_action="keep"``) where Esc means "keep installing".
    - When ``radio`` is given, Enter on the focused list selects the
      highlighted row and advances. It's ``eager`` so it wins over
      RadioList's own Enter (which would only tick the row without
      advancing); the ``has_focus`` filter keeps Enter on a *button*
      (e.g. Back) activating that button instead.
    """
    kb = KeyBindings()

    @kb.add(Keys.ControlC)
    @kb.add(Keys.SIGINT)
    def _force_quit(event) -> None:
        event.app.exit(result=_FORCE_QUIT)

    @kb.add("escape")
    def _escape(event) -> None:
        event.app.exit(result=False if escape_action == "keep" else _ESCAPE)

    if radio is not None:

        @kb.add("enter", filter=has_focus(radio), eager=True)
        def _advance(event) -> None:
            event.app.exit(result=(radio.current_value, "advance"))

    return kb


def _make_app(title, body, buttons, kb, *, focused=None) -> Application:
    """Wrap a dialog body + buttons in a full-screen Application.

    Mirrors ``prompt_toolkit.shortcuts.dialogs._create_app`` (full screen,
    mouse support) but with our own key bindings injected.
    """
    dialog = Dialog(title=title, body=body, buttons=buttons, with_background=True)
    return Application(
        layout=Layout(dialog, focused_element=focused),
        key_bindings=kb,
        mouse_support=True,
        full_screen=True,
    )


class _AdvanceRadioList(RadioList):
    """A RadioList whose mouse click picks a row *and* advances the dialog.

    With the Continue button gone, keyboard users advance via the eager
    Enter binding in :func:`_base_bindings`. This makes a mouse click on a
    row do the same, instead of merely ticking it, so mouse users aren't
    stranded without a button to press.
    """

    def _get_text_fragments(self):
        fragments = super()._get_text_fragments()
        out = []
        for frag in fragments:
            if len(frag) >= 3:
                out.append((frag[0], frag[1], self._advance_on_click(frag[2])))
            else:
                out.append(frag)
        return out

    def _advance_on_click(self, inner):
        def handler(mouse_event) -> None:
            inner(mouse_event)  # updates _selected_index + current_value
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                get_app().exit(result=(self.current_value, "advance"))

        return handler


def _back_button() -> Button:
    return Button(text="Back", handler=lambda: get_app().exit(result=(None, "back")))


def _handle_common(result):
    """Normalise a dialog's raw exit value into our ``(value, action)`` shape.

    Returns a ``(value, action)`` tuple, ``(None, "cancel")`` when the user
    confirmed exit through the Esc dialog, or ``None`` to mean "re-show this
    screen" (Esc declined). Raises ``KeyboardInterrupt`` on Ctrl-C, which
    ``__main__`` maps to exit code 1.
    """
    if result is _FORCE_QUIT:
        raise KeyboardInterrupt
    if result is _ESCAPE:
        if _confirm_cancel():
            return None, "cancel"
        return None  # keep installing → re-show
    return result


def _radio(
    title: str,
    text: str,
    values: list[tuple[str, str]],
    *,
    default: str | None,
    allow_back: bool,
) -> tuple[str | None, Action]:
    """Single-choice screen. Enter on the highlighted row advances.

    Returns ``(value, "advance")`` on a pick, ``(None, "back")`` when Back
    is used, or ``(None, "cancel")`` when the user confirms exiting.
    """
    if not values:
        raise ValueError("radio dialog requires at least one option")
    default_value = (
        default if default and any(v[0] == default for v in values) else values[0][0]
    )
    while True:
        radio = _AdvanceRadioList(
            values=values, default=default_value, select_on_focus=True
        )
        buttons = [_back_button()] if allow_back else []
        body = HSplit(
            [
                Label(text=text, dont_extend_height=True),
                radio,
                Label(text=_HINT, dont_extend_height=True),
            ],
            padding=1,
        )
        app = _make_app(title, body, buttons, _base_bindings(radio=radio), focused=radio)
        outcome = _handle_common(app.run())
        if outcome is not None:
            return outcome
        # Esc declined → re-show this screen.


def _text(
    title: str,
    text: str,
    *,
    default: str = "",
    validator: Callable[[str], str | None] | None = None,
    allow_back: bool = False,
) -> tuple[str | None, Action]:
    """Free-text screen. Enter submits directly; loops until valid or cancel."""
    while True:

        def _accept(buf) -> bool:
            get_app().exit(result=(buf.text, "advance"))
            return True  # keep the text in the buffer

        textfield = TextArea(text=default, multiline=False, accept_handler=_accept)
        buttons = [_back_button()] if allow_back else []
        body = HSplit(
            [
                Label(text=text, dont_extend_height=True),
                textfield,
                Label(text=_HINT, dont_extend_height=True),
            ],
            padding=1,
        )
        app = _make_app(title, body, buttons, _base_bindings(), focused=textfield)
        outcome = _handle_common(app.run())
        if outcome is None:
            continue  # Esc declined → re-show with the same default
        value, action = outcome
        if action != "advance":
            return outcome
        if validator is not None:
            err = validator(value or "")
            if err is not None:
                _message(title="Invalid input", text=err)
                default = value or ""
                continue
        return value, action


def _choice(
    title: str,
    text: str,
    options: list[tuple[str, Action]],
    *,
    focused_action: Action,
) -> tuple[None, Action]:
    """A message screen whose buttons carry ``action`` strings.

    Used by the confirm screen. Esc runs the cancel-confirm like every other
    screen; Ctrl-C force-quits.
    """
    while True:
        buttons = []
        focused = None
        for label, action in options:
            btn = Button(
                text=label, handler=lambda a=action: get_app().exit(result=(None, a))
            )
            buttons.append(btn)
            if action == focused_action:
                focused = btn
        body = Label(text=text, dont_extend_height=True)
        app = _make_app(title, body, buttons, _base_bindings(), focused=focused)
        outcome = _handle_common(app.run())
        if outcome is not None:
            return outcome


def _message(title: str, text: str) -> None:
    """Informational popup with a single OK button (Ctrl-C still force-quits)."""
    ok = Button(text="OK", handler=lambda: get_app().exit(result=None))
    body = Label(text=text, dont_extend_height=True)
    result = _make_app(title, body, [ok], _base_bindings(), focused=ok).run()
    if result is _FORCE_QUIT:
        raise KeyboardInterrupt
    # OK (None) and Esc (_ESCAPE) both just dismiss the popup.


def _confirm_cancel() -> bool:
    """Confirm before aborting the installer.

    ``Yes`` aborts; ``Keep installing`` (including Esc on this dialog) keeps
    the installer running — the safer side when the user is one keystroke
    away from losing their choices. Ctrl-C still force-quits.
    """
    keep = Button(text="Keep installing", handler=lambda: get_app().exit(result=False))
    quit_ = Button(
        text="Yes, exit installer", handler=lambda: get_app().exit(result=True)
    )
    body = Label(
        text="Are you sure? The installer will exit without making changes.",
        dont_extend_height=True,
    )
    kb = _base_bindings(escape_action="keep")
    result = _make_app("Cancel install?", body, [keep, quit_], kb, focused=keep).run()
    if result is _FORCE_QUIT:
        raise KeyboardInterrupt
    return result is True


# ── individual screens ──────────────────────────────────────────────────


def screen_channel(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.channel:
        return state, "skip"
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
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    return replace(state, channel=value or ""), "advance"


def screen_version_mode(state: TuiResult, ctx: "Context") -> ScreenResult:
    # Dev channel: no upstream releases to choose from.
    if state.channel == "dev":
        return state, "skip"
    # Production + electron-version pin: already locked, no choice to make.
    if state.channel == "production" and ctx.electron_version:
        return replace(state, version_spec=ctx.electron_version), "skip"
    # Explicit --version supplied: treat as already-specific, skip both screens.
    if state.version_spec:
        return state, "skip"

    value, action = _radio(
        title="Cremind · Version",
        text="Install the latest version on this channel, or pick a specific release?",
        values=[
            ("latest", "Latest — auto-resolve the newest release"),
            ("specific", "Pick a specific version from the release list"),
        ],
        default="latest",
        allow_back=ctx.can_go_back,
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
        return state, "skip"
    if state.version_spec:
        return state, "skip"
    if ctx.version_mode != "specific":
        return state, "skip"

    try:
        releases = list_releases(channel=state.channel, limit=30)  # type: ignore[arg-type]
    except RateLimitExceeded as exc:
        when = ""
        if exc.reset_at:
            when = _dt.datetime.fromtimestamp(exc.reset_at).strftime(" (resets at %H:%M)")
        _message(
            title="GitHub rate limit",
            text=(
                f"GitHub's unauthenticated rate limit (60/hour/IP) is exhausted{when}.\n\n"
                "Re-run the installer with --version <spec> to skip the picker, or "
                "wait until the limit resets."
            ),
        )
        return state, "back"
    except Exception as exc:  # noqa: BLE001 — surface any network error politely
        _message(
            title="Couldn't fetch releases",
            text=f"Failed to query GitHub: {exc}\n\nReturning to the previous screen.",
        )
        return state, "back"

    if not releases:
        _message(
            title="No releases found",
            text=(
                f"GitHub returned no matching releases for the {state.channel} channel.\n\n"
                "Falling back to 'Latest'."
            ),
        )
        return state, "skip"

    values = [(rel.version, _fmt_release_row(rel)) for rel in releases]
    chosen, action = _radio(
        title=f"Cremind · Pick a {state.channel} release",
        text=f"{len(releases)} releases available — newest first. Use ↑/↓ then Enter.",
        values=values,
        default=releases[0].version,
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    return replace(state, version_spec=chosen or ""), "advance"


def screen_deployment(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.deployment:
        return state, "skip"
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
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    return replace(state, deployment=value or ""), "advance"


def screen_server_host(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.deployment != "server":
        return state, "skip"
    if state.app_host:
        return state, "skip"

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
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    return replace(state, app_host=(value or "").strip()), "advance"


def _custom_field_screen(
    field_def: CustomField, current: str, *, allow_back: bool
) -> tuple[str | None, Action]:
    if field_def.choices:
        return _radio(
            title=f"Cremind · {field_def.key}",
            text=f"{field_def.prompt}\n\n{field_def.hint}",
            values=[(c, c) for c in field_def.choices],
            default=current or field_def.default,
            allow_back=allow_back,
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
        allow_back=allow_back,
    )


def screen_custom_fields(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.deployment != "custom":
        return state, "skip"
    deployment = ctx.catalog.deployment("custom")
    if deployment is None:
        return state, "skip"

    fields = deployment.advanced_fields
    new_state = state
    # Per-field history so Back walks fields one at a time; only Back on the
    # first *prompted* field bubbles out to the driver.
    field_stack: list[tuple[int, TuiResult]] = []
    prompted = False
    i = 0
    while i < len(fields):
        field_def = fields[i]
        slot = f"custom_{field_def.key}"
        if getattr(new_state, slot, ""):
            i += 1
            continue  # pre-populated by flag
        allow_back = bool(field_stack) or ctx.can_go_back
        before = new_state
        value, action = _custom_field_screen(field_def, "", allow_back=allow_back)
        if action == "cancel":
            return new_state, "cancel"
        if action == "back":
            if field_stack:
                i, new_state = field_stack.pop()
                continue
            return state, "back"  # first prompted field → bubble to the driver
        field_stack.append((i, before))
        new_state = replace(new_state, **{slot: value or ""})
        prompted = True
        i += 1
    return (new_state, "advance") if prompted else (new_state, "skip")


def screen_mode(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.mode:
        return state, "skip"
    if not ctx.has_docker:
        # Docker not available — default to native without asking.
        return replace(state, mode="native"), "skip"

    values: list[tuple[str, str]] = []
    for mode in ctx.catalog.modes:
        badge = f" [{mode.badge}]" if mode.badge else ""
        values.append((mode.id, f"{mode.label}{badge} — {mode.description}"))
    value, action = _radio(
        title="Cremind · Mode",
        text="How do you want to run Cremind?",
        values=values,
        default=state.mode or "docker",
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    return replace(state, mode=value or ""), "advance"


def screen_desktop(state: TuiResult, ctx: "Context") -> ScreenResult:
    # Only relevant for Docker installs — the desktop UI is a container image
    # flavor. Native installs share the host desktop and skip this.
    if state.mode != "docker":
        return state, "skip"
    if state.desktop:
        return state, "skip"

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
        allow_back=ctx.can_go_back,
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
    options: list[tuple[str, Action]] = [("Install with these settings", "advance")]
    if ctx.can_go_back:
        options.append(("Go back and change a value", "back"))
    _, action = _choice(
        title="Cremind · Confirm",
        text=f"Review your selections:\n\n{summary}\n",
        options=options,
        focused_action="advance",
    )
    return state, action


# ── runner ──────────────────────────────────────────────────────────────


@dataclass
class Context:
    """Per-run context the screens share (not written to the output file)."""

    catalog: Catalog
    in_container: bool
    has_docker: bool
    electron_version: str
    version_mode: str = "latest"
    # Set by the driver before each screen call: True when there is a previous
    # *prompted* screen to return to. Screens forward it as ``allow_back``.
    can_go_back: bool = False


# Order matters: each screen advances or rewinds the cursor.
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
    """Drive the screen list; return the final TuiResult or ``None`` on cancel.

    ``history`` records ``(cursor, state_before)`` for the screens that
    actually prompted the user. ``back`` pops the last of those and restores
    the state as it was *before* that screen ran, so re-running re-prompts
    (its slot is empty again) instead of the short-circuit guard bouncing it
    forward — and Back naturally steps over the auto-``skip``ped screens
    (flag-prepopulated or inapplicable), which are never pushed.
    """
    ctx = Context(
        catalog=catalog,
        in_container=in_container,
        has_docker=has_docker,
        electron_version=electron_version,
    )
    state = initial
    cursor = 0
    history: list[tuple[int, TuiResult]] = []
    while 0 <= cursor < len(_SCREENS):
        ctx.can_go_back = bool(history)
        state_before = state
        new_state, action = _SCREENS[cursor](state, ctx)
        if action == "cancel":
            return None
        if action == "back":
            if not history:
                # Unreachable in practice: Back is only offered (and the
                # picker's error-back only returned) when a prior screen
                # prompted, i.e. history is non-empty. Re-show as a safety net.
                continue
            cursor, state = history.pop()
            continue
        if action == "skip":
            state = new_state
            cursor += 1
            continue
        # action == "advance": a real prompt happened.
        history.append((cursor, state_before))
        state = new_state
        cursor += 1

    return state


__all__ = ["Context", "run"]
