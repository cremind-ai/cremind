"""Channel primitives behind direct sends: strict sending and phone identity.

``send_strict`` exists because ``send`` deliberately swallows transport errors
— right for a reply stream, wrong for a per-recipient report that decides
whether a message enters someone's conversation history. And WhatsApp is the
one platform whose sender ids encode a phone number, which is what makes "here
is a spreadsheet of numbers" resolvable at all; the ``@lid`` adoption path keeps
that from splitting one human into two contacts.
"""

from __future__ import annotations

import asyncio

import pytest

from app.channels.base import BaseChannelAdapter, PartialSendError


class _Adapter(BaseChannelAdapter):
    def __init__(self, channel, storage=None, *, fail_on: int | None = None):
        super().__init__(channel, storage or object())
        self.sent: list[tuple[str, str]] = []
        self._fail_on = fail_on

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        if self._fail_on is not None and len(self.sent) == self._fail_on:
            raise RuntimeError("transport died")
        self.sent.append((sender_id, text))


def _channel():
    return {"id": "ch1", "profile": "p1", "channel_type": "whatsapp", "mode": "bot"}


def test_send_swallows_but_send_strict_raises():
    """The two send paths differ exactly where it matters: error visibility."""
    a = _Adapter(_channel(), fail_on=0)
    asyncio.run(a.send("u1", "hi"))          # swallowed, no exception
    with pytest.raises(RuntimeError, match="transport died"):
        asyncio.run(a.send_strict("u1", "hi"))


def test_send_strict_returns_chunk_count():
    a = _Adapter(_channel())
    assert asyncio.run(a.send_strict("u1", "hi")) == 1
    assert a.sent == [("u1", "hi")]


def test_send_strict_ignores_empty_text():
    a = _Adapter(_channel())
    assert asyncio.run(a.send_strict("u1", "   ")) == 0
    assert a.sent == []


def test_send_strict_chunks_long_text():
    a = _Adapter(_channel())
    count = asyncio.run(a.send_strict("u1", "x" * 9000))
    assert count > 1 and len(a.sent) == count


def test_partial_failure_reports_what_was_delivered():
    """Failing on a later chunk is not the same as delivering nothing."""
    a = _Adapter(_channel(), fail_on=1)   # first chunk lands, second dies
    with pytest.raises(PartialSendError) as exc:
        asyncio.run(a.send_strict("u1", "x" * 9000))
    assert exc.value.sent_chunks == 1
    assert len(a.sent) == 1


def test_first_chunk_failure_raises_the_original_error():
    a = _Adapter(_channel(), fail_on=0)
    with pytest.raises(RuntimeError):
        asyncio.run(a.send_strict("u1", "x" * 9000))


def test_base_adapter_derives_no_phone():
    """Most platforms' sender ids say nothing about the person's number."""
    a = _Adapter(_channel())
    assert a._derive_phone("123456789") is None
    assert a._derive_phone("U01ABCDEF") is None


# ── WhatsApp identity ──────────────────────────────────────────────────────


def _wa_adapter():
    from app.channels.adapters.whatsapp import WhatsappAdapter

    return WhatsappAdapter.__new__(WhatsappAdapter)


@pytest.mark.parametrize(
    "sender_id,expected",
    [
        ("84901234567@s.whatsapp.net", "84901234567"),
        ("123456@lid", None),               # opaque alias, not phone-derived
        ("84901234567", None),              # bare id, not a JID
        ("notdigits@s.whatsapp.net", None),
        ("", None),
    ],
)
def test_whatsapp_derives_phone_from_pn_jids_only(sender_id, expected):
    assert _wa_adapter()._derive_phone(sender_id) == expected


class _LidStorage:
    """Storage stand-in tracking the one row a cold send created."""

    def __init__(self, row):
        self.row = row
        self.updates: list[dict] = []

    async def get_sender_by_wa_lid(self, channel_id, wa_lid):
        return dict(self.row) if self.row.get("wa_lid") == wa_lid else None

    async def update_sender(self, row_id, **fields):
        self.updates.append(fields)
        self.row.update(fields)
        return dict(self.row)

    async def get_or_create_sender(self, channel_id, sender_id, display_name=None, **kw):
        return {"id": "new", "channel_id": channel_id, "sender_id": sender_id,
                "display_name": display_name, "conversation_id": None}


def _wa_with_storage(storage):
    from app.channels.adapters.whatsapp import WhatsappAdapter

    adapter = WhatsappAdapter.__new__(WhatsappAdapter)
    adapter.channel = {"id": "ch1", "profile": "p1", "channel_type": "whatsapp"}
    adapter.storage = storage
    return adapter


def test_lid_reply_adopts_the_cold_contact_row():
    """The same human replying from their @lid must not become a second contact.

    ``wa_lid`` is seeded in the exact form a cold send stores it — the full
    ``<id>@lid`` JID — because the lookup is an exact match: seeding a bare id
    here would let a suffix mismatch pass unnoticed while the real adoption
    never fires.
    """
    row = {"id": "r1", "channel_id": "ch1",
           "sender_id": "84901234567@s.whatsapp.net",
           "phone": "84901234567", "wa_lid": "9988@lid", "conversation_id": "conv1"}
    storage = _LidStorage(row)
    adapter = _wa_with_storage(storage)

    out = asyncio.run(adapter._upsert_sender("9988@lid", "Lee"))

    # The existing row is re-pointed at the identity they actually write from,
    # keeping their conversation and phone rather than forking a new contact.
    assert out["id"] == "r1"
    assert out["sender_id"] == "9988@lid"
    assert out["conversation_id"] == "conv1"
    assert out["phone"] == "84901234567"
    assert storage.updates[0]["sender_id"] == "9988@lid"


def test_lid_lookup_uses_the_same_form_that_was_stored():
    """Guards the write/read shapes against drifting apart.

    A cold send stores whatever ``resolve_phone`` returned; adoption looks the
    alias up by exact match. If one side carried the ``@lid`` suffix and the
    other didn't, adoption would silently never fire and every reply would fork
    a second contact — so pin the key the lookup actually asks for.
    """
    asked: list[str] = []

    class _Recorder(_LidStorage):
        async def get_sender_by_wa_lid(self, channel_id, wa_lid):
            asked.append(wa_lid)
            return await super().get_sender_by_wa_lid(channel_id, wa_lid)

    storage = _Recorder({"id": "r1", "wa_lid": "9988@lid", "sender_id": "old"})
    asyncio.run(_wa_with_storage(storage)._upsert_sender("9988@lid", "Lee"))
    assert asked == ["9988@lid"]


def test_resolve_phone_normalizes_a_bare_lid_to_full_jid():
    """Baileys has returned the alias both ways; only one shape may be stored."""
    from app.channels.adapters.whatsapp import _normalize_lid

    assert _normalize_lid("9988") == "9988@lid"
    assert _normalize_lid("9988@lid") == "9988@lid"
    assert _normalize_lid(None) is None
    assert _normalize_lid("") is None


def test_unknown_lid_creates_a_normal_row():
    storage = _LidStorage({"id": "r1", "wa_lid": "other@lid", "sender_id": "x"})
    adapter = _wa_with_storage(storage)
    out = asyncio.run(adapter._upsert_sender("9988@lid", "Lee"))
    assert out["id"] == "new" and out["sender_id"] == "9988@lid"
    assert storage.updates == []
