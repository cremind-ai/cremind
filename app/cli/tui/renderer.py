"""Render SSE events as ANSI-styled lines for the chat TUI.

Port of `cli/internal/tui/events.go` and `styles.go`. Produces ANSI escape
sequences that prompt_toolkit consumes via `FormattedText.from_ansi`.

The styles are inlined as `\\x1b[...]m` codes rather than going through
prompt_toolkit's `Style` class, so the same renderer can also be used for
non-TUI rendering paths if needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from app.cli.client._sse import Event


# ── ANSI palette (256-color, matching the Go lipgloss palette) ───────────

_RESET = "\x1b[0m"


@dataclass(frozen=True)
class Theme:
    user_msg: str
    assist_msg: str
    thinking: str
    phase: str
    err: str
    dim: str
    section: str

    def style(self, code: str, text: str) -> str:
        if not code:
            return text
        return f"{code}{text}{_RESET}"


def default_theme() -> Theme:
    """The colored theme — matches `tui.DefaultTheme` in styles.go."""
    return Theme(
        user_msg="\x1b[1;38;5;39m",     # bold blue
        assist_msg="\x1b[38;5;252m",    # near-white
        thinking="\x1b[3;38;5;244m",    # italic gray
        phase="\x1b[3;38;5;214m",       # italic orange
        err="\x1b[1;38;5;196m",         # bold red
        dim="\x1b[38;5;240m",           # dim gray
        section="\x1b[1;38;5;252m",     # bold near-white
    )


def monochrome_theme() -> Theme:
    """Monochrome theme — emphasis via bold/italic only."""
    return Theme(
        user_msg="\x1b[1m",
        assist_msg="",
        thinking="\x1b[3m",
        phase="\x1b[3m",
        err="\x1b[1m",
        dim="",
        section="\x1b[1m",
    )


# ── rendered event records ───────────────────────────────────────────────


@dataclass(frozen=True)
class RenderedLine:
    """A single styled chunk ready for the transcript buffer.

    `kind` matches `events.go` — used for coalescing consecutive `text`
    chunks into a single growing message.
    """

    kind: str  # "user" | "thinking" | "text" | "phase" | "summary" |
               # "terminal" | "file" | "error" | "info"
    body: str  # ANSI-styled string


def format_event(event: Event, theme: Theme) -> Optional[RenderedLine]:
    """Translate one SSE event into a renderable line, or None to skip.

    Mirrors `formatEvent` in events.go.
    """
    data: dict[str, Any] = {}
    if isinstance(event.data, dict):
        data = event.data.get("data") if isinstance(event.data.get("data"), dict) else {}

    t = theme

    if event.type == "ready":
        return RenderedLine("info", t.style(t.dim, "* connected"))

    if event.type == "user_message":
        content = str(data.get("content") or "")
        return RenderedLine(
            "user",
            t.style(t.user_msg, "you: ") + content,
        )

    if event.type == "thinking":
        thought = str(data.get("Thought") or "")
        action = str(data.get("Action") or "")
        action_input = data.get("Action_Input")
        body_parts: list[str] = []
        if thought:
            body_parts.append(t.style(t.thinking, "<> Thought: " + thought))
        if action:
            body_parts.append(t.style(t.dim, "  -> Action: " + action))
        ai = _format_action_input(action_input)
        if ai:
            body_parts.append(t.style(t.dim, "    Input: " + ai))
        if not body_parts:
            return None
        return RenderedLine("thinking", "\n".join(body_parts))

    if event.type == "text":
        token = str(data.get("token") or data.get("text") or "")
        if not token:
            return None
        return RenderedLine("text", t.style(t.assist_msg, token))

    if event.type == "phase":
        label = str(data.get("label") or data.get("phase") or "")
        if not label:
            return None
        return RenderedLine("phase", t.style(t.phase, ">> " + label))

    if event.type == "summary":
        summary = str(data.get("summary") or "")
        if not summary:
            return None
        return RenderedLine("summary", t.style(t.dim, "S " + summary))

    if event.type == "terminal":
        command = str(data.get("command") or "")
        output = str(data.get("output") or "")
        body = f"$ {command}\n{output}"
        return RenderedLine("terminal", body)

    if event.type == "file":
        action = str(data.get("action") or "")
        path = str(data.get("path") or "")
        return RenderedLine("file", f"{action} {path}")

    if event.type == "result":
        observation = str(data.get("Observation") or "")
        if not observation:
            return None
        indented = observation.replace("\n", "\n    ")
        return RenderedLine(
            "thinking",
            t.style(t.dim, "  <- Observation: " + indented),
        )

    if event.type == "complete":
        return RenderedLine("info", t.style(t.dim, "* run complete"))

    if event.type == "error":
        msg = str(data.get("message") or data.get("error") or "error")
        if data.get("setup_required"):
            label = str(data.get("settings_label") or "the Settings page")
            hint = "  -> Fix it from " + label
            sp = data.get("settings_path")
            if sp:
                hint += f" ({sp})"
            return RenderedLine(
                "error",
                t.style(t.err, "[!] Setup required: " + msg) + "\n" + t.style(t.dim, hint),
            )
        return RenderedLine("error", t.style(t.err, "x " + msg))

    # token_usage and unknown types are dropped from the transcript
    return None


def extract_token_usage(event: Event) -> str:
    """Parse a `token_usage` event into a one-line status string.

    Mirrors `extractTokenUsage` in events.go. Returns "" for unparseable.
    """
    if event.type != "token_usage":
        return ""
    data: dict[str, Any] = {}
    if isinstance(event.data, dict):
        data = event.data.get("data") if isinstance(event.data.get("data"), dict) else {}
    usage = data.get("token_usage") if isinstance(data.get("token_usage"), dict) else {}
    try:
        in_tok = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return ""
    # ``input_tokens`` is uncached only; add cached reads/writes back for the total.
    cached = cache_read + cache_creation
    total = in_tok + cached + out_tok
    if total == 0:
        return ""
    if cached:
        return f"tokens: {total}  (in {in_tok}, cached {cached} / out {out_tok})"
    return f"tokens: {total}  (in {in_tok} / out {out_tok})"


def _format_action_input(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n    ")
    except (TypeError, ValueError):
        return str(value)
