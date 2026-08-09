"""Tests for the CLI verb surface, especially `search` vs `list`.

`search` is the whole-mailbox verb the SKILL.md promises: it must require a
query and must never narrow to a category tab or to INBOX.

Run standalone (no pytest needed):  python scripts/tests/test_cli_parser.py
Or via pytest:                      pytest scripts/tests/test_cli_parser.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import cli


class _Recorder:
    """Stands in for operations.list_emails and remembers how it was called."""

    def __init__(self):
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return []


def _run(argv):
    """Run main() with list_emails stubbed; returns (exit_code, call kwargs)."""
    rec = _Recorder()
    original = cli.operations.list_emails
    cli.operations.list_emails = rec
    try:
        code = cli.main(argv)
    finally:
        cli.operations.list_emails = original
    return code, rec.kwargs


def _expect_exit(argv):
    try:
        cli.build_parser().parse_args(argv)
    except SystemExit:
        return True
    return False


def test_search_requires_a_query():
    assert _expect_exit(["search"])


def test_search_rejects_category_because_it_always_searches_everything():
    assert _expect_exit(["search", "--query", "x", "--category", "all"])


def test_search_defaults_match_the_old_gmail_verb():
    args = cli.build_parser().parse_args(["search", "--query", "x"])
    assert args.max_results == 10
    assert args.detail == "summary"
    assert not hasattr(args, "category")


def test_search_searches_all_mail():
    code, kwargs = _run(["--json", "search", "--query", "from:alice"])
    assert code == 0
    assert kwargs["category"] == "all"
    assert kwargs["query"] == "from:alice"


def test_search_passes_through_its_other_flags():
    code, kwargs = _run([
        "--json", "search", "--query", "q", "--max-results", "3",
        "--detail", "full", "--since", "2026-01-01", "--before", "2026-02-01",
    ])
    assert code == 0
    assert kwargs["max_results"] == 3
    assert kwargs["detail"] == "full"
    assert kwargs["since"] == "2026-01-01"
    assert kwargs["before"] == "2026-02-01"


def test_list_still_defaults_to_the_primary_inbox():
    code, kwargs = _run(["--json", "list"])
    assert code == 0
    assert kwargs["category"] == "primary"
    assert kwargs["query"] is None


def test_list_keeps_its_category_flag():
    code, kwargs = _run(["--json", "list", "--category", "spam"])
    assert code == 0
    assert kwargs["category"] == "spam"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
