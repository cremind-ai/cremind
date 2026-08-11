"""Unit tests for the ``send_channel_message`` built-in tool's ``run()``.

The tool messages individual channel clients. Its own job is thin — validate,
enforce the confirmation flow, resolve live adapters, delegate to
``direct_send`` — so these tests concentrate on the guard rails: an omitted
``dry_run`` must never send, a live send must require explicit confirmation, and
every failure must surface as a structured observation rather than an exception.
"""

from __future__ import annotations

import asyncio

import app.channels.registry as reg
from app.tools.builtin.send_channel_message import SendChannelMessageTool


class _FakeAdapter:
    def __init__(self, channel_type, channel_id, *, profile="p") -> None:
        self.channel_type = channel_type
        self.channel_id = channel_id
        self.profile = profile
        self.sent: list[tuple[str, str]] = []
        self._locks: dict = {}

    def _inbound_lock(self, sender_id):
        import asyncio as _a
        return self._locks.setdefault(sender_id, _a.Lock())

    async def send_strict(self, sender_id, text):
        self.sent.append((sender_id, text))
        return 1

    async def resolve_phone(self, phone):
        return {"exists": True, "jid": f"{phone}@s.whatsapp.net", "lid": None}


class _FakeStorage:
    def __init__(self, senders=None) -> None:
        self._senders = senders or {}
        self.messages: list[dict] = []

    async def list_senders(self, channel_id):
        return list(self._senders.get(channel_id, []))

    async def get_or_create_sender(self, channel_id, sender_id, display_name=None,
                                   phone=None, wa_lid=None):
        rows = self._senders.setdefault(channel_id, [])
        row = next((s for s in rows if s["sender_id"] == sender_id), None)
        if row is None:
            row = {
                "id": f"row-{sender_id}", "channel_id": channel_id,
                "sender_id": sender_id, "display_name": display_name,
                "phone": phone, "wa_lid": wa_lid, "authenticated": False,
                "conversation_id": None,
            }
            rows.append(row)
        return dict(row)

    async def ensure_sender_conversation(self, sender, profile, channel_id, display_name=None):
        return sender.get("conversation_id") or f"conv-{sender['sender_id']}"

    async def add_message(self, conversation_id, role, content=None, metadata=None, **kw):
        msg = {"id": f"m{len(self.messages)}", "conversation_id": conversation_id,
               "role": role, "content": content, "metadata": metadata}
        self.messages.append(msg)
        return msg


class _FakeRegistry:
    def __init__(self, adapters, storage=None) -> None:
        self._adapters = list(adapters)
        self.storage = storage or _FakeStorage()

    def adapters_for_profile(self, profile):
        return list(self._adapters)


def _run(arguments, *, registry=None, raise_runtime=False):
    def _get_registry(*_a, **_k):
        if raise_runtime:
            raise RuntimeError("not initialized")
        return registry

    backup = reg.get_channel_registry
    reg.get_channel_registry = _get_registry
    try:
        tool = SendChannelMessageTool()
        return asyncio.run(tool.run(arguments)).structured_content
    finally:
        reg.get_channel_registry = backup


def _known(sender_id, **kw):
    """A client who has messaged before — i.e. one that can carry an override."""
    return {
        "id": f"row-{sender_id}", "channel_id": "c-wa", "sender_id": sender_id,
        "display_name": kw.get("display_name", "Lee"),
        "phone": kw.get("phone", "84901234567"), "wa_lid": None,
        "authenticated": True, "conversation_id": kw.get("conversation_id", "conv1"),
        "send_confirmation": kw.get("send_confirmation"),
    }


def _wa_registry(senders=None):
    adapter = _FakeAdapter("whatsapp", "c-wa")
    storage = _FakeStorage(senders if senders is not None else {"c-wa": []})
    return _FakeRegistry([adapter], storage), adapter, storage


def test_missing_profile_is_structured_error():
    out = _run({"recipients": [{"to": "+84901234567"}], "message": "hi"})
    assert out["error"] == "NoProfile"


def test_missing_recipients_is_structured_error():
    registry, _a, _s = _wa_registry()
    out = _run({"message": "hi", "_profile": "p"}, registry=registry)
    assert out["error"] == "InvalidArgument"


def test_registry_uninitialized_reports_channels_unavailable():
    out = _run(
        {"recipients": [{"to": "U1"}], "message": "hi", "_profile": "p"},
        raise_runtime=True,
    )
    assert out["error"] == "ChannelsUnavailable"


def test_no_channel_is_structured_error():
    out = _run(
        {"recipients": [{"to": "U1"}], "message": "hi", "_profile": "p"},
        registry=_FakeRegistry([]),
    )
    assert out["error"] == "NoChannel"


