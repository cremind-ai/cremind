"""Unit tests for the direct-send service (:mod:`app.channels.direct_send`).

Two properties matter most here and drive most of these tests:

1. **Nothing is guessed.** Resolution is exact, so an ambiguous recipient must
   come back as an error rather than a delivered message to the wrong human.
2. **History matches reality.** A message is written to a client's conversation
   only when the transport confirmed it, so the agent's picture of what was
   said is never fiction.

Adapters and storage are faked; what is exercised is the resolution ladder, the
per-recipient isolation, and the register→send→persist ordering.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.channels import direct_send as ds
from app.channels.base import PartialSendError


class _FakeAdapter:
    """Stands in for a live channel adapter."""

    def __init__(
        self, channel_type: str, channel_id: str, *, profile: str = "p1",
        fail: Exception | None = None, chunks: int = 1,
        wa_exists: bool = True, wa_lid: str | None = None,
    ) -> None:
        self.channel_type = channel_type
        self.channel_id = channel_id
        self.profile = profile
        self._fail = fail
        self._chunks = chunks
        self._wa_exists = wa_exists
        self._wa_lid = wa_lid
        self.sent: list[tuple[str, str]] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def _inbound_lock(self, sender_id: str) -> asyncio.Lock:
        return self._locks.setdefault(sender_id, asyncio.Lock())

    async def send_strict(self, sender_id: str, text: str) -> int:
        if self._fail is not None:
            raise self._fail
        self.sent.append((sender_id, text))
        return self._chunks

    async def resolve_phone(self, phone: str) -> dict:
        return {
            "exists": self._wa_exists,
            "jid": f"{phone}@s.whatsapp.net",
            "lid": self._wa_lid,
        }


class _PartialFailAdapter(_FakeAdapter):
    """Delivers the first chunk, then dies — the partial-delivery case."""

    async def send_strict(self, sender_id: str, text: str) -> int:
        self.sent.append((sender_id, text))
        raise PartialSendError(1, RuntimeError("socket died mid-message"))


class _FakeStorage:
    """Faithful stand-in for ConversationStorage.

    Every read hands back a **detached copy** and every write goes to the
    internal row, exactly like the real storage (``_sender_to_dict`` copies;
    ``update_sender`` writes through to the DB, not to the caller's dict). That
    fidelity matters: an earlier version of this fake mutated the caller's dict
    in place, which hid a real bug where a stale snapshot row caused a second
    conversation to be created for the same contact.
    """

    def __init__(self, senders: dict[str, list[dict]] | None = None) -> None:
        self._senders = senders or {}
        self.messages: list[dict] = []
        self.conversations: dict[str, dict] = {}

    async def list_senders(self, channel_id: str) -> list[dict]:
        return [dict(s) for s in self._senders.get(channel_id, [])]

    async def get_or_create_sender(
        self, channel_id: str, sender_id: str, display_name=None,
        phone=None, wa_lid=None,
    ) -> dict:
        rows = self._senders.setdefault(channel_id, [])
        row = next((s for s in rows if s["sender_id"] == sender_id), None)
        if row is None:
            row = {
                "id": str(uuid.uuid4()), "channel_id": channel_id,
                "sender_id": sender_id, "display_name": display_name,
                "phone": phone, "wa_lid": wa_lid, "authenticated": False,
                "conversation_id": None, "created_at": 0, "updated_at": 0,
            }
            rows.append(row)
        else:
            if display_name and not row.get("display_name"):
                row["display_name"] = display_name
            # fill-if-empty, mirroring ConversationStorage
            if phone and not row.get("phone"):
                row["phone"] = phone
            if wa_lid and not row.get("wa_lid"):
                row["wa_lid"] = wa_lid
        return dict(row)

    async def ensure_sender_conversation(
        self, sender: dict, profile: str, channel_id: str, display_name=None,
    ) -> str:
        # Reads the id off the dict it was handed and writes through to the
        # stored row — deliberately NOT back into ``sender``, matching the real
        # storage. A caller holding a stale dict must not be silently rescued.
        if sender.get("conversation_id"):
            return sender["conversation_id"]
        conv_id = f"conv-{uuid.uuid4().hex[:8]}"
        self.conversations[conv_id] = {"profile": profile, "channel_id": channel_id}
        for row in self._senders.get(channel_id, []):
            if row["sender_id"] == sender["sender_id"]:
                row["conversation_id"] = conv_id
        return conv_id

    async def add_message(self, conversation_id, role, content=None, metadata=None, **kw):
        msg = {
            "id": f"m-{len(self.messages)}", "conversation_id": conversation_id,
            "role": role, "content": content, "metadata": metadata,
            "created_at": time.time(),
        }
        self.messages.append(msg)
        return msg


def _sender(channel_id, sender_id, **kw):
    row = {
        "id": f"row-{sender_id}", "channel_id": channel_id, "sender_id": sender_id,
        "display_name": kw.get("display_name"), "phone": kw.get("phone"),
        "wa_lid": kw.get("wa_lid"), "authenticated": kw.get("authenticated", True),
        "conversation_id": kw.get("conversation_id"),
        # Per-client confirmation override; None = inherit the profile setting.
        "send_confirmation": kw.get("send_confirmation"),
    }
    return row


def _send(**kwargs):
    kwargs.setdefault("dry_run", False)
    return asyncio.run(ds.send_direct_messages(**kwargs))


# ── phone normalization ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+84901234567", "84901234567"),
        ("+84 90 123 4567", "84901234567"),
        ("+84-90-123-4567", "84901234567"),
        ("(84) 901234567", "84901234567"),
        ("0084901234567", "84901234567"),
        ("84901234567", "84901234567"),
        ("", None),
        ("abc", None),
        ("12345", None),          # too short to be a real number
        ("0901234567", None),     # national form: ambiguous without a country
    ],
)
def test_normalize_phone(raw, expected):
    assert ds.normalize_phone(raw) == expected


def test_normalize_phone_national_form_with_country_code():
    assert ds.normalize_phone("0901234567", "84") == "84901234567"
    assert ds.normalize_phone("0901234567", "+84") == "84901234567"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+84901234567", True),
        ("84901234567", True),
        ("84901234567@s.whatsapp.net", False),   # a JID, not a number
        ("U01ABCDEF", False),
        ("", False),
        ("12345", False),
    ],
)
def test_looks_like_phone(raw, expected):
    assert ds.looks_like_phone(raw) is expected


# ── resolution ladder ──────────────────────────────────────────────────────


def _resolve(to, adapters, senders, **kw):
    return asyncio.run(ds.resolve_recipient(to, adapters, senders, **kw))


def test_exact_sender_id_wins_over_phone_interpretation():
    """A known numeric Telegram id must not be read as a phone number."""
    tg = _FakeAdapter("telegram", "c-tg")
    wa = _FakeAdapter("whatsapp", "c-wa")
    senders = {"c-tg": [_sender("c-tg", "84901234567")], "c-wa": []}
    adapter, sender_id, row, info = _resolve("84901234567", [tg, wa], senders)
    assert adapter is tg
    assert sender_id == "84901234567"
    assert row is not None and info["cold"] is False


def test_phone_column_match_resolves_non_whatsapp_contact():
    tg = _FakeAdapter("telegram", "c-tg")
    senders = {"c-tg": [_sender("c-tg", "555000111", phone="84901234567")]}
    adapter, sender_id, row, info = _resolve("+84901234567", [tg], senders)
    assert adapter is tg and sender_id == "555000111"
    assert info["phone"] == "84901234567"


def test_whatsapp_jid_derivation_matches_known_contact():
    wa = _FakeAdapter("whatsapp", "c-wa")
    jid = "84901234567@s.whatsapp.net"
    senders = {"c-wa": [_sender("c-wa", jid)]}
    adapter, sender_id, row, info = _resolve("+84901234567", [wa], senders)
    assert sender_id == jid and row is not None and info["cold"] is False


def test_whatsapp_cold_send_is_existence_checked():
    wa = _FakeAdapter("whatsapp", "c-wa", wa_lid="9988@lid")
    adapter, sender_id, row, info = _resolve("+84901234567", [wa], {"c-wa": []})
    assert sender_id == "84901234567@s.whatsapp.net"
    assert row is None
    assert info["cold"] is True and info["wa_lid"] == "9988@lid"


def test_number_not_on_whatsapp_is_rejected():
    wa = _FakeAdapter("whatsapp", "c-wa", wa_exists=False)
    with pytest.raises(ds.RecipientError) as exc:
        _resolve("+84901234567", [wa], {"c-wa": []})
    assert exc.value.code == "not_on_whatsapp"


def test_phone_on_bot_only_platform_explains_alternatives():
    tg = _FakeAdapter("telegram", "c-tg")
    wa = _FakeAdapter("whatsapp", "c-wa")
    with pytest.raises(ds.RecipientError) as exc:
        _resolve("+84901234567", [tg, wa], {"c-tg": [], "c-wa": []}, channel="telegram")
    assert exc.value.code == "platform_cannot_initiate"
    assert any("whatsapp" in a for a in exc.value.alternatives)


def test_same_phone_on_two_channels_is_ambiguous_not_guessed():
    tg = _FakeAdapter("telegram", "c-tg")
    sl = _FakeAdapter("slack", "c-sl")
    senders = {
        "c-tg": [_sender("c-tg", "111", phone="84901234567")],
        "c-sl": [_sender("c-sl", "U222", phone="84901234567")],
    }
    with pytest.raises(ds.RecipientError) as exc:
        _resolve("+84901234567", [tg, sl], senders)
    assert exc.value.code == "ambiguous_recipient"


def test_channel_filter_disambiguates():
    tg = _FakeAdapter("telegram", "c-tg")
    sl = _FakeAdapter("slack", "c-sl")
    senders = {
        "c-tg": [_sender("c-tg", "111", phone="84901234567")],
        "c-sl": [_sender("c-sl", "U222", phone="84901234567")],
    }
    adapter, sender_id, _row, _info = _resolve(
        "+84901234567", [tg, sl], senders, channel="slack",
    )
    assert adapter is sl and sender_id == "U222"


def test_unknown_identifier_on_multiple_channels_is_ambiguous():
    sl = _FakeAdapter("slack", "c-sl")
    dc = _FakeAdapter("discord", "c-dc")
    with pytest.raises(ds.RecipientError) as exc:
        _resolve("U01ABCDEF", [sl, dc], {"c-sl": [], "c-dc": []})
    assert exc.value.code == "ambiguous_identifier"


def test_unknown_identifier_on_single_channel_is_attempted():
    sl = _FakeAdapter("slack", "c-sl")
    adapter, sender_id, row, info = _resolve("U01ABCDEF", [sl], {"c-sl": []})
    assert adapter is sl and sender_id == "U01ABCDEF"
    assert row is None and info["cold"] is True


def test_national_format_number_without_country_is_reported():
    wa = _FakeAdapter("whatsapp", "c-wa")
    with pytest.raises(ds.RecipientError) as exc:
        _resolve("0901234567", [wa], {"c-wa": []})
    assert exc.value.code == "ambiguous_phone"


# ── recipient normalization ────────────────────────────────────────────────


def test_normalize_recipients_accepts_bare_strings_and_objects():
    out = ds.normalize_recipients(["+8490", {"to": "U1", "message": "hi", "name": "Lee"}])
    assert out[0]["to"] == "+8490"
    assert out[1] == {"to": "U1", "message": "hi", "name": "Lee", "channel": None}


def test_normalize_recipients_rejects_empty_and_bad_shapes():
    for bad in (None, [], [{"name": "no to"}], [42]):
        with pytest.raises(ValueError):
            ds.normalize_recipients(bad)


# ── the send pipeline ──────────────────────────────────────────────────────


def test_dry_run_sends_nothing_and_writes_nothing():
    wa = _FakeAdapter("whatsapp", "c-wa")
    storage = _FakeStorage({"c-wa": []})
    out = asyncio.run(ds.send_direct_messages(
        adapters=[wa], storage=storage,
        recipients=[{"to": "+84901234567"}], message="hi", dry_run=True,
    ))
    assert out["dry_run"] is True
    assert out["results"][0]["status"] == "would_send"
    assert out["results"][0]["new_contact"] is True
    assert wa.sent == [] and storage.messages == []


def test_successful_send_records_history_with_provenance():
    jid = "84901234567@s.whatsapp.net"
    wa = _FakeAdapter("whatsapp", "c-wa")
    storage = _FakeStorage({"c-wa": [_sender("c-wa", jid, conversation_id="conv1")]})
    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": jid}], message="Thanks!",
    )
    assert out["sent"] == 1 and out["failed"] == 0
    assert wa.sent == [(jid, "Thanks!")]
    msg = storage.messages[0]
    assert msg["role"] == "agent"          # maps to "assistant" in model history
    assert msg["content"] == "Thanks!"
    assert msg["conversation_id"] == "conv1"
    assert msg["metadata"]["source"] == "agent_outbound"
    assert msg["metadata"]["sender_id"] == jid
    assert msg["metadata"]["channel_type"] == "whatsapp"
    assert "cold_contact" not in msg["metadata"]
    assert out["results"][0]["message_id"] == msg["id"]


def test_cold_send_registers_contact_unauthenticated_and_records():
    wa = _FakeAdapter("whatsapp", "c-wa", wa_lid="9988@lid")
    storage = _FakeStorage({"c-wa": []})
    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": "+84901234567", "name": "Lee"}], message="Thanks!",
    )
    assert out["sent"] == 1
    row = storage._senders["c-wa"][0]
    assert row["sender_id"] == "84901234567@s.whatsapp.net"
    assert row["phone"] == "84901234567"
    assert row["wa_lid"] == "9988@lid"
    # Reaching out to someone is not a decision to let them command the agent.
    assert row["authenticated"] is False
    assert row["conversation_id"] is not None
    assert storage.messages[0]["metadata"]["cold_contact"] is True
    assert out["results"][0]["new_contact"] is True


def test_failed_send_writes_no_history():
    jid = "84901234567@s.whatsapp.net"
    wa = _FakeAdapter("whatsapp", "c-wa", fail=RuntimeError("no route"))
    storage = _FakeStorage({"c-wa": [_sender("c-wa", jid, conversation_id="conv1")]})
    out = _send(adapters=[wa], storage=storage, recipients=[{"to": jid}], message="hi")
    assert out["sent"] == 0 and out["failed"] == 1
    assert out["results"][0]["error"] == "delivery_failed"
    assert storage.messages == []


def test_partial_delivery_is_recorded_because_the_client_saw_it():
    jid = "84901234567@s.whatsapp.net"
    wa = _PartialFailAdapter("whatsapp", "c-wa")
    storage = _FakeStorage({"c-wa": [_sender("c-wa", jid, conversation_id="conv1")]})
    out = _send(adapters=[wa], storage=storage, recipients=[{"to": jid}], message="long")
    # send_strict recorded a chunk before raising, so the recipient saw part of
    # it — history has to reflect that, flagged as partial.
    assert out["results"][0]["status"] == "failed"
    assert out["results"][0]["error"] == "partial_delivery"
    assert storage.messages[0]["metadata"]["partial_failure"] is True


def test_one_bad_recipient_does_not_abort_the_others(monkeypatch):
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa = _FakeAdapter("whatsapp", "c-wa")
    good1 = "84900000001@s.whatsapp.net"
    good2 = "84900000002@s.whatsapp.net"
    storage = _FakeStorage({"c-wa": [
        _sender("c-wa", good1, conversation_id="conv1"),
        _sender("c-wa", good2, conversation_id="conv2"),
    ]})
    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": good1}, {"to": "0901234567"}, {"to": good2}],
        message="hi",
    )
    assert out["sent"] == 2 and out["failed"] == 1
    assert [r["status"] for r in out["results"]] == ["sent", "failed", "sent"]
    assert len(storage.messages) == 2


def test_repeated_recipient_without_a_conversation_gets_only_one(monkeypatch):
    """One contact must end up with one conversation, however often they appear.

    Notification-mode subscribers have ``conversation_id = NULL`` (their
    subscribe path never creates one). Listing such a contact twice — a
    duplicated spreadsheet row — must not create a second conversation and
    strand the first message somewhere no sender points at.
    """
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa = _FakeAdapter("whatsapp", "c-wa")
    jid = "84901234567@s.whatsapp.net"
    storage = _FakeStorage({"c-wa": [_sender("c-wa", jid, conversation_id=None)]})

    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": jid, "message": "first"}, {"to": jid, "message": "second"}],
    )

    assert out["sent"] == 2
    assert len(storage.conversations) == 1
    conv_id = next(iter(storage.conversations))
    assert [m["conversation_id"] for m in storage.messages] == [conv_id, conv_id]
    # And the contact still points at the conversation holding both messages.
    assert storage._senders["c-wa"][0]["conversation_id"] == conv_id


def test_duplicate_cold_recipient_still_lands_in_one_contact(monkeypatch):
    """The same new number listed twice must not fork into two contacts.

    Recipients are all resolved up front (so the confirmation policy sees one
    consistent picture), which means both entries report ``new_contact`` — that
    flag describes the state at *resolution* time, and neither had been
    contacted then. What must still hold is the important part: the send path
    deduplicates, so one person ends up with one client record and one
    conversation holding both messages.
    """
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa = _FakeAdapter("whatsapp", "c-wa", wa_lid="9988@lid")
    storage = _FakeStorage({"c-wa": []})

    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": "+84901234567"}, {"to": "+84901234567"}],
        message="hi",
    )

    assert out["sent"] == 2
    assert len(storage._senders["c-wa"]) == 1
    assert len(storage.conversations) == 1
    assert len(storage.messages) == 2
    assert {m["conversation_id"] for m in storage.messages} == set(storage.conversations)
    assert [r["new_contact"] for r in out["results"]] == [True, True]


def test_per_recipient_message_overrides_shared(monkeypatch):
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa = _FakeAdapter("whatsapp", "c-wa")
    a = "84900000001@s.whatsapp.net"
    b = "84900000002@s.whatsapp.net"
    storage = _FakeStorage({"c-wa": [
        _sender("c-wa", a, conversation_id="c1"), _sender("c-wa", b, conversation_id="c2"),
    ]})
    _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": a, "message": "Hi Lee!"}, {"to": b}],
        message="Hi there",
    )
    assert wa.sent == [(a, "Hi Lee!"), (b, "Hi there")]


def test_missing_text_for_a_recipient_is_rejected_up_front():
    wa = _FakeAdapter("whatsapp", "c-wa")
    storage = _FakeStorage({"c-wa": []})
    with pytest.raises(ValueError, match="No message text"):
        _send(adapters=[wa], storage=storage, recipients=[{"to": "U1"}], message=None)


def test_recipient_cap_is_enforced():
    wa = _FakeAdapter("whatsapp", "c-wa")
    storage = _FakeStorage({"c-wa": []})
    too_many = [{"to": f"+8490000{i:04d}"} for i in range(ds.MAX_RECIPIENTS + 1)]
    with pytest.raises(ValueError, match="exceeds"):
        _send(adapters=[wa], storage=storage, recipients=too_many, message="hi")


def test_partial_failure_counts_as_a_circuit_breaker_failure(monkeypatch):
    """A run of partial deliveries is still a failing channel — abort it."""
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa = _PartialFailAdapter("whatsapp", "c-wa")
    jids = [f"8490000000{i}@s.whatsapp.net" for i in range(7)]
    storage = _FakeStorage({"c-wa": [
        _sender("c-wa", j, conversation_id=f"c{i}") for i, j in enumerate(jids)
    ]})
    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": j} for j in jids], message="hi",
    )
    assert out["aborted"] is True
    assert out["sent"] == 0


# ── the configurable confirmation gate ─────────────────────────────────────
#
# `confirm_policy` is how the agent-facing tool opts into the profile/per-client
# setting. Callers that leave it None (REST, CLI) must keep today's behaviour
# exactly, which is what the rest of this file already covers.


def _wa_with(jid="84901234567@s.whatsapp.net", **kw):
    wa = _FakeAdapter("whatsapp", "c-wa")
    storage = _FakeStorage({"c-wa": [_sender("c-wa", jid, conversation_id="c1", **kw)]})
    return wa, storage, jid


def test_policy_holds_the_send_and_reports_who_needs_approval(monkeypatch):
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa, storage, jid = _wa_with()
    out = _send(
        adapters=[wa], storage=storage, recipients=[{"to": jid}], message="hi",
        confirm_policy=lambda sender, cold: True,
    )
    assert out["sent"] == 0
    assert wa.sent == [] and storage.messages == []
    assert [r["to"] for r in out["needs_confirmation"]] == [jid]
    assert "approval" in out["message"]
    # Not a dry run — the caller asked to send, the policy held it.
    assert out["dry_run"] is False


def test_policy_that_exempts_everyone_sends_on_the_first_call(monkeypatch):
    """The automation case: no preview round trip at all."""
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa, storage, jid = _wa_with()
    out = _send(
        adapters=[wa], storage=storage, recipients=[{"to": jid}], message="hi",
        confirm_policy=lambda sender, cold: False,
    )
    assert out["sent"] == 1
    assert wa.sent == [(jid, "hi")]
    assert "needs_confirmation" not in out


def test_confirm_true_bypasses_the_policy(monkeypatch):
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa, storage, jid = _wa_with()
    out = _send(
        adapters=[wa], storage=storage, recipients=[{"to": jid}], message="hi",
        confirm=True, confirm_policy=lambda sender, cold: True,
    )
    assert out["sent"] == 1 and wa.sent == [(jid, "hi")]


def test_one_unconfirmed_recipient_holds_the_whole_batch(monkeypatch):
    """All-or-nothing: no partial delivery before the user has seen the list."""
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa = _FakeAdapter("whatsapp", "c-wa")
    ok = "84900000001@s.whatsapp.net"
    ask = "84900000002@s.whatsapp.net"
    storage = _FakeStorage({"c-wa": [
        _sender("c-wa", ok, conversation_id="c1", send_confirmation="skip"),
        _sender("c-wa", ask, conversation_id="c2"),
    ]})

    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": ok}, {"to": ask}], message="hi",
        confirm_policy=lambda sender, cold: (sender or {}).get("send_confirmation") != "skip",
    )

    assert out["sent"] == 0
    assert wa.sent == [] and storage.messages == []
    assert [r["to"] for r in out["needs_confirmation"]] == [ask]
    # Both are still reported, so the user sees the whole list they're approving.
    assert [r["to"] for r in out["results"]] == [ok, ask]


def test_the_policy_sees_the_resolved_client_row(monkeypatch):
    """Per-client overrides are only knowable after resolution — check they arrive."""
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa, storage, jid = _wa_with(send_confirmation="skip")
    seen: list[tuple] = []

    def _policy(sender, cold):
        seen.append(((sender or {}).get("send_confirmation"), cold))
        return False

    _send(
        adapters=[wa], storage=storage, recipients=[{"to": jid}], message="hi",
        confirm_policy=_policy,
    )
    assert seen == [("skip", False)]


def test_cold_recipients_are_flagged_to_the_policy(monkeypatch):
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa = _FakeAdapter("whatsapp", "c-wa", wa_lid="9988@lid")
    storage = _FakeStorage({"c-wa": []})
    seen: list[bool] = []

    out = _send(
        adapters=[wa], storage=storage, recipients=[{"to": "+84901234567"}],
        message="hi",
        confirm_policy=lambda sender, cold: (seen.append(cold), cold)[1],
    )
    assert seen == [True]
    assert out["sent"] == 0 and "needs_confirmation" in out


def test_a_failing_policy_holds_the_send(monkeypatch):
    """An un-evaluable policy must mean "ask", never "send anyway"."""
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa, storage, jid = _wa_with()

    def _boom(sender, cold):
        raise RuntimeError("policy lookup failed")

    out = _send(
        adapters=[wa], storage=storage, recipients=[{"to": jid}], message="hi",
        confirm_policy=_boom,
    )
    assert out["sent"] == 0 and wa.sent == []
    assert [r["to"] for r in out["needs_confirmation"]] == [jid]


def test_explicit_dry_run_still_previews_under_a_permissive_policy(monkeypatch):
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa, storage, jid = _wa_with()
    out = asyncio.run(ds.send_direct_messages(
        adapters=[wa], storage=storage, recipients=[{"to": jid}], message="hi",
        dry_run=True, confirm_policy=lambda sender, cold: False,
    ))
    assert out["dry_run"] is True and out["sent"] == 0
    assert wa.sent == []
    assert "needs_confirmation" not in out
    # The old two-step wording must not leak into a plain preview any more.
    assert "confirm=true" not in out["message"]


def test_unresolvable_recipients_are_not_offered_for_confirmation(monkeypatch):
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa, storage, jid = _wa_with()
    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": jid}, {"to": "0901234567"}], message="hi",
        confirm_policy=lambda sender, cold: False,
    )
    # The good one sent; the bad one is a failure, never a confirmation prompt.
    assert out["sent"] == 1 and out["failed"] == 1
    assert "needs_confirmation" not in out


def test_circuit_breaker_stops_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(ds, "_delay_for", lambda adapter: 0.0)
    wa = _FakeAdapter("whatsapp", "c-wa", fail=RuntimeError("socket dead"))
    jids = [f"8490000000{i}@s.whatsapp.net" for i in range(8)]
    storage = _FakeStorage({"c-wa": [
        _sender("c-wa", j, conversation_id=f"c{i}") for i, j in enumerate(jids)
    ]})
    out = _send(
        adapters=[wa], storage=storage,
        recipients=[{"to": j} for j in jids], message="hi",
    )
    assert out["aborted"] is True
    statuses = [r["status"] for r in out["results"]]
    assert statuses.count("failed") == ds.CIRCUIT_BREAKER_FAILURES
    assert statuses.count("skipped") == len(jids) - ds.CIRCUIT_BREAKER_FAILURES
