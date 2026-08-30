"""The group-chat rule in the single history chokepoint, and what it must NOT do.

*Silent turns are hidden.* Every member is asked about every message, so most
turns in a busy room end in the sentinel. Replaying a wall of ``[silent]``
teaches the model that silence is the house style, and it stops answering at all.
The rule is keyed off ``metadata.group``, so it cannot reach a conversation that
is not a group seat — the surrounding tests in ``test_convert_history.py`` pin
that the ordinary shapes are unchanged.

*Runs of group posts stay separate.* They were once joined into a single message,
which quietly cost every quiet seat its prompt cache: dropping a silent turn puts
the posts on either side next to each other, so the joined block grew on each
turn and rewrote a message that had already been sent. The prefix diverged there
and the whole room transcript was re-processed every turn. The last test here is
the one that matters — it pins the property a prefix cache actually needs.
"""

from __future__ import annotations

from app.utils.common import convert_db_messages_to_history


def _user(content, *, group=True, **meta):
    metadata = {"group": {"group_id": "g1", **meta}} if group else {}
    return {"role": "user", "content": content, "metadata": metadata}


def _agent(content, **group_meta):
    metadata = {"group": {"group_id": "g1", **group_meta}} if group_meta else {}
    return {"role": "agent", "content": content, "metadata": metadata}


def test_a_silent_turn_never_reaches_the_model():
    out = convert_db_messages_to_history([
        _user("Alexa (user): what time is it?"),
        _agent("[silent]", kind="silent", posted_message_ids=[]),
        _user("Alexa (user): Dog, you there?"),
    ])
    assert all("[silent]" not in m["content"] for m in out)
    assert all(m["role"] == "user" for m in out)
    assert "what time is it?" in out[0]["content"]
    assert "Dog, you there?" in out[-1]["content"]


def test_a_silent_turn_is_hidden_with_trace_replay_too():
    """Its stored trace ends with the same sentinel, so replaying it would put
    the habit back."""
    rows = [
        _user("Alexa (user): hi"),
        {
            "role": "agent",
            "content": "[silent]",
            "metadata": {"group": {"kind": "silent"}},
            "llm_messages": [{"role": "assistant", "content": "[silent]"}],
        },
    ]
    out = convert_db_messages_to_history(rows, include_reasoning=True)
    assert all("[silent]" not in str(m.get("content")) for m in out)
    assert [m["role"] for m in out] == ["user"]


def test_a_silent_turn_still_carries_what_it_absorbed_mid_flight():
    """The two rules compose in a way that used to lose messages outright.

    A post that arrived while a member was mid-turn is folded into that turn and
    its own row is marked ``consumed`` — deliberately hidden, because the trace
    carries it. If that turn then decides it has nothing to add, dropping the
    silent row wholesale would delete the only surviving copy. In a room, where
    staying silent is the common outcome and mid-turn arrival is normal, that
    lost group messages routinely.
    """
    rows = [
        _user("Alexa (user): deploy when ready"),
        {
            "role": "user",
            "content": "Cat (agent): deploy done — do NOT restart the server",
            "metadata": {
                "group": {"group_id": "g1"},
                "mid_turn": {"state": "consumed", "run_id": "r1"},
            },
        },
        {
            "role": "agent",
            "content": "[silent]",
            "metadata": {"group": {"kind": "silent"}},
            "llm_messages": [
                {
                    "role": "user",
                    "content": (
                        "[New group message] Cat (agent): deploy done — "
                        "do NOT restart the server"
                    ),
                },
                {"role": "assistant", "content": "[silent]"},
            ],
        },
    ]
    out = convert_db_messages_to_history(rows, include_reasoning=True)
    assert any("do NOT restart" in str(m["content"]) for m in out)
    assert all("[silent]" not in str(m["content"]) for m in out)