def test_unknown_channel_lists_available():
    registry, adapter, _s = _wa_registry()
    out = _run(
        {"recipients": [{"to": "U1"}], "message": "hi", "_profile": "p",
         "channel": "discord"},
        registry=registry,
    )
    assert out["error"] == "UnknownChannel"
    assert out["available_channels"] == ["whatsapp"]
    assert adapter.sent == []


def test_a_cold_recipient_is_held_for_confirmation():
    """Default profile setting is ON, and a stranger always confirms anyway."""
    registry, adapter, storage = _wa_registry()
    out = _run(
        {"recipients": [{"to": "+84901234567"}], "message": "hi", "_profile": "p"},
        registry=registry,
    )
    assert out["sent"] == 0
    assert out["results"][0]["status"] == "would_send"
    assert [r["to"] for r in out["needs_confirmation"]] == ["+84901234567"]
    assert adapter.sent == [] and storage.messages == []


def test_known_client_is_held_while_the_profile_setting_is_on():
    jid = "84901234567@s.whatsapp.net"
    registry, adapter, storage = _wa_registry(
        {"c-wa": [_known(jid)]},
    )
    out = _run(
        {"recipients": [{"to": jid}], "message": "hi", "_profile": "p"},
        registry=registry,
    )
    assert out["sent"] == 0
    assert out["confirmation_required_by_default"] is True
    assert adapter.sent == []


def test_profile_setting_off_sends_a_known_client_on_the_first_call(monkeypatch):
    """The complaint: one call, no preview, so an automation can complete."""
    import app.channels.send_policy as sp

    monkeypatch.setattr(sp, "confirm_before_send_default", lambda profile: False)
    jid = "84901234567@s.whatsapp.net"
    registry, adapter, storage = _wa_registry({"c-wa": [_known(jid)]})
    out = _run(
        {"recipients": [{"to": jid}], "message": "hi", "_profile": "p"},
        registry=registry,
    )
    assert out["sent"] == 1
    assert adapter.sent == [(jid, "hi")]
    assert out["confirmation_required_by_default"] is False
    assert "needs_confirmation" not in out


def test_per_client_skip_sends_even_though_the_profile_asks():
    """A single exempted client, global default untouched."""
    jid = "84901234567@s.whatsapp.net"
    registry, adapter, storage = _wa_registry(
        {"c-wa": [_known(jid, send_confirmation="skip")]},
    )
    out = _run(
        {"recipients": [{"to": jid}], "message": "hi", "_profile": "p"},
        registry=registry,
    )
    assert out["sent"] == 1 and adapter.sent == [(jid, "hi")]


def test_per_client_required_still_asks_with_the_setting_off(monkeypatch):
    import app.channels.send_policy as sp

    monkeypatch.setattr(sp, "confirm_before_send_default", lambda profile: False)
    jid = "84901234567@s.whatsapp.net"
    registry, adapter, storage = _wa_registry(
        {"c-wa": [_known(jid, send_confirmation="required")]},
    )
    out = _run(
        {"recipients": [{"to": jid}], "message": "hi", "_profile": "p"},
        registry=registry,
    )
    assert out["sent"] == 0 and adapter.sent == []
    assert [r["to"] for r in out["needs_confirmation"]] == [jid]


def test_cold_recipient_still_asks_with_the_setting_off(monkeypatch):
    """Turning confirmation off must not enable silent cold outreach."""
    import app.channels.send_policy as sp

    monkeypatch.setattr(sp, "confirm_before_send_default", lambda profile: False)
    registry, adapter, storage = _wa_registry()
    out = _run(
        {"recipients": [{"to": "+84901234567"}], "message": "hi", "_profile": "p"},
        registry=registry,
    )
    assert out["sent"] == 0 and adapter.sent == []
    assert "never messaged" in out["needs_confirmation"][0]["reason"]


def test_explicit_dry_run_previews_even_when_nothing_needs_confirming(monkeypatch):
    import app.channels.send_policy as sp

    monkeypatch.setattr(sp, "confirm_before_send_default", lambda profile: False)
    jid = "84901234567@s.whatsapp.net"
    registry, adapter, storage = _wa_registry({"c-wa": [_known(jid)]})
    out = _run(
        {"recipients": [{"to": jid}], "message": "hi", "_profile": "p",
         "dry_run": True},
        registry=registry,
    )
    assert out["dry_run"] is True and out["sent"] == 0 and adapter.sent == []


