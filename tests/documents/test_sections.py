"""Tests for the pure Markdown-structure module behind section delivery.

Two halves:

- unit tests over hand-written bodies, which pin the delicate parts — the
  fenced-code-block state machine and the heading-lookup tiers;
- a **corpus pin** parametrized over every real bundled document, which fails
  the moment a doc author writes something the delivery path cannot serve
  (no ``##`` at all, a duplicated heading, or an ``###`` too big to return).
"""

from __future__ import annotations

import textwrap
from collections import Counter
from pathlib import Path

import pytest

from app.documents.sections import (
    SectionSizes,
    children,
    find_section,
    normalize_heading,
    open_fence_at_end,
    rank_sections_for_query,
    render_toc,
    section_own_text,
    section_text,
    size_sections,
    split_sections,
)


def tok(text: str) -> int:
    """Stand-in tokenizer: roughly four characters to a token."""
    return max(1, len(text) // 4)


def body(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def titles(sections) -> list[str]:
    return [s.title for s in sections]


# ---------------------------------------------------------------------------
# splitting
# ---------------------------------------------------------------------------

BASIC = body(
    """
    # Cremind Channels

    Intro prose that belongs to nobody.

    ## Alpha

    Alpha's own body.

    ### Alpha one

    First child.

    #### A deeper heading

    Still inside the first child.

    ### Alpha two

    Second child.

    ## Beta

    Beta's body.
    """
)


def test_split_returns_lines_head_sections_in_that_order():
    lines, head, sections = split_sections(BASIC)

    assert lines == BASIC.splitlines()
    assert head == ["# Cremind Channels", "", "Intro prose that belongs to nobody.", ""]
    assert titles(sections) == ["Alpha", "Alpha one", "Alpha two", "Beta"]


def test_h1_lands_in_the_head_and_is_never_a_section():
    _, head, sections = split_sections(BASIC)

    assert "# Cremind Channels" in head
    assert all(s.level in (2, 3) for s in sections)


def test_h4_is_content_not_a_boundary():
    lines, _, sections = split_sections(BASIC)
    alpha_one = sections[1]

    assert "A deeper heading" not in titles(sections)
    assert "#### A deeper heading" in section_text(lines, alpha_one)


def test_levels_and_document_order_index():
    _, _, sections = split_sections(BASIC)

    assert [s.level for s in sections] == [2, 3, 3, 2]
    assert [s.index for s in sections] == [0, 1, 2, 3]


def test_own_end_stops_at_the_first_child_while_end_includes_them():
    lines, _, sections = split_sections(BASIC)
    alpha, alpha_one, alpha_two, beta = sections

    # own_end: the next heading of *any* boundary level.
    assert lines[alpha.own_end] == "### Alpha one"
    assert lines[alpha_one.own_end] == "### Alpha two"

    # end: the next heading of this level or shallower.
    assert lines[alpha.end] == "## Beta"
    assert lines[alpha_one.end] == "### Alpha two"
    assert lines[alpha_two.end] == "## Beta"

    # The last section runs to the end of the document.
    assert beta.own_end == beta.end == len(lines)

    own = section_own_text(lines, alpha)
    assert "Alpha's own body." in own
    assert "First child." not in own

    total = section_text(lines, alpha)
    assert "First child." in total
    assert "Second child." in total
    assert "Beta's body." not in total


def test_children_of_a_parent_and_of_a_leaf():
    _, _, sections = split_sections(BASIC)
    alpha, alpha_one, _, beta = sections

    assert titles(children(sections, alpha)) == ["Alpha one", "Alpha two"]
    assert children(sections, alpha_one) == []
    assert children(sections, beta) == []


def test_heading_keeps_backticks_and_drops_a_closing_hash_run():
    _, _, sections = split_sections("## `cremind channels add` ##\n\nbody\n")

    assert titles(sections) == ["`cremind channels add`"]


def test_document_with_no_h2_or_h3_is_all_head():
    text = body(
        """
        # Just a title

        Some prose.

        #### Not a boundary
        """
    )
    lines, head, sections = split_sections(text)

    assert sections == []
    assert head == lines


def test_empty_body():
    lines, head, sections = split_sections("")

    assert (lines, head, sections) == ([], [], [])


# ---------------------------------------------------------------------------
# fenced code blocks
# ---------------------------------------------------------------------------


def test_hash_lines_inside_a_bash_fence_are_not_headings():
    text = body(
        """
        ## Real heading

        ```bash
        # add a channel
        cremind channels add telegram
        ## not a heading either
        ```

        ## Second real heading
        """
    )
    _, _, sections = split_sections(text)

    assert titles(sections) == ["Real heading", "Second real heading"]


def test_indented_fence_opens_and_closes():
    # Real docs indent fences inside list items, e.g.
    # app/documents/bundled/[cli]cremind llm.md and [tool]codex.md.
    text = body(
        """
        ## Real heading

        - **CLI** — list, then set:

          ```bash
          cremind tools options codex
          ## still inside the fence
          ```

        ## After the fence
        """
    )
    _, _, sections = split_sections(text)

    assert titles(sections) == ["Real heading", "After the fence"]


def test_indented_fence_hides_a_column_zero_hash_line():
    # The opener is indented, the '##' line inside it is not: only a parser
    # that recognised the indented opener knows that line is fence content.
    text = body(
        """
        ## Real heading

          ```bash
        ## fence content, not a heading
          ```

        ## After the fence
        """
    )
    _, _, sections = split_sections(text)

    assert titles(sections) == ["Real heading", "After the fence"]


@pytest.mark.parametrize(
    "closer,closes",
    [
        ("```", True),
        ("   ```", True),  # up to three spaces of indentation still closes
        ("    ```", False),  # four is an indented code line, not a fence
        ("`````", True),  # a longer run than the opener closes
        ("``", False),  # a shorter run does not
    ],
)
def test_closing_fence_indentation_and_run_length(closer, closes):
    text = "## Real heading\n\n```bash\ncremind x\n" + closer + "\n\n## After the fence\n"
    _, _, sections = split_sections(text)

    expected = ["Real heading", "After the fence"] if closes else ["Real heading"]
    assert titles(sections) == expected


def test_tilde_fence():
    text = body(
        """
        ## Real heading

        ~~~
        ## hidden by tildes
        ~~~

        ## After the fence
        """
    )
    _, _, sections = split_sections(text)

    assert titles(sections) == ["Real heading", "After the fence"]


def test_a_backtick_fence_is_not_closed_by_a_tilde_line():
    text = body(
        """
        ## Real heading

        ```
        ~~~
        ## hidden
        ```

        ## After the fence
        """
    )
    _, _, sections = split_sections(text)

    assert titles(sections) == ["Real heading", "After the fence"]


def test_four_backtick_fence_is_not_closed_by_a_three_backtick_line():
    text = body(
        """
        ## Real heading

        ````markdown
        ```bash
        cremind channels list
        ```
        ## still inside the outer fence
        ````

        ## After the fence
        """
    )
    _, _, sections = split_sections(text)

    assert titles(sections) == ["Real heading", "After the fence"]


def test_a_closing_fence_may_not_carry_an_info_string():
    text = body(
        """
        ## Real heading

        ```
        ## hidden
        ``` trailing words are not a close
        ## also hidden
        ```

        ## After the fence
        """
    )
    _, _, sections = split_sections(text)

    assert titles(sections) == ["Real heading", "After the fence"]


def test_unclosed_fence_swallows_every_later_heading():
    text = body(
        """
        ## Real heading

        ```bash
        cremind channels add telegram

        ## never seen
        ### never seen either
        """
    )
    _, _, sections = split_sections(text)

    assert titles(sections) == ["Real heading"]


# ---------------------------------------------------------------------------
# sizes and the outline
# ---------------------------------------------------------------------------


def test_size_sections_own_versus_total():
    lines, _, sections = split_sections(BASIC)
    sizes = size_sections(lines, sections, tok)

    assert set(sizes) == {s.index for s in sections}
    assert all(isinstance(v, SectionSizes) for v in sizes.values())

    alpha, alpha_one, _, _ = sections
    assert sizes[alpha.index].own < sizes[alpha.index].total
    # A leaf's own text is its whole text.
    assert sizes[alpha_one.index].own == sizes[alpha_one.index].total

    assert sizes[alpha.index].total == tok(section_text(lines, alpha))
    assert sizes[alpha.index].own == tok(section_own_text(lines, alpha))


def test_render_toc_indents_h3_and_reports_inclusive_totals():
    lines, _, sections = split_sections(BASIC)
    sizes = size_sections(lines, sections, tok)

    expected = "\n".join(
        [
            f"- Alpha ({sizes[0].total})",
            f"  - Alpha one ({sizes[1].total})",
            f"  - Alpha two ({sizes[2].total})",
            f"- Beta ({sizes[3].total})",
        ]
    )
    assert render_toc(sections, sizes) == expected


def test_render_toc_of_nothing_is_empty():
    assert render_toc([], {}) == ""


# ---------------------------------------------------------------------------
# normalize_heading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("`cremind channels add`", "cremind channels add"),
        ("  ## `cremind channels add`  ", "cremind channels add"),
        ("### Flags:", "flags"),
        ("Notes.", "notes"),
        ("CHANNELS ADD", "channels add"),
        ("cremind   channels\tadd", "cremind channels add"),
        ("#### Deep", "deep"),
    ],
)
def test_normalize_heading(raw, expected):
    assert normalize_heading(raw) == expected