def test_a_silent_turn_keeps_the_work_it_actually_did():
    """It may have called tools before concluding it had nothing to say; those
    calls and their results are real history."""
    rows = [{
        "role": "agent",
        "content": "[silent]",
        "metadata": {"group": {"kind": "silent"}},
        "llm_messages": [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "x"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "server ok"},
            {"role": "assistant", "content": "[silent]"},
        ],
    }]
    out = convert_db_messages_to_history(rows, include_reasoning=True)
    assert [m["role"] for m in out] == ["assistant", "tool"]


def test_a_posted_turn_is_kept():
    out = convert_db_messages_to_history([
        _user("Alexa (user): what time is it?"),
        _agent("It is 12:00.", kind="posted", posted_message_ids=["m1"]),
    ])
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert out[1]["content"] == "It is 12:00."


def test_consecutive_group_posts_stay_separate_messages():
    """One message per post, in order — a room reads as a sequence of turns, and
    every provider here takes consecutive user messages."""
    out = convert_db_messages_to_history([
        _user("Cat (agent): the article is live"),
        _user("Chicken (agent): the server is stable"),
        _user("Alexa (user): thanks both"),
    ])
    assert [m["role"] for m in out] == ["user", "user", "user"]
    assert [m["content"] for m in out] == [
        "Cat (agent): the article is live",
        "Chicken (agent): the server is stable",
        "Alexa (user): thanks both",
    ]


def test_an_assistant_turn_sits_between_the_posts_it_answered():
    out = convert_db_messages_to_history([
        _user("Alexa (user): status?"),
        _agent("All good.", kind="posted"),
        _user("Cat (agent): agreed"),
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "user"]


def test_a_dropped_silent_turn_leaves_two_separate_user_rows():
    """Dropping the silent turn puts the posts either side of it next to each
    other. They stay two messages: joining them would rewrite the first one,
    which is already in the model's cached prefix."""
    out = convert_db_messages_to_history([
        _user("Alexa (user): first"),
        _agent("[silent]", kind="silent"),
        _user("Alexa (user): second"),
    ])
    assert [m["content"] for m in out] == [
        "Alexa (user): first",
        "Alexa (user): second",
    ]


def test_non_group_user_rows_render_one_per_row():
    """An ordinary conversation must render exactly as it did before."""
    out = convert_db_messages_to_history([
        _user("one", group=False),
        _user("two", group=False),
    ])
    assert [m["content"] for m in out] == ["one", "two"]


def test_a_group_row_and_an_ordinary_one_are_two_messages():
    out = convert_db_messages_to_history([
        _user("typed in the web composer", group=False),
        _user("Cat (agent): posted in the room"),
    ])
    assert len(out) == 2


def test_a_silent_seats_history_is_a_prefix_of_its_next_turns_history():
    """The property a prompt cache actually needs, and the one the old merge
    broke: everything the model was sent last turn is still there, byte for
    byte, with the new post appended after it.

    A seat that keeps quiet is the common case in a room — every member is asked
    about every message — so this is the shape that decides whether a room is
    cheap or re-pays for its whole transcript on every single turn.
    """
    def room(posts: int, *, replay: bool):
        rows = []
        for i in range(1, posts + 1):
            rows.append(_user(f"Alexa (user): post {i}"))
            if i < posts:
                rows.append({
                    "role": "agent", "content": "[silent]",
                    "metadata": {"group": {"kind": "silent"}},
                    # A silent turn that called no tool stores no trace, which is
                    # what makes the posts either side of it adjacent.
                    "llm_messages": None,
                })
        # The current turn's own row is excluded from history by stream_runner.
        return convert_db_messages_to_history(rows[:-1], include_reasoning=replay)

    for replay in (False, True):
        for turn in range(2, 7):
            older = room(turn, replay=replay)
            newer = room(turn + 1, replay=replay)
            assert newer[: len(older)] == older, (
                f"turn {turn}->{turn + 1} rewrote history (replay={replay})"
            )
            assert len(newer) == len(older) + 1