def test_a_stringy_false_confirm_is_not_approval():
    """Tool arguments are model output, and bool("false") is True.

    Reading the approval flag with bare truthiness would hand out an approval
    nobody gave, so it is parsed strictly — this is the one flag where a
    misreading delivers messages instead of holding them.
    """
    jid = "84901234567@s.whatsapp.net"
    for value in ("false", "False", "no", "0", "", None, 0):
        registry, adapter, _s = _wa_registry({"c-wa": [_known(jid)]})
        out = _run(
            {"recipients": [{"to": jid}], "message": "hi", "_profile": "p",
             "confirm": value},
            registry=registry,
        )
        assert out["sent"] == 0, f"confirm={value!r} was treated as approval"
        assert adapter.sent == []


def test_affirmative_confirm_values_do_approve():
    jid = "84901234567@s.whatsapp.net"
    for value in (True, "true", "TRUE", " yes ", "1", "on"):
        registry, adapter, _s = _wa_registry({"c-wa": [_known(jid)]})
        out = _run(
            {"recipients": [{"to": jid}], "message": "hi", "_profile": "p",
             "confirm": value},
            registry=registry,
        )
        assert out["sent"] == 1, f"confirm={value!r} should approve"


def test_confirm_on_description_warns_that_exempt_clients_send_immediately():
    """Otherwise the model previews, sees a send, then confirms — delivering twice."""
    import app.tools.builtin.send_channel_message as mod

    text = mod.SendChannelMessageTool.description
    assert "send directly" in text            # names the per-client exemption
    assert "second" in text                   # warns about the double delivery
    assert "do NOT" in text and "confirm=true" in text
    # It must not promise the old unconditional "nothing is sent".
    assert "nothing is sent" not in text


def test_the_description_matches_the_profile_setting(monkeypatch):
    """prepare_tools shapes the contract the model reads — not self.description."""
    import app.channels.send_policy as sp
    import app.tools.builtin.send_channel_message as mod

    prepare = mod.get_prepare_tools()
    spec = [{"function": {"name": "send_channel_message", "description": "x"}}]

    monkeypatch.setattr(sp, "confirm_before_send_default", lambda profile: True)
    out = prepare("q", [dict(f) for f in spec], profile="p")
    assert "GET APPROVAL FIRST" in out[0]["function"]["description"]

    monkeypatch.setattr(sp, "confirm_before_send_default", lambda profile: False)
    out = prepare("q", [dict(f) for f in spec], profile="p")
    assert "SEND DIRECTLY" in out[0]["function"]["description"]
    assert "GET APPROVAL FIRST" not in out[0]["function"]["description"]

    # The shared singleton's own attribute must never be rewritten.
    assert "GET APPROVAL FIRST" in mod.SendChannelMessageTool.description


def test_prepare_tools_is_a_noop_without_a_profile():
    import app.tools.builtin.send_channel_message as mod

    prepare = mod.get_prepare_tools()
    spec = [{"function": {"name": "send_channel_message", "description": "keep"}}]
    assert prepare("q", spec, profile=None)[0]["function"]["description"] == "keep"


def test_confirmed_live_send_delivers_and_records():
    jid = "84901234567@s.whatsapp.net"
    registry, adapter, storage = _wa_registry(
        {"c-wa": [{"id": "r1", "channel_id": "c-wa", "sender_id": jid,
                   "display_name": "Lee", "phone": "84901234567", "wa_lid": None,
                   "authenticated": True, "conversation_id": "conv1"}]}
    )
    out = _run(
        {"recipients": [{"to": jid}], "message": "Thanks!", "_profile": "p",
         "dry_run": False, "confirm": True},
        registry=registry,
    )
    assert out["sent"] == 1 and out["failed"] == 0
    assert adapter.sent == [(jid, "Thanks!")]
    assert storage.messages[0]["role"] == "agent"
    assert storage.messages[0]["metadata"]["initiated_by"] == "send_channel_message"
    assert "note" in out  # the unauthenticated-new-contact caveat


def test_bare_string_recipients_are_coerced():
    registry, _adapter, _storage = _wa_registry()
    out = _run(
        {"recipients": ["+84901234567"], "message": "hi", "_profile": "p"},
        registry=registry,
    )
    assert out["results"][0]["to"] == "+84901234567"


def test_recipient_cap_is_a_structured_error():
    registry, adapter, _s = _wa_registry()
    out = _run(
        {"recipients": [{"to": f"+8490000{i:04d}"} for i in range(101)],
         "message": "hi", "_profile": "p"},
        registry=registry,
    )
    assert out["error"] == "InvalidArgument"
    assert "100" in out["message"]
    assert adapter.sent == []


def test_unresolvable_recipient_is_reported_not_raised():
    registry, adapter, _s = _wa_registry()
    out = _run(
        {"recipients": [{"to": "0901234567"}], "message": "hi", "_profile": "p"},
        registry=registry,
    )
    # National-format number with no country: reported per-recipient, and the
    # call as a whole still succeeds.
    assert "error" not in out
    assert out["results"][0]["error"] == "ambiguous_phone"
    assert adapter.sent == []