# ---------------------------------------------------------------------------
# find_section
# ---------------------------------------------------------------------------

CHANNELS = body(
    """
    # cremind channels

    ## `cremind channels`

    Overview.

    ### `cremind channels add`

    Add a channel.

    ### Examples of `cremind channels add`

    A near-namesake: "channels add" is a whole-word run inside this title too.

    ### `cremind channels enable` / `cremind channels disable`

    Flip a channel on or off.

    ### `cremind group add`

    Add a group.

    ### Channel groups

    About groups.

    ## Troubleshooting

    When it breaks.
    """
)


def _find(query: str):
    _, _, sections = split_sections(CHANNELS)
    return find_section(sections, query)


@pytest.mark.parametrize(
    "query",
    [
        "`cremind channels add`",  # exact, backticks and all
        "cremind channels add",  # backticked title matched by plain text
        "cremind channels add.",  # trailing punctuation
        "### `cremind channels add`",  # the model pasted the whole heading line
        # Tier 2. The whole-word tier alone would also hit "Examples of
        # `cremind channels add`", so these pin the prefix-stripped equality.
        "channels add",
        "CHANNELS ADD",
    ],
)
def test_find_section_resolves_one_section(query):
    matches, suggestions = _find(query)

    assert titles(matches) == ["`cremind channels add`"]
    assert suggestions == []


