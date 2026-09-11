"""No bundled doc may teach a trailing `--json`.

`--json` is a ROOT option of the `cremind` CLI (``_root`` in app/cli/main.py),
so it goes right after `cremind`: ``cremind --json tools list``. Typer rejects
``cremind tools list --json`` with "No such option: --json". The documentation
search tool tells the agent exactly that in its CLI directive, so a bundled
example contradicting it would teach the agent a command that fails — which is
what logged conversations show it doing, re-running each one with the flag
moved.

A few commands declare their OWN ``--json`` (a JSON payload, e.g.
``channels add --json '{...}'``). Those are derived from the CLI itself by
walking the Typer tree, so this guard follows the code rather than a list.
Prose that explains the rejected form ("`cremind x --json` is rejected with
No such option") is allowed.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
import typer

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"

# An invocation: the word `cremind`, then everything up to a shell or markdown
# boundary (pipe, separator, backtick, closing paren, end of line).
_INVOCATION = re.compile(r"(?<![\w/.-])cremind[ \t]+([^\n`|;&)]*)")


def _command_tree() -> dict[tuple[str, ...], bool]:
    """Every command/group path -> whether that command declares its own --json."""
    from app.cli.main import app

    tree: dict[tuple[str, ...], bool] = {}

    def walk(typer_app, path: tuple[str, ...]) -> None:
        for command in typer_app.registered_commands:
            callback = command.callback
            name = command.name or (callback.__name__.replace("_", "-") if callback else "")
            own_json = any(
                isinstance(param.default, typer.models.OptionInfo)
                and "--json" in (param.default.param_decls or ())
                for param in inspect.signature(callback).parameters.values()
            )
            tree[path + (name,)] = own_json
        for group in typer_app.registered_groups:
            sub = group.typer_instance
            name = group.name or sub.info.name
            tree.setdefault(path + (name,), False)
            walk(sub, path + (name,))

    walk(app, ())
    return tree


TREE = _command_tree()


def _payload_commands() -> set[str]:
    return {" ".join(path) for path, own in TREE.items() if own}


_EXPLAINS_WRONG_FORM = ("rejected", "No such option", "WRONG")


def _explains_wrong_form(lines: list[str], index: int) -> bool:
    """The line (or, for a sentence wrapped across two, the next line) says the
    form it shows is the wrong one — an explanation, not an example."""
    window = lines[index : index + 2]
    return any(marker in line for line in window for marker in _EXPLAINS_WRONG_FORM)


def _violations(text: str) -> list[str]:
    found = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        if _explains_wrong_form(lines, lineno - 1):
            continue
        for match in _INVOCATION.finditer(line):
            # Synopsis brackets and braces are notation, not part of the flag:
            # `cremind boot status [--json]` teaches a trailing --json too.
            tokens = [t.strip("[]{}") for t in match.group(1).split()]
            tokens = [t for t in tokens if t]
            if not tokens or tokens[0].startswith("-"):
                continue  # root options first: `cremind --json <group> ...` is right
            path: tuple[str, ...] = ()
            rest = tokens
            while rest and path + (rest[0],) in TREE:
                path += (rest[0],)
                rest = rest[1:]
            if not path or "--json" not in rest:
                continue
            if not TREE[path]:
                found.append(f"line {lineno}: `cremind {' '.join(tokens)}`")
    return found


def test_the_walk_finds_the_payload_json_commands():
    """If this drifts, the CLI grew or lost a command with its own --json —
    make sure the CLI directive in documentation_search.py lists it."""
    assert _payload_commands() == {
        "channels add",
        "channels edit",
        "channels notify-filter",
        "embedding set",
        "llm providers configure",
        "setup complete",
        "tools set-args",
    }


def test_the_directive_names_every_payload_json_command():
    from app.tools.builtin.documentation_search import _CLI_EXECUTION_DIRECTIVE

    for command in _payload_commands():
        assert f"`{command}`" in _CLI_EXECUTION_DIRECTIVE, command


@pytest.mark.parametrize("path", sorted(BUNDLED.glob("*.md")), ids=lambda p: p.name)
def test_no_bundled_example_puts_json_after_a_command(path: Path):
    violations = _violations(path.read_text(encoding="utf-8"))
    assert not violations, (
        f"{path.name}: `--json` is a root flag — write `cremind --json <group> "
        f"<command>`: {violations}"
    )


def test_the_guard_catches_a_trailing_json_and_allows_the_right_forms():
    assert _violations("run `cremind tools list --json` now")
    assert _violations("$ cremind me --json | jq .profile")
    assert _violations("cremind boot status [--json]"), "a synopsis teaches it too"
    assert not _violations("cremind [--json] boot status")
    assert not _violations("run `cremind --json tools list` now")
    assert not _violations("cremind channels add --type telegram --json '{}'")
    assert not _violations("`cremind me --json` is rejected with No such option")
    assert not _violations("cremind google status --json   # WRONG - not a subcommand flag")
    assert not _violations("goes before it — `cremind tools list --json`\nis rejected with:")
