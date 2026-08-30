"""Turning a group post into the text a model reads, and back again.

Two directions, both pure:

:func:`render_attributed` prefixes a post with who said it. It has to live in the
message CONTENT rather than in metadata because
:func:`app.utils.common.convert_db_messages_to_history` hands the model only
``role`` and ``content`` — a speaker recorded anywhere else is a speaker the
model never learns about.

:func:`split_segments` and :func:`is_silent` read a finished turn back out. A
turn interrupted mid-flight speaks more than once (an acknowledgement, then the
answer), and ``stream_runner`` concatenates all of it into one string with the
cut points recorded separately — so the split has to happen here before each
piece is judged. Judging the whole string at once is the bug this exists to
avoid: an agent that says "Got it, checking" and then decides to stay quiet ends
up with ``"Got it, checking\\n\\n[silent]"``, where a whole-string test sees no
sentinel and posts it verbatim.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# Characters a model wraps a sentinel in without meaning to change it: emphasis
# marks, terminal punctuation, quotes, whitespace — and the sentinel's own
# brackets, so ``[silent]``, ``**[silent]**`` and ``silent.`` all reduce alike.
_SENTINEL_TRIM = "*_`~.!?,:;\"'“”‘’ \t\r\n[]()"

# The routing note appended to a delivered post, telling the agent who the
# room's router expected to answer it. Written on its own last line so it reads
# as an annotation rather than part of what the sender said, and kept here
# beside ``is_silent`` because the two must agree: an agent that copies the note
# onto its own answer would otherwise defeat the sentinel (``"[silent]\n[to:
# you]"`` does not reduce to "silent"), and the room would be shown the word.
ROUTING_NOTE_YOU = "[to: you]"
ROUTING_NOTE_EVERYONE = "[to: everyone in the room]"

_ROUTING_NOTE_RE = re.compile(r"^\s*\[to:[^\]\n]*\]\s*$")

# The same shape, but tacked onto the end of a line rather than standing on its
# own. Used only to JUDGE silence, never to rewrite a post: see
# :func:`is_silent_ignoring_notes`.
_INLINE_NOTE_TAIL_RE = re.compile(r"\s*\[to:[^\]\n]*\]\s*$")


def render_attributed(sender_name: str, sender_kind: str, text: str) -> str:
    """The line a member's agent actually receives: ``Alexa (user): …``.

    ``sender_kind`` is spelled out rather than implied because it is the whole
    basis of the agent's judgement about what to do: a *user* assigns work, a
    peer *agent* coordinates, and ``system`` is the room talking about itself.
    """
    name = (sender_name or "").strip() or "Someone"
    kind = (sender_kind or "user").strip() or "user"
    return f"{name} ({kind}): {text}"


def routing_note_for_names(names: Sequence[str]) -> str:
    """The note naming who was woken: ``[to: you, Mimi]``.

    Callers put ``"you"`` first when the reader is one of them, so an agent sees
    both that it was asked and who else was — a bare ``[to: you]`` on a
    two-target decision would have it answer as though it were alone.
    """
    listed = ", ".join(n for n in names if (n or "").strip())
    return f"[to: {listed}]" if listed else ""


def strip_routing_notes(text: Optional[str]) -> str:
    """Drop routing notes an agent copied onto the end of its own answer.

    Every message a seat receives now ends with one, so a model imitating the
    shape it sees is a matter of time — and the cost is not cosmetic: with a
    note stuck to it, ``[silent]`` no longer reduces to the sentinel, so the
    turn that meant to say nothing posts the word "[silent]" into the room.
    Stripped before the sentinel test rather than
    forbidden in the prompt alone, because the prompt is advice and this is not.

    Only trailing note-shaped lines go; a note in the middle of a sentence is
    the agent quoting something, and quoting is not annotating.
    """
    lines = (text or "").splitlines()
    while lines and _ROUTING_NOTE_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


def is_silent_ignoring_notes(text: Optional[str]) -> bool:
    """Whether this is the sentinel, with any routing note the agent copied
    onto it discounted — including one it put on the SAME line.

    Two functions rather than one because the two answers are used for
    different things, and only one of them may rewrite a post.
    :func:`strip_routing_notes` cleans the text that is actually published, so
    it only ever removes whole lines: an answer that happens to end "…forward it
    [to: Mimi]" is a sentence, and silently editing it would be worse than
    leaving the note in. Judging silence has no such cost — nothing is
    published either way — so it may be as tolerant as it likes, and the
    asymmetry is the same one :func:`is_silent` is already built on: a missed
    sentinel is the word "[silent]" posted into a room for everyone to read, a
    false positive is one unsent line.
    """
    stripped = strip_routing_notes(text)
    if is_silent(stripped):
        return True
    return is_silent(_INLINE_NOTE_TAIL_RE.sub("", stripped))


def is_silent(text: Optional[str]) -> bool:
    """Whether this piece of a turn means "nothing to say".

    Tolerant on purpose. A missed sentinel is visible to everyone in the room
    while a false positive costs one unsent line, so
    the asymmetry is worth a slightly loose match. Text that merely *starts*
    with the sentinel and continues is NOT silent — the agent had something to
    add.
    """
    core = (text or "").strip()
    if not core:
        return True
    return core.strip(_SENTINEL_TRIM).lower() == "silent"


def strip_silent_lines(text: Optional[str]) -> str:
    """The part of a turn that is meant to be published, sentinel lines removed.

    Line-wise rather than whole-string because a turn interrupted mid-flight
    speaks twice — "Got it, checking" and then, once it has looked, ``[silent]``
    — and whoever reads the turn afterwards sees only the concatenation, with no
    record of where the cut points were. Testing the whole string would publish
    the sentinel verbatim; dropping the sentinel lines keeps the part the agent
    meant to say.

    Shared so that "did this turn say anything?" has ONE answer: the channel
    forwarder uses it to decide what to post, and the stream runner uses it to
    stamp the outcome the model's replayed history is filtered on. Two
    implementations that disagreed would mean a post the history says never
    happened.
    """
    kept = [line for line in (text or "").splitlines() if not is_silent(line)]
    return "\n".join(kept).strip()


def split_segments(
    raw_text: Optional[str], breaks: Optional[List[Dict[str, Any]]],
) -> List[str]:
    """Cut a turn's text back into the pieces the user watched it say.

    ``breaks`` are ``stream_runner``'s ``mid_turn_breaks``, whose
    ``content_offset`` is a character index into the UNSTRIPPED concatenation of
    everything the turn emitted. Offsets at 0 or past the end are ignored (an
    interruption before the agent had said anything cuts nothing).
    """
    text = raw_text or ""
    offsets = sorted({
        int(b.get("content_offset") or 0)
        for b in (breaks or [])
        if isinstance(b, dict)
    })
    offsets = [o for o in offsets if 0 < o < len(text)]
    cuts = [0, *offsets, len(text)]
    out: List[str] = []
    for start, end in zip(cuts, cuts[1:]):
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
    return out