def test_find_section_matches_one_half_of_a_compound_heading():
    matches, suggestions = _find("channels disable")

    assert titles(matches) == ["`cremind channels enable` / `cremind channels disable`"]
    assert suggestions == []


def test_find_section_finds_a_heading_pasted_with_trailing_words():
    # Both "`cremind channels`" and "`cremind channels add`" sit inside this
    # query; the shorter one is part of the longer, so only the longer counts.
    matches, suggestions = _find("### `cremind channels add` — Flags")

    assert titles(matches) == ["`cremind channels add`"]
    assert suggestions == []


def test_find_section_stems_words_in_both_directions():
    # "channels" is longer than the title's "channel"; "group" is shorter
    # than the title's "groups".
    matches, suggestions = _find("channels group")

    assert titles(matches) == ["Channel groups"]
    assert suggestions == []


def test_find_section_is_ambiguous_across_distinct_titles():
    matches, suggestions = _find("add")

    assert len(matches) > 1
    assert len({normalize_heading(s.title) for s in matches}) > 1
    assert suggestions == []


def test_find_section_returns_every_duplicate_heading():
    text = body(
        """
        ## First

        ### Notes

        One.

        ## Second

        ### Notes

        Two.
        """
    )
    _, _, sections = split_sections(text)

    # Tier 1, and tier 4's "drop a shorter title swallowed by a longer one":
    # identical titles are the same length, so neither swallows the other.
    for query in ("notes", "### Notes — Flags"):
        matches, suggestions = find_section(sections, query)

        assert len(matches) == 2, query
        assert {normalize_heading(s.title) for s in matches} == {"notes"}
        assert [s.index for s in matches] == sorted(s.index for s in matches)
        assert suggestions == []


