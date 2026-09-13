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
from pathlib import Path
from typing import Callable, Literal, NamedTuple

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
    password: bool = False,
) -> tuple[str | None, Action]:
    """Free-text screen. Enter submits directly; loops until valid or cancel.

    ``password`` masks the typed characters (prompt_toolkit renders ``*``);
    the value itself is returned in the clear.
    """
    while True:

        def _accept(buf) -> bool:
            get_app().exit(result=(buf.text, "advance"))
            return True  # keep the text in the buffer

        textfield = TextArea(
            text=default, multiline=False, accept_handler=_accept, password=password,
        )
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
                # Handing a rejected password back as a masked default would
                # leave the user editing a value they cannot read.
                default = "" if password else (value or "")
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
    # Kubernetes has no host to bind: the chart sets HOST/APP_URL on the pod
    # and the browser reaches it through a port-forward or an ingress.
    if state.mode == "kubernetes":
        return state, "skip"
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
    if state.mode == "kubernetes":
        return state, "skip"
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
    field_def: CustomField,
    current: str,
    *,
    allow_back: bool,
    validator: Callable[[str], str | None] | None = None,
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
        validator=validator or _validate,
        allow_back=allow_back,
    )


def _fields_loop(
    state: TuiResult,
    ctx: "Context",
    fields: tuple[CustomField, ...],
    *,
    slot_prefix: str,
    validator_for: Callable[[str, TuiResult], Callable[[str], str | None] | None]
    | None = None,
) -> ScreenResult:
    """Walk ``fields`` one screen at a time, writing ``<slot_prefix><key>``.

    Shared by the ``custom`` deployment's advanced fields and the kubernetes
    Helm options — same catalog shape, same per-field Back. A field whose slot
    is already populated (by a flag) is stepped over silently.
    """
    new_state = state
    # Per-field history so Back walks fields one at a time; only Back on the
    # first *prompted* field bubbles out to the driver.
    field_stack: list[tuple[int, TuiResult]] = []
    prompted = False
    i = 0
    while i < len(fields):
        field_def = fields[i]
        slot = f"{slot_prefix}{field_def.key}"
        if getattr(new_state, slot, ""):
            i += 1
            continue  # pre-populated by flag
        allow_back = bool(field_stack) or ctx.can_go_back
        before = new_state
        validator = validator_for(field_def.key, new_state) if validator_for else None
        value, action = _custom_field_screen(
            field_def, "", allow_back=allow_back, validator=validator
        )
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


