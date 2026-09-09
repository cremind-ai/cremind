"""Pin the cwd conventions in ``skill-creator/SKILL.md``.

The file mixes two working directories on purpose. Its authoring workflow runs
after Cremind has anchored cwd to the skill's own directory, so ``..`` there
legitimately means the profile skills root. The delegation brief is different:
the reasoning agent's skill-authoring clause sends the delegate to the skills
root itself (``change_working_directory`` with target ``skills``), so a ``..``
in the brief resolves to the root's *parent* — outside the watched tree, where
a new skill never hot-loads and the brief's own "write nothing outside the
skills root" rule is broken by its own path definition.

These tests keep the two conventions separated and each one stated, rather than
left to whichever cwd the reader happens to be in.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_MD = (
    Path(__file__).resolve().parents[2]
    / "app" / "skills" / "builtin" / "skill-creator" / "SKILL.md"
)


def _text() -> str:
    assert SKILL_MD.exists(), f"missing built-in skill file: {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """Return the body of the ``## <heading>`` section, up to the next ``##``."""
    text = _text()
    start = text.index(heading)
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _delegation_brief() -> str:
    """The fenced ```text block the agent copies into the delegate's prompt."""
    section = _section("## Two ways to build a skill")
    match = re.search(r"```text\n(.*?)```", section, re.S)
    assert match, "the delegation section no longer contains a ```text brief block"
    return match.group(1)


def test_the_brief_defines_the_skills_root_without_a_cwd_assumption():
    intro = _section("## Two ways to build a skill").split("```text")[0]
    defining = next(
        (s for s in re.split(r"(?<=\.)\s", intro) if "`<skills root>`" in s),
        "",
    )
    assert defining, "the delegation section no longer defines `<skills root>`"
    # "the absolute path of `..`" was the original definition: correct only
    # from inside the skill directory, wrong from the skills root the
    # delegation flow actually starts in.
    assert "`..`" not in defining, (
        f"the skills root must not be defined relative to cwd: {defining!r}"
    )
    assert "parent" in defining.lower(), (
        "the skills root must be defined as this skill's parent directory"
    )
    assert "change_working_directory" in intro, (
        "the brief must say that the `skills` target lands in that same directory"
    )


def test_no_path_in_the_brief_is_relative_to_the_reader_cwd():
    brief = _delegation_brief()
    assert ".." not in brief, "the delegation brief must contain no `..` paths"
    for line in brief.splitlines():
        # Every line that names a path either roots it at <skills root> or is
        # relative to it (skill-creator/... is a child of the root).
        if "skill-creator/" in line or "<name>" in line:
            assert "<skills root>" in line or "skill-creator/" in line, (
                f"unrooted path instruction in the delegation brief: {line!r}"
            )


def test_the_validator_invocation_names_the_directory_it_runs_from():
    brief = _delegation_brief()
    line = next(
        (ln for ln in brief.splitlines() if "validate.py" in ln),
        "",
    )
    assert line, "the brief no longer tells the delegate to run validate.py"
    # validate.py resolves its argument against the process cwd, so the brief
    # has to say which directory that is.
    assert "<skills root>" in line, (
        "the validate.py invocation must name <skills root> as the directory it "
        f"runs from; got {line!r}"
    )


def test_the_sibling_path_section_states_the_cwd_it_assumes():
    section = _section("## Where new skills go, and naming")
    assert "../" in section, "this section is the one that uses sibling paths"
    lowered = section.lower()
    assert "cwd is" in lowered or "working directory is" in lowered, (
        "the section that uses `../` must say which cwd it assumes, so it does "
        "not read as contradicting the delegation brief"
    )
