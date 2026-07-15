"""Interactive profile picker for the CLI (prompt_toolkit).

A compact, filterable single-column list shown on first use in a terminal when
more than one profile is available (see ``app/cli/session.resolve_profile``).
Typing narrows the list (case-insensitive substring), Up/Down move the
highlight, Enter selects, Esc / Ctrl-C / Ctrl-D cancel.

Built on prompt_toolkit (a core dependency, already used by the chat TUI). Kept
deliberately small — an inline mini-menu rather than the multi-pane chat
Application — and rendered with ANSI escapes to match ``tui/renderer.py``.
prompt_toolkit is imported inside the function so ``cremind --help`` and
non-interactive paths never pay for it.
"""

from __future__ import annotations

from typing import Optional


def pick_profile(
    profiles: list[str],
    current: Optional[str] = None,
    *,
    input=None,
    output=None,
) -> Optional[str]:
    """Prompt the user to choose one of ``profiles``.

    Returns the chosen profile name, or ``None`` if cancelled or if ``profiles``
    is empty. ``input``/``output`` are prompt_toolkit I/O overrides used only by
    tests; production callers leave them ``None`` to bind to the real terminal.
    """
    if not profiles:
        return None

    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, VSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

    state: dict = {"selected": 0, "result": None}
    filter_buffer = Buffer(multiline=False)

    def _filtered() -> list[str]:
        query = filter_buffer.text.strip().lower()
        if not query:
            return profiles
        return [p for p in profiles if query in p.lower()]

    def _clamp() -> None:
        items = _filtered()
        if not items:
            state["selected"] = 0
        else:
            state["selected"] = max(0, min(state["selected"], len(items) - 1))

    def _on_text_changed(_buf: Buffer) -> None:
        # Reset the highlight to the top match whenever the filter changes.
        state["selected"] = 0

    filter_buffer.on_text_changed += _on_text_changed

    def _header() -> ANSI:
        return ANSI(
            "\x1b[1;38;5;252mSelect a Cremind profile\x1b[0m  "
            "\x1b[38;5;240m(type to filter · ↑↓ move · Enter select · Esc cancel)\x1b[0m"
        )

    def _list_fragments() -> ANSI:
        items = _filtered()
        _clamp()
        if not items:
            return ANSI("  \x1b[38;5;240m(no matching profile)\x1b[0m")
        rows: list[str] = []
        for i, name in enumerate(items):
            marker = "*" if name == current else " "
            if i == state["selected"]:
                rows.append(f"\x1b[1;38;5;39m❯ {marker} {name}\x1b[0m")
            else:
                rows.append(f"  \x1b[38;5;252m{marker} {name}\x1b[0m")
        return ANSI("\n".join(rows))

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _(event) -> None:
        state["selected"] -= 1
        _clamp()

    @kb.add("down")
    @kb.add("c-n")
    def _(event) -> None:
        state["selected"] += 1
        _clamp()

    @kb.add("enter")
    def _(event) -> None:
        items = _filtered()
        if items:
            _clamp()
            state["result"] = items[state["selected"]]
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    @kb.add("c-d")
    def _(event) -> None:
        state["result"] = None
        event.app.exit()

    filter_row = VSplit([
        Window(
            content=FormattedTextControl(lambda: ANSI("\x1b[38;5;240mfilter› \x1b[0m")),
            width=8,
            height=1,
        ),
        Window(content=BufferControl(buffer=filter_buffer), height=1),
    ])

    root = HSplit([
        Window(content=FormattedTextControl(_header), height=1),
        filter_row,
        Window(height=1, char=" "),
        Window(content=FormattedTextControl(_list_fragments)),
    ])

    app: Application = Application(
        layout=Layout(root, focused_element=filter_row.children[1]),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,
        input=input,
        output=output,
    )
    app.run()
    return state["result"]