def test_find_section_suggests_titles_sharing_a_word():
    matches, suggestions = _find("channels remove")

    assert matches == []
    assert "`cremind channels add`" in suggestions
    assert "Channel groups" in suggestions  # "channels" stems to "channel"
    assert "Troubleshooting" not in suggestions
    # Original titles, deduped.
    assert len(suggestions) == len(set(suggestions))


def test_find_section_ranks_the_closest_heading_first_among_many_sharers():
    """Every heading here repeats the doc's name, so they all share a word
    with the query; a document-order list would bury the one that was meant."""
    subcommands = [
        "list", "add", "edit", "enable", "senders", "approve", "revoke",
        "clear-history", "forget", "pair", "repair", "delete", "catalog",
    ]
    text = "\n\n".join(f"### `cremind channels {name}`" for name in subcommands)
    _, _, sections = split_sections(text)

    matches, suggestions = find_section(sections, "channels paring")

    assert matches == []
    assert suggestions[0] == "`cremind channels pair`"
    assert len(suggestions) == 8


def test_find_section_suggests_a_close_spelling_with_no_shared_word():
    matches, suggestions = _find("trubleshooting")

    assert matches == []
    assert "Troubleshooting" in suggestions


def test_find_section_caps_suggestions_at_eight_in_document_order():
    text = "\n\n".join(f"### `cremind widget cmd{i:02d}`" for i in range(12))
    _, _, sections = split_sections(text)
    matches, suggestions = find_section(sections, "widget frobnicate")

    assert matches == []
    assert suggestions == [f"`cremind widget cmd{i:02d}`" for i in range(8)]


def test_find_section_on_an_empty_corpus():
    assert find_section([], "anything") == ([], [])


# ---------------------------------------------------------------------------
# rank_sections_for_query
# ---------------------------------------------------------------------------

RANKED = body(
    """
    # cremind channels

    ## Groups

    About groups.

    ### Group brakes

    Bot-streak brakes.

    ## `cremind channels add`

    Add a channel.

    ## `cremind channels list`

    List channels.

    ## `cremind channels remove`

    Remove a channel.

    ## Troubleshooting

    When it breaks.
    """
)


def _rank(query: str, stop_words: set[str]):
    _, _, sections = split_sections(RANKED)
    return rank_sections_for_query(sections, query, stop_words)


def test_rank_drops_doc_name_words_so_they_do_not_match_everything():
    # The doc's own name is in every command heading, so on its own it
    # "matches" all of them.
    without_stops = _rank("cremind channels add", set())
    assert titles(without_stops) == [
        "`cremind channels add`",
        "`cremind channels list`",
        "`cremind channels remove`",
    ]

    # With the doc-name words stopped, only the word that carries meaning is
    # left to score.
    with_stops = _rank("cremind channels add", {"cremind", "channels"})
    assert titles(with_stops) == ["`cremind channels add`"]


