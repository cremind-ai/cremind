"""Log lines must interpolate their values.

The app logs through loguru, not stdlib ``logging``. Loguru does not do
printf-style interpolation: ``logger.info("cwd -> %s", path)`` logs the literal
text ``cwd -> %s`` and silently discards ``path``. Nothing fails, nothing warns
— the line just goes out useless.

Found the hard way. A tool call auto-switched the working directory and logged
``Sandbox denial from '%s'; auto-switching cwd to '%s'``; moments later the
server hung, and the one line that would have named the tool and the directory
named neither.

This walks the AST rather than grepping so a ``%s`` inside an f-string or a
literal percentage ("100%s complete" style text, SQL ``LIKE '%s'``) doesn't
register: what it flags is specifically a logger call whose first argument is a
constant string containing a printf placeholder AND that passes extra
positional arguments — the exact shape that silently drops data.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "success"}
# %s %r %d %(name)s … but not a literal "%%" escape.
_PLACEHOLDER = re.compile(r"(?<!%)%[-+ #0-9.*]*[srdifgeExXoc(]")


def _offenders(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LOG_METHODS:
            continue
        target = node.func.value
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
        if name != "logger":
            continue
        if len(node.args) < 2:
            continue  # no extra positionals → nothing can be dropped
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        if _PLACEHOLDER.search(first.value):
            found.append((node.lineno, first.value[:60]))
    return found


def test_no_printf_style_logger_calls() -> None:
    bad: list[str] = []
    for path in APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for lineno, snippet in _offenders(tree):
            rel = path.relative_to(APP.parent)
            bad.append(f"{rel}:{lineno}: {snippet!r}")

    assert not bad, (
        "loguru does not interpolate printf-style args — these lines log a "
        "literal '%s' and drop the value. Use an f-string:\n  "
        + "\n  ".join(bad)
    )


def test_the_check_can_actually_see_an_offender() -> None:
    """A guard that cannot fail guards nothing."""
    tree = ast.parse('logger.info("switching cwd to %s", path)')
    assert _offenders(tree) == [(1, "switching cwd to %s")]


def test_innocent_shapes_are_not_flagged() -> None:
    for source in (
        'logger.info(f"switching cwd to {path}")',       # the correct form
        'logger.info("100%% done")',                     # escaped literal
        'logger.debug("no watcher", exc_info=True)',     # kwargs, not positionals
        'logger.info("plain text")',
        'other.info("cwd %s", path)',                    # not the logger
    ):
        assert _offenders(ast.parse(source)) == [], source
