"""A file attached to a PAST user message keeps its absolute path in history.

An image only ever reaches the model as a path it can hand to
``image_understanding``. That path is appended to the query of the turn the
file arrived on (``_append_attachments_note``) — but a message that never ran
a turn never produced that note. In a platform group most messages are exactly
that: the relevance judge stays quiet, and the row is written with its file
parts and nothing else. A later turn ("what is that image?") then saw only the
row's text, which names the file but not where it is.

Real case: two images arrived in a Zalo group unmentioned, were quiet-written
with correct ``uri``s pointing inside the profile's ``uploads_tmp`` (already an
allowed root for the file tools), and when a member finally asked about them
the agent went hunting for the bare filenames across the disk.

The pins below are the two halves of that: the path is restored, and rows
WITHOUT file parts are rendered byte-identically to before — history feeds the
prompt cache, so an unrelated row must not shift by one character.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")  # app.utils.common imports a2a.types at module load

from app.utils.common import convert_db_messages_to_history  # noqa: E402

_URI = (
    r"C:\Users\lyntc\.cremind\bobo\uploads_tmp"
    r"\ffe1eb96-1247-45ff-a588-937345981aab\photo.jpg"
)


def _file_part(name: str = "photo.jpg", uri: str = _URI) -> dict:
    return {
        "kind": "file",
        "file": {"name": name, "mimeType": "image/jpeg", "uri": uri},
    }


def _quiet_row(parts: object, content: str = "Lý Nguyen: [sent a file: photo.jpg]") -> dict:
    """A group message the judge stayed quiet on — stored, never answered."""
    return {
        "role": "user",
        "content": content,
        "parts": parts,
        "metadata": {"channel_group": {"group_id": "g1", "quiet": True}},
    }


# ── the path is restored ──────────────────────────────────────────────────


def test_a_file_part_contributes_its_absolute_path() -> None:
    out = convert_db_messages_to_history([_quiet_row([_file_part()])])

    (msg,) = out
    assert msg["role"] == "user"
    # The row's own text is kept verbatim, first.
    assert msg["content"].startswith("Lý Nguyen: [sent a file: photo.jpg]")
    # And the path — the part the agent actually needs — is now present.
    assert _URI in msg["content"]
    assert "photo.jpg" in msg["content"]


def test_several_files_each_get_a_line() -> None:
    row = _quiet_row([
        _file_part("a.jpg", _URI),
        _file_part("b.pdf", _URI.replace("photo.jpg", "b.pdf")),
    ])
    content = convert_db_messages_to_history([row])[0]["content"]

    assert _URI in content
    assert _URI.replace("photo.jpg", "b.pdf") in content
    assert content.count("absolute path:") == 2


def test_a_file_only_row_with_no_text_still_reaches_the_model() -> None:
    """Empty content is normally dropped (``if not content.strip(): continue``).
    A row whose whole point is the file must survive that filter."""
    out = convert_db_messages_to_history([_quiet_row([_file_part()], content="")])

    assert len(out) == 1
    assert _URI in out[0]["content"]


def test_the_name_falls_back_to_the_basename() -> None:
    part = {"kind": "file", "file": {"uri": _URI}}
    content = convert_db_messages_to_history([_quiet_row([part])])[0]["content"]
    assert "photo.jpg" in content
    assert _URI in content


# ── everything else is untouched ──────────────────────────────────────────


@pytest.mark.parametrize("parts", [None, "null", [], {}, "not-a-list", 7])
def test_rows_without_usable_parts_are_byte_identical(parts: object) -> None:
    """``parts`` is a JSON column: it arrives as None, as a list, or — for rows
    written before/around this — as something else entirely. None of those may
    change the rendered message, or every history after it misses the cache."""
    row = _quiet_row(parts)
    assert convert_db_messages_to_history([row]) == [
        {"role": "user", "content": "Lý Nguyen: [sent a file: photo.jpg]"},
    ]


@pytest.mark.parametrize("part", [
    "a string, not a part",
    {"kind": "text", "text": "hello"},          # not a file part
    {"kind": "file"},                            # no file object
    {"kind": "file", "file": "not-a-dict"},
    {"kind": "file", "file": {"name": "x.jpg"}},  # no uri
    {"kind": "file", "file": {"name": "x.jpg", "uri": "   "}},  # blank uri
])
def test_a_malformed_part_is_skipped_not_raised_on(part: object) -> None:
    """History assembly runs on every turn; one bad row must not kill it."""
    assert convert_db_messages_to_history([_quiet_row([part])]) == [
        {"role": "user", "content": "Lý Nguyen: [sent a file: photo.jpg]"},
    ]


def test_a_good_part_survives_a_malformed_sibling() -> None:
    row = _quiet_row(["junk", _file_part(), {"kind": "file"}])
    assert _URI in convert_db_messages_to_history([row])[0]["content"]


def test_agent_rows_are_left_alone() -> None:
    """Parts on an agent row describe a file the agent SENT. Its path is the
    server's business, not something to read back to the model."""
    db = [{
        "role": "agent",
        "content": "here you go",
        "parts": [_file_part("report.pdf", r"C:\Users\lyntc\Documents\report.pdf")],
    }]
    assert convert_db_messages_to_history(db) == [
        {"role": "assistant", "content": "here you go"},
    ]


def test_a_replayed_trace_still_wins_over_parts() -> None:
    """When a row carries a real reasoning trace, the trace is spliced verbatim
    — appending to it would corrupt the cached prefix."""
    trace = [{"role": "assistant", "content": "done"}]
    db = [{
        "role": "user",
        "content": "look at this",
        "parts": [_file_part()],
        "llm_messages": trace,
    }]
    assert convert_db_messages_to_history(db, include_reasoning=True) == trace