def test_rank_prefers_a_child_over_its_parent():
    ranked = _rank("groups", set())

    assert titles(ranked) == ["Group brakes"]


def test_rank_drops_zero_scoring_sections():
    ranked = _rank("brakes", set())

    assert titles(ranked) == ["Group brakes"]


def test_rank_sorts_by_score_then_document_order():
    text = body(
        """
        ## Add a channel

        One.

        ## Remove a channel

        Two.

        ## Add and remove a channel

        Three.
        """
    )
    _, _, sections = split_sections(text)
    ranked = rank_sections_for_query(sections, "add remove", set())

    # Score 2 first; the two score-1 sections follow in document order.
    assert titles(ranked) == ["Add and remove a channel", "Add a channel", "Remove a channel"]


def test_rank_with_an_all_stop_word_query_returns_nothing():
    assert _rank("cremind channels", {"cremind", "channels"}) == []


# ---------------------------------------------------------------------------
# corpus pin — the real bundled documents
# ---------------------------------------------------------------------------

BUNDLED_DIR = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"
BUNDLED_DOCS = sorted(BUNDLED_DIR.glob("*.md"))

# An ``###`` whose inclusive text is bigger than this cannot be delivered in
# one piece, so the pin below tells the doc author to split it.
MAX_H3_TOKENS = 3000


def _doc_body(path: Path) -> str:
    """The Markdown body: everything after the closing frontmatter ``---``."""
    lines = path.read_text(encoding="utf-8").lstrip("﻿").splitlines()
    seen = 0
    for i, line in enumerate(lines):
        if line.strip() == "---":
            seen += 1
            if seen == 2:
                return "\n".join(lines[i + 1 :])
    pytest.fail(f"{path.name}: no closing frontmatter delimiter")


def test_the_corpus_was_actually_found():
    assert len(BUNDLED_DOCS) > 20


@pytest.mark.parametrize("path", BUNDLED_DOCS, ids=lambda p: p.name)
def test_bundled_doc_has_at_least_one_h2(path: Path):
    _, _, sections = split_sections(_doc_body(path))

    assert [s for s in sections if s.level == 2], (
        f"{path.name}: no `##` section — section delivery has nothing to offer. "
        "Give the body top-level headings (see bundled/document.md)."
    )


@pytest.mark.parametrize("path", BUNDLED_DOCS, ids=lambda p: p.name)
def test_bundled_doc_headings_are_unique(path: Path):
    _, _, sections = split_sections(_doc_body(path))
    dupes = [t for t, n in Counter(normalize_heading(s.title) for s in sections).items() if n > 1]

    assert not dupes, (
        f"{path.name}: duplicate headings {dupes} — a reader asking for one of "
        "these cannot be told which is meant. Make each heading unique."
    )


@pytest.mark.parametrize("path", BUNDLED_DOCS, ids=lambda p: p.name)
def test_bundled_doc_h3_sections_fit_in_one_delivery(path: Path):
    lines, _, sections = split_sections(_doc_body(path))
    sizes = size_sections(lines, sections, tok)

    too_big = sorted(
        s.title
        for s in sections
        if s.level == 3 and sizes[s.index].total > MAX_H3_TOKENS
    )

    assert not too_big, (
        f"{path.name}: {too_big} exceed {MAX_H3_TOKENS} tokens — split them into "
        "smaller `###` sections."
    )


# ---------------------------------------------------------------------------
# open_fence_at_end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lines, expected",
    [
        (["intro", "```bash", "echo hi"], "```"),
        (["intro", "```bash", "echo hi", "```"], None),
        (["~~~~", "code"], "~~~~"),
        (["  ```", "# comment inside"], "```"),
        (["````", "```", "still code"], "````"),
        (["no fences at all"], None),
        (["``` has `backtick` in info so not a fence"], None),
    ],
)
def test_open_fence_at_end(lines, expected):
    assert open_fence_at_end(lines) == expected