def screen_custom_fields(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.mode == "kubernetes":
        return state, "skip"
    if state.deployment != "custom":
        return state, "skip"
    deployment = ctx.catalog.deployment("custom")
    if deployment is None:
        return state, "skip"
    return _fields_loop(
        state, ctx, deployment.advanced_fields, slot_prefix="custom_"
    )


# Modes that run Cremind in a container image, i.e. the ones the desktop-UI
# and VNC-password questions apply to. Docker runs that image locally;
# Kubernetes runs the same image in a pod.
_CONTAINER_MODES = ("docker", "kubernetes")

# What a missing capability is called when the mode screen explains why a
# mode is not on the list. Keys are the `requires` ids from catalog.toml.
CAPABILITY_LABELS = {
    "docker": "Docker",
    "kubectl": "kubectl with a kubeconfig context",
    "helm": "helm 3",
}


def screen_mode(state: TuiResult, ctx: "Context") -> ScreenResult:
    """Offer the install methods this machine can actually run.

    The catalog's ``requires`` is the gate (see ``Catalog.available_modes``):
    a mode is listed only when every capability it names was probed and found.
    When exactly one survives there is nothing to ask, so it is selected with
    a ``skip`` — which is also what happens on a plain laptop with no Docker,
    where ``native`` is the only mode left.
    """
    if state.mode:
        return state, "skip"

    available = ctx.catalog.available_modes(ctx.capabilities)
    if not available:
        # Defensive: `native` requires nothing, so this only happens with a
        # catalog that dropped it. Falling back keeps the installer usable.
        return replace(state, mode="native"), "skip"
    if len(available) == 1:
        return replace(state, mode=available[0].id), "skip"

    values: list[tuple[str, str]] = []
    for mode in available:
        badge = f" [{mode.badge}]" if mode.badge else ""
        values.append((mode.id, f"{mode.label}{badge} — {mode.description}"))

    text = "How do you want to run Cremind?"
    hidden = [m for m in ctx.catalog.modes if m not in available]
    if hidden:
        parts = []
        for mode in hidden:
            missing = [
                CAPABILITY_LABELS.get(r, r)
                for r in mode.requires
                if r not in ctx.capabilities
            ]
            parts.append(f"{mode.label} (needs {', '.join(missing)})")
        text += "\n\nNot offered here: " + "; ".join(parts)

    value, action = _radio(
        title="Cremind · Mode",
        text=text,
        values=values,
        # Catalog order is the recommendation: the first available mode wins.
        default=available[0].id,
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    return replace(state, mode=value or ""), "advance"


# ── kubernetes mode ──────────────────────────────────────────────────────
#
# The context list cannot be a catalog `choices` array: it is probed at run
# time. install.sh / install.ps1 enumerate the kubeconfigs before launching
# the TUI and hand the result over as a file, one context per line:
#
#     name<TAB>server<TAB>default-namespace<TAB>kubeconfig<TAB>current
#
# ``kubeconfig`` is empty for the config kubectl reads on its own
# ($KUBECONFIG, else ~/.kube/config) and the file's path for a sibling file
# the shell found under ~/.kube. Those files routinely reuse a context name
# ("default"), so a row is identified by (kubeconfig, name), never by the
# name alone. ``current`` is 1 on the ambient current-context. Only the name
# is required — a three-column line still parses.


class KubeContext(NamedTuple):
    name: str
    server: str = ""
    namespace: str = ""
    kubeconfig: str = ""
    current: bool = False

    @property
    def key(self) -> tuple[str, str]:
        """What identifies a target: sibling files may reuse a context name."""
        return (self.kubeconfig, self.name)


def parse_kube_contexts(text: str) -> tuple[KubeContext, ...]:
    """Parse the shell's contexts file. Blank and duplicate rows are dropped."""
    seen: set[tuple[str, str]] = set()
    out: list[KubeContext] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("\t")]
        parts += [""] * (5 - len(parts))
        name, server, namespace, kubeconfig, current = parts[:5]
        if not name or (kubeconfig, name) in seen:
            continue
        seen.add((kubeconfig, name))
        out.append(
            KubeContext(
                name=name,
                server=server,
                namespace=namespace,
                kubeconfig=kubeconfig,
                current=current == "1",
            )
        )
    return tuple(out)


def kubeconfig_label(path: str, home: str | None = None) -> str:
    """A kubeconfig path as the operator would type it: ``~`` for home.

    ``home`` exists for tests; the real one is :meth:`Path.home`.
    """
    if not path:
        return "default kubeconfig"
    root = home if home is not None else str(Path.home())
    if (
        root
        and len(path) > len(root)
        and path.startswith(root)
        and path[len(root)] in "/\\"
    ):
        return "~" + path[len(root):]
    return path


# An RFC 1123 label, which is what Kubernetes accepts for a namespace and
# Helm for a release name. install.sh and install.ps1 carry the same regex
# literally so a flag is rejected the same way an answer here is.
KUBE_NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
KUBE_NAME_ERROR = (
    "Use lowercase letters, digits and hyphens, starting and ending with a "
    "letter or digit."
)
#: Helm's own cap on a release name, stricter than the 63 of a DNS label.
HELM_RELEASE_NAME_MAX = 53


def validate_kube_namespace(value: str) -> str | None:
    """Return an error for a rejected namespace, or ``None`` if valid."""
    import re

    text = (value or "").strip()
    if not text:
        return "A namespace is required."
    if len(text) > 63:
        return "A namespace can be at most 63 characters."
    if not re.match(KUBE_NAME_PATTERN, text):
        return KUBE_NAME_ERROR
    return None


def validate_helm_release_name(value: str) -> str | None:
    """Return an error for a rejected Helm release name, or ``None``."""
    import re

    text = (value or "").strip()
    if not text:
        return "A release name is required."
    if len(text) > HELM_RELEASE_NAME_MAX:
        return f"Helm allows at most {HELM_RELEASE_NAME_MAX} characters."
    if not re.match(KUBE_NAME_PATTERN, text):
        return KUBE_NAME_ERROR
    return None


def screen_kube_context(state: TuiResult, ctx: "Context") -> ScreenResult:
    """Pick the cluster. The whole point of the mode's safety story.

    Whatever is chosen here is passed as ``--kube-context`` — and, for a
    context from a sibling kubeconfig file, ``--kubeconfig`` — on every helm
    and kubectl call the installer makes, so the ambient current-context
    never decides where a release lands. Each row carries the API server
    because two contexts can look alike by name and point at different
    clusters — or at the same one — and names its file when more than one
    file is in play, because sibling files reuse context names.
    """
    if state.mode != "kubernetes":
        return state, "skip"

    rows = list(ctx.kube_contexts)
    if state.kube_config_file:
        # --kubeconfig narrowed the shell's probe to that file; mirror it.
        narrowed = [k for k in rows if k.kubeconfig == state.kube_config_file]
        rows = narrowed or rows
    if state.kube_context:
        matches = [k for k in rows if k.name == state.kube_context]
        if state.kube_config_file or len(matches) <= 1:
            # Settled — or unknown, which the shell rejects with the full
            # list. Record the file the name resolved to and move on.
            if matches:
                state = replace(state, kube_config_file=matches[0].kubeconfig)
            return state, "skip"
        # The name exists in several files: ask which, and only that.
        rows = matches
    if not rows:
        _message(
            title="No kubeconfig contexts",
            text=(
                "kubectl reported no contexts, so there is no cluster to "
                "install into.\n\nCreate one (kubectl config set-context …) "
                "or re-run with --kube-context."
            ),
        )
        # Nothing to answer here: go back to the previous question if there
        # was one, otherwise there is no way forward at all.
        return state, ("back" if ctx.can_go_back else "cancel")

    kp = ctx.catalog.kubernetes
    if state.kube_context:
        text = (
            f"'{state.kube_context}' exists in {len(rows)} kubeconfig files. "
            "Which one did you mean?"
        )
    else:
        text = kp.context_prompt
    if kp.context_hint:
        text += f"\n\n{kp.context_hint}"

    # Values are positions in the full list: a name is not unique, and the
    # ambient rows share the empty kubeconfig.
    several_files = len({k.kubeconfig for k in ctx.kube_contexts}) > 1
    values: list[tuple[str, str]] = []
    default = ""
    for index, kube in enumerate(ctx.kube_contexts):
        if kube not in rows:
            continue
        server = kube.server or "(server unknown)"
        namespace = kube.namespace or "default"
        row = f"{kube.name} — {server} ({namespace})"
        if several_files:
            row += f"  ·  {kubeconfig_label(kube.kubeconfig)}"
        if kube.current:
            row += "  [current]"
            default = str(index)
        values.append((str(index), row))
    if not default:
        default = values[0][0]

    value, action = _radio(
        title="Cremind · Kubernetes context",
        text=text,
        values=values,
        default=default,
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    chosen = ctx.kube_contexts[int(value)] if value else rows[0]
    return (
        replace(state, kube_context=chosen.name, kube_config_file=chosen.kubeconfig),
        "advance",
    )


def screen_kube_namespace(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.mode != "kubernetes":
        return state, "skip"
    if state.kube_namespace:
        return state, "skip"

    kp = ctx.catalog.kubernetes
    text = kp.namespace_prompt
    if kp.namespace_hint:
        text += f"\n\n{kp.namespace_hint}"
    value, action = _text(
        title="Cremind · Namespace",
        text=text,
        default=kp.namespace_default,
        validator=validate_kube_namespace,
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    return replace(state, kube_namespace=(value or "").strip()), "advance"


def _k8s_validator(
    key: str, state: TuiResult
) -> Callable[[str], str | None] | None:
    """The per-field validator for the kubernetes advanced fields.

    ``app_url`` is checked against the HTTPS answer because the chart refuses
    to render ``cremind.ssl`` together with an ``http://`` app URL — and by
    the time helm says so the TUI is long gone.
    """
    if key == "release_name":
        return validate_helm_release_name

    if key == "app_url":

        def _validate_app_url(value: str) -> str | None:
            text = (value or "").strip()
            if not text:
                return None  # blank = let the chart derive it
            if not (text.startswith("http://") or text.startswith("https://")):
                return "Start the URL with http:// or https://, or leave it blank."
            if state.ssl_choice in ("auto", "after-setup") and text.startswith(
                "http://"
            ):
                return (
                    "HTTPS is enabled for this install, so the URL must be "
                    "https:// — or leave it blank and the chart derives it."
                )
            return None

        return _validate_app_url

    return None


def screen_k8s_advanced(state: TuiResult, ctx: "Context") -> ScreenResult:
    """Ask once whether the Helm options need customizing at all.

    Answering "recommended" fills every advanced slot with its catalog
    default, which makes the field loop below a no-op.

    A populated release name is the "already answered" signal, here and in
    both install scripts. It works because ``release_name`` is the one
    advanced field with a non-empty default — ``app_url`` and ``extra_set``
    are legitimately blank, so emptiness alone cannot mean "not asked yet".
    """
    if state.mode != "kubernetes":
        return state, "skip"
    fields = ctx.catalog.kubernetes.advanced_fields
    if not fields:
        return state, "skip"
    if state.k8s_release_name:
        # Answered already — by a flag, or by the shell replaying the
        # previous release. The field loop has nothing left to fill either.
        ctx.k8s_advanced = "recommended"
        return state, "skip"
    if any(getattr(state, f"k8s_{f.key}", "") for f in fields):
        # A flag answered part of this, so the operator is customizing: the
        # field loop asks the rest, release name included.
        ctx.k8s_advanced = "customize"
        return state, "skip"

    kp = ctx.catalog.kubernetes
    text = kp.advanced_prompt
    if kp.advanced_hint:
        text += f"\n\n{kp.advanced_hint}"
    value, action = _radio(
        title="Cremind · Helm options",
        text=text,
        values=[
            ("recommended", "Use the recommended options"),
            (
                "customize",
                "Customize — release name, app URL, Postgres image, data "
                "retention, extra --set",
            ),
        ],
        default="recommended",
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    ctx.k8s_advanced = value or "recommended"
    if ctx.k8s_advanced == "recommended":
        return (
            replace(state, **{f"k8s_{f.key}": f.default for f in fields}),
            "advance",
        )
    return state, "advance"


def screen_k8s_fields(state: TuiResult, ctx: "Context") -> ScreenResult:
    if state.mode != "kubernetes":
        return state, "skip"
    if ctx.k8s_advanced == "recommended":
        return state, "skip"
    fields = ctx.catalog.kubernetes.advanced_fields
    if not fields:
        return state, "skip"
    return _fields_loop(
        state, ctx, fields, slot_prefix="k8s_", validator_for=_k8s_validator
    )


def screen_desktop(state: TuiResult, ctx: "Context") -> ScreenResult:
    # Only relevant where Cremind runs from a container image — Docker locally,
    # Kubernetes in a pod. Native installs share the host desktop and skip this.
    if state.mode not in _CONTAINER_MODES:
        return state, "skip"
    if state.desktop:
        return state, "skip"
    if state.mode == "kubernetes" and state.channel == "production":
        # Not a choice on this channel: the production chart and the basic
        # image are published to the same Docker Hub tag, so a headless
        # Kubernetes install would pull the chart artifact as its image. The
        # install scripts refuse that combination outright (it can also arrive
        # by flag); here there is simply nothing to ask.
        return replace(state, desktop="1"), "skip"

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


def screen_ssl(state: TuiResult, ctx: "Context") -> ScreenResult:
    """Offer HTTPS on fresh installs; preserve flags and existing settings."""
    if state.ssl_choice:
        return state, "skip"
    # Kubernetes never "keeps": TLS is a chart value on the pod, not a host
    # .env, so neither an inherited CREMIND_SSL in this shell nor a previous
    # Docker/native install says anything about it. A re-install carries the
    # previous answer forward through --ssl, which short-circuits this screen
    # above — one owner of the release's state, the install script.
    if state.mode != "kubernetes":
        previous_env = ctx.docker_env if state.mode == "docker" else ctx.native_env
        if ctx.ssl_inherited or (previous_env and Path(previous_env).is_file()):
            return replace(state, ssl_choice="keep"), "skip"
    value, action = _radio(
        title="Cremind · HTTPS",
        text=(
            "Enable HTTPS (SSL)?\n\n"
            "HTTP is the default. You can enable HTTPS later in Settings > Security.\n"
            "HTTPS encrypts connections and enables HTTP/2. If enabled, setup\n"
            "first guides you through trusting the local certificate, then switches to HTTPS."
        ),
        values=[
            ("none", "HTTP — default"),
            ("after-setup", "Enable HTTPS (SSL) — trust the certificate during setup"),
        ],
        default="none",
        allow_back=ctx.can_go_back,
    )
    if action != "advance":
        return state, action
    return replace(state, ssl_choice=value or "none"), "advance"


# The one rule for a VNC password, shared by every front-end. install.sh and
# install.ps1 carry the same regex literally (a drift test pins the three
# copies together), and the Electron wizard validates against it too.
#
# 6-8 is VNC's own range: TigerVNC's vncpasswd refuses anything under 6, and
# the classic DES scheme keys off the first 8 characters only — a 20-character
# password would be silently truncated to 8, so we cap rather than mislead.
# The charset is what survives the trip to the container: install.sh renders
# the .env template with ``sed s|__VNC_PASSWORD__|...|g`` (so no ``|``, ``&``
# or ``\``), install.ps1 with ``-replace`` (so no ``$``), and the value then
# sits unquoted in a compose .env and inside a TOML basic string. Widening
# this set means fixing both renderers first.
VNC_PASSWORD_PATTERN = r"^[A-Za-z0-9@%_+=:,.-]{6,8}$"
VNC_PASSWORD_ERROR = (
    "Use 6 to 8 characters, from letters, digits and @ % _ + = : , . -\n"
    "(VNC itself ignores anything past the 8th character.)"
)


def validate_vnc_password(value: str, *, allow_blank: bool = False) -> str | None:
    """Return an error message for a rejected password, or ``None`` if valid.

    ``allow_blank`` is set on a re-install that already has a password: an
    empty entry there means "keep the current one" rather than "no password".
    """
    import re

    if not value:
        if allow_blank:
            return None
        return "A password is required for the VNC Desktop.\n\n" + VNC_PASSWORD_ERROR
    if not re.match(VNC_PASSWORD_PATTERN, value):
        return VNC_PASSWORD_ERROR
    return None


def screen_vnc_password(state: TuiResult, ctx: "Context") -> ScreenResult:
    """Ask for the desktop's VNC password, twice, and only when it applies.

    Sits immediately after :func:`screen_desktop` so ``state.desktop`` is
    already decided. Skipped whenever there is nothing to protect (native
    install, desktop declined) or the value arrived by flag.
    """
    if state.mode not in _CONTAINER_MODES:
        return state, "skip"
    if state.desktop == "0":
        return state, "skip"
    if state.vnc_password:
        return state, "skip"

    vp = ctx.catalog.vnc_password
    text = vp.prompt
    if vp.hint:
        text += f"\n\n{vp.hint}"

    while True:
        value, action = _text(
            title="Cremind · VNC password",
            text=text,
            validator=lambda v: validate_vnc_password(
                v, allow_blank=ctx.vnc_password_preset
            ),
            allow_back=ctx.can_go_back,
            password=True,
        )
        if action != "advance":
            return state, action
        entered = value or ""
        if not entered:
            # Blank + a previous install ⇒ keep what is already in docker/.env.
            # (The validator rejects blank when there is nothing to keep.)
            return replace(state, vnc_password=""), "advance"

        confirm, action = _text(
            title="Cremind · VNC password",
            text="Type the same password again to confirm.",
            allow_back=True,
            password=True,
        )
        if action == "cancel":
            return state, "cancel"
        if action == "back":
            continue  # back on the confirm re-asks for the password itself
        if (confirm or "") != entered:
            _message(
                title="Passwords do not match",
                text="The two entries were different. Please type it again.",
            )
            continue
        return replace(state, vnc_password=entered), "advance"


def screen_confirm(state: TuiResult, ctx: "Context") -> ScreenResult:
    version_label = state.version_spec or "(latest on channel)"
    if state.channel == "dev":
        version_label = "(local checkout)"

    rows = [
        ("Channel", state.channel or "production"),
        ("Version", version_label),
    ]
    if state.mode != "kubernetes":
        rows.append(("Deployment", state.deployment))
    rows.append(("Mode", state.mode))
    if state.mode == "kubernetes":
        picked = next(
            (
                k
                for k in ctx.kube_contexts
                if k.name == state.kube_context
                and k.kubeconfig == state.kube_config_file
            ),
            None,
        )
        server = picked.server if picked else next(
            (k.server for k in ctx.kube_contexts if k.name == state.kube_context),
            "",
        )
        label = f"{state.kube_context} — {server}" if server else state.kube_context
        if state.kube_config_file:
            label += f" ({kubeconfig_label(state.kube_config_file)})"
        rows.append(("Context", label))
        rows.append(("Namespace", state.kube_namespace))
        rows.append(("Release", state.k8s_release_name or "cremind"))
    rows.append(
        ("HTTPS (SSL)", {
            "after-setup": "enabled after certificate trust in setup",
            "auto": "enabled from first boot",
            "keep": "keep existing transport settings",
        }.get(state.ssl_choice, "off (HTTP)"))
    )
    if state.mode in _CONTAINER_MODES:
        if state.mode == "kubernetes" and state.channel == "production":
            desktop_label = "yes (required on the production channel)"
        else:
            desktop_label = "yes" if state.desktop != "0" else "no (basic image)"
        rows.append(("Desktop UI", desktop_label))
        if state.desktop != "0":
            rows.append((
                "VNC password",
                "********" if state.vnc_password else "(keep existing)",
            ))
    if state.mode == "kubernetes":
        rows.append(
            ("App URL", state.k8s_app_url or "(auto: http(s)://localhost:1515)")
        )
        rows.append((
            "Postgres image",
            "chart default"
            if state.k8s_legacy_postgres_image == "no"
            else "docker.io/bitnamilegacy/postgresql",
        ))
        rows.append((
            "Postgres data",
            "deleted on uninstall"
            if state.k8s_delete_postgres_data == "yes"
            else "kept on uninstall",
        ))
        rows.append(("Extra --set", state.k8s_extra_set or "(none)"))
    if state.mode != "kubernetes" and state.deployment == "server" and state.app_host:
        rows.append(("Host", state.app_host))
    if state.mode != "kubernetes" and state.deployment == "custom":
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
    has_kubectl: bool = False
    has_helm: bool = False
    # Probed by the shell before the TUI launches; empty when kubectl found
    # nothing or is not installed.
    kube_contexts: tuple[KubeContext, ...] = ()
    version_mode: str = "latest"
    # "" until screen_k8s_advanced runs; then "recommended" or "customize".
    k8s_advanced: str = ""
    # True when a previous install already has a VNC password on disk, which
    # makes an empty entry mean "keep that one" instead of being rejected.
    vnc_password_preset: bool = False
    ssl_inherited: bool = False
    native_env: str = ""
    docker_env: str = ""
    # Set by the driver before each screen call: True when there is a previous
    # *prompted* screen to return to. Screens forward it as ``allow_back``.
    can_go_back: bool = False

    @property
    def capabilities(self) -> frozenset[str]:
        """The ``requires`` ids this environment probed and found.

        A capability the front-end cannot probe is simply absent, which makes
        any mode requiring it unavailable — the same rule every front-end
        applies (see the comment under ``[modes]`` in install/catalog.toml).
        """
        found = set()
        if self.has_docker:
            found.add("docker")
        if self.has_kubectl:
            found.add("kubectl")
        if self.has_helm:
            found.add("helm")
        return frozenset(found)


# Order matters: each screen advances or rewinds the cursor. The mode comes
# before the deployment questions because it decides whether they are asked at
# all — a kubernetes install has no host to bind. The kubernetes Helm options
# come after HTTPS so the app-URL check knows which scheme was chosen.
_SCREENS: list[Callable[[TuiResult, Context], ScreenResult]] = [
    screen_channel,
    screen_version_mode,
    screen_version_picker,
    screen_mode,
    screen_kube_context,
    screen_kube_namespace,
    screen_deployment,
    screen_server_host,
    screen_custom_fields,
    screen_desktop,
    screen_vnc_password,
    screen_ssl,
    screen_k8s_advanced,
    screen_k8s_fields,
    screen_confirm,
]


def run(
    *,
    catalog: Catalog,
    initial: TuiResult,
    in_container: bool,
    has_docker: bool,
    electron_version: str,
    has_kubectl: bool = False,
    has_helm: bool = False,
    kube_contexts: tuple[KubeContext, ...] = (),
    vnc_password_preset: bool = False,
    ssl_inherited: bool = False,
    native_env: str = "",
    docker_env: str = "",
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
        has_kubectl=has_kubectl,
        has_helm=has_helm,
        kube_contexts=tuple(kube_contexts),
        vnc_password_preset=vnc_password_preset,
        ssl_inherited=ssl_inherited,
        native_env=native_env,
        docker_env=docker_env,
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


__all__ = ["Context", "KubeContext", "kubeconfig_label", "parse_kube_contexts", "run"]
