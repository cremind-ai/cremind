"""The engine behind ``send_files_to_chat``: which chat, which files, and what
became of each one.

Three questions, each with its own section, because each fails differently in
front of real people. **Where**: the chat is read off the conversation the turn
runs in — never from the model — and every way that chat can be unreachable is
refused before a byte moves. **What**: every file is checked before any is sent,
so a batch starts whole or not at all. **How it went**: files go one at a time,
each outcome is reported, a dead chat stops the batch, the clock never cuts the
report off, and nothing is posted twice in one turn.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.storage as storage_mod
from app.channels import chat_files
from app.channels.chat_files import (
    ALREADY_SENT,
    FAILED,
    KIND_GROUP,
    KIND_PRIVATE,
    NOT_SENT,
    SENT,
    UNCONFIRMED,
    ChatFilesError,
)
from app.channels.exceptions import ChannelNotImplemented, DeliveryUnconfirmed
from app.config import working_dirs as wd

cfg = importlib.import_module("app.config.settings")

_CHAT_ID = "-1001234"


# ── fakes ──────────────────────────────────────────────────────────────────


class _Adapter:
    """What chat_files asks of a live adapter; records every file it sends.

    ``fail`` scripts the outcome of each send in order (``None`` = success).
    """

    supports_file_send = True
    supports_group_chats = True
    max_file_send_bytes = None

    def __init__(self, *, channel_id="ch-tg", channel_type="telegram", profile="p",
                 groups_on=True, auth="open", fail=None, delay=0.0):
        self.channel_id = channel_id
        self.channel_type = channel_type
        self.profile = profile
        self._groups_on = groups_on
        self._auth = auth
        self.fail = list(fail or [])
        self.delay = delay
        self.sent: list[tuple[str, str, str]] = []
        self.attempts = 0

    def groups_enabled(self):
        return self._groups_on

    def _subscribe_auth(self):
        return self._auth

    async def _deliver(self, kind, address, path, name):
        self.attempts += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.fail.pop(0) if self.fail else None
        if outcome is not None:
            raise outcome
        assert name == os.path.basename(path)
        self.sent.append((kind, address, name))

    async def send_file_strict(self, sender_id, path, *, name=None, mime=None, caption=None):
        await self._deliver("dm", sender_id, path, name)

    async def send_file_to_chat_strict(self, chat_id, path, *, name=None, mime=None,
                                       caption=None):
        await self._deliver("room", chat_id, path, name)


class _NoFiles(_Adapter):
    supports_file_send = False


class _SmallCap(_Adapter):
    max_file_send_bytes = 10


class _ConversationStorage:
    def __init__(self, senders):
        self.senders = senders

    async def get_sender_by_conversation(self, conversation_id):
        return self.senders.get(conversation_id)


class _Registry:
    def __init__(self, *adapters, senders=None):
        self._adapters = {a.channel_id: a for a in adapters}
        self.storage = _ConversationStorage(senders or {})

    def get_adapter(self, channel_id):
        return self._adapters.get(channel_id)


class _Groups:
    def __init__(self, *rows):
        self.rows = {row["id"]: row for row in rows}

    async def get_group(self, group_id):
        return self.rows.get(group_id)

    async def get_group_by_conversation(self, conversation_id):
        return next(
            (r for r in self.rows.values() if r.get("conversation_id") == conversation_id),
            None,
        )


def _group(**kw):
    row = {
        "id": "g1", "channel_id": "ch-tg", "profile": "p",
        "platform_chat_id": _CHAT_ID, "title": "Sales team",
        "status": "approved", "conversation_id": "conv-g1",
    }
    row.update(kw)
    return row


def _sender(**kw):
    row = {
        "id": "row-1", "channel_id": "ch-tg", "sender_id": "777",
        "display_name": "Lee", "authenticated": True, "conversation_id": "conv-dm",
    }
    row.update(kw)
    return row


class _Store:
    def __init__(self, rows):
        self.rows = dict(rows)

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """Profile ``p`` with a real system dir and working directory, the default
    upload cap, and no pause between files."""
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    monkeypatch.setattr(cfg, "_dynamic_config_storage", _Store({"p": None}))
    wd.invalidate()
    import app.utils.uploads_tmp as uploads

    monkeypatch.setattr(uploads, "max_upload_bytes", lambda: 100 * 1024 * 1024)
    monkeypatch.setattr(chat_files, "_PACING_SECONDS", {})
    monkeypatch.setattr(chat_files, "_DEFAULT_PACING_SECONDS", 0.0)
    chat_files.reset_delivery_records()
    ns = SimpleNamespace(sys=sysdir, work=Path(cfg.get_user_working_directory("p")))
    yield ns
    chat_files.reset_delivery_records()
    wd.invalidate()


@pytest.fixture
def groups(monkeypatch):
    fake = _Groups()
    monkeypatch.setattr(storage_mod, "get_channel_group_storage", lambda *a, **k: fake)
    return fake


def _file(box, name="report.pdf", data=b"%PDF-1.7 the report") -> str:
    path = box.work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _resolve(registry, conversation_id="conv-g1", context_id=None, profile="p"):
    return asyncio.run(chat_files.resolve_chat(
        profile, conversation_id, registry=registry, context_id=context_id,
    ))


def _refused(registry, **kw) -> ChatFilesError:
    with pytest.raises(ChatFilesError) as caught:
        _resolve(registry, **kw)
    return caught.value


# ── where ──────────────────────────────────────────────────────────────────


def test_a_group_conversation_resolves_to_its_room(box, groups):
    groups.rows["g1"] = _group()
    chat = _resolve(_Registry(_Adapter()))
    assert chat.kind == KIND_GROUP
    assert chat.target.is_group and chat.target.address == _CHAT_ID
    assert chat.describe() == {"kind": "group", "name": "Sales team", "platform": "Telegram"}


def test_a_private_conversation_resolves_to_the_person(box, groups):
    chat = _resolve(
        _Registry(_Adapter(), senders={"conv-dm": _sender()}), conversation_id="conv-dm",
    )
    assert chat.kind == KIND_PRIVATE
    assert not chat.target.is_group and chat.target.address == "777"
    assert chat.name == "Lee"


def test_without_a_run_binding_the_group_context_still_resolves(box, groups):
    groups.rows["g1"] = _group(conversation_id="conv-renamed")
    chat = _resolve(_Registry(_Adapter()), conversation_id=None,
                    context_id="channel_group:g1")
    assert chat.target.address == _CHAT_ID


def test_without_a_run_binding_a_private_chats_context_is_its_conversation(box, groups):
    chat = _resolve(_Registry(_Adapter(), senders={"conv-dm": _sender()}),
                    conversation_id=None, context_id="conv-dm")
    assert chat.target.address == "777"


@pytest.mark.parametrize("conversation_id, context_id", [
    ("conv-web", "conv-web"),       # a web UI chat: no group, no sender
    (None, None),                   # no turn at all
    ("conv-seat", "group:gc1:p"),   # a Cremind group seat
])
def test_a_conversation_that_is_not_a_channel_chat_is_refused(
    box, groups, conversation_id, context_id,
):
    err = _refused(_Registry(_Adapter()), conversation_id=conversation_id,
                   context_id=context_id)
    assert err.code == "NotInAChat"


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_a_group_that_is_not_approved_gets_nothing(box, groups, status):
    groups.rows["g1"] = _group(status=status)
    err = _refused(_Registry(_Adapter()))
    assert err.code == "GroupNotApproved"
    assert err.extra["group_status"] == status


def test_a_channel_that_is_not_running_is_refused(box, groups):
    groups.rows["g1"] = _group()
    assert _refused(_Registry()).code == "ChannelNotRunning"


def test_group_chats_switched_off_on_the_channel_are_refused(box, groups):
    groups.rows["g1"] = _group()
    assert _refused(_Registry(_Adapter(groups_on=False))).code == "GroupChatsOff"


@pytest.mark.parametrize("where", ["group", "adapter"])
def test_another_profiles_chat_is_refused(box, groups, where):
    groups.rows["g1"] = _group(profile="q" if where == "group" else "p")
    adapter = _Adapter(profile="q" if where == "adapter" else "p")
    assert _refused(_Registry(adapter)).code == "NotYourChat"


def test_a_group_row_without_a_chat_id_is_refused(box, groups):
    groups.rows["g1"] = _group(platform_chat_id="")
    assert _refused(_Registry(_Adapter())).code == "GroupUnreachable"


def test_a_transport_without_file_support_is_refused_before_anything_goes(box, groups):
    groups.rows["g1"] = _group()
    err = _refused(_Registry(_NoFiles()))
    assert err.code == "FilesUnsupported"
    assert err.extra["chat"]["kind"] == "group"


def test_a_person_the_channel_no_longer_admits_gets_nothing(box, groups):
    registry = _Registry(
        _Adapter(auth="approval"), senders={"conv-dm": _sender(authenticated=False)},
    )
    assert _refused(registry, conversation_id="conv-dm").code == "NotAllowed"


def test_an_open_channel_admits_everyone_it_talks_to(box, groups):
    registry = _Registry(_Adapter(auth="open"),
                         senders={"conv-dm": _sender(authenticated=False)})
    assert _resolve(registry, conversation_id="conv-dm").kind == KIND_PRIVATE


# ── what ───────────────────────────────────────────────────────────────────


def _chat(adapter=None, kind="group"):
    adapter = adapter or _Adapter()
    from app.channels.reply_target import group_target, sender_target

    target = group_target(_group()) if kind == "group" else sender_target("777")
    return chat_files.ChatRef(
        adapter=adapter, target=target,
        kind=KIND_GROUP if kind == "group" else KIND_PRIVATE,
        name="Sales team", platform="Telegram",
    )


def test_paths_are_normalized_and_counted():
    assert chat_files.normalize_paths("/a.pdf") == ["/a.pdf"]
    assert chat_files.normalize_paths(["/a.pdf", " ", None]) == ["/a.pdf"]
    for bad in (None, [], ["  "], 5):
        with pytest.raises(ValueError):
            chat_files.normalize_paths(bad)
    with pytest.raises(ValueError, match="more than the 10"):
        chat_files.normalize_paths([f"/f{i}.pdf" for i in range(11)])


def test_a_path_given_twice_is_sent_once(box):
    path = _file(box)
    ok, rejected = chat_files.check_files("p", [path, path], chat=_chat())
    assert rejected == [] and len(ok) == 1


def test_an_empty_file_is_refused(box):
    path = _file(box, "empty.txt", b"")
    ok, rejected = chat_files.check_files("p", [path], chat=_chat())
    assert ok == [] and "empty" in rejected[0]["reason"]


def test_a_file_over_the_platforms_cap_is_refused_up_front(box):
    path = _file(box, data=b"x" * 11)
    ok, rejected = chat_files.check_files("p", [path], chat=_chat(_SmallCap()))
    assert ok == []
    assert "Telegram accepts" in rejected[0]["reason"]


def test_a_file_over_the_servers_upload_cap_is_refused(box, monkeypatch):
    import app.utils.uploads_tmp as uploads

    monkeypatch.setattr(uploads, "max_upload_bytes", lambda: 5)
    ok, rejected = chat_files.check_files("p", [_file(box)], chat=_chat())
    assert ok == [] and "uploads.tmp_max_bytes" in rejected[0]["reason"]


def test_refusals_come_back_alongside_what_could_go(box):
    good = _file(box)
    ok, rejected = chat_files.check_files(
        "p", [good, str(box.work / "missing.pdf"), str(box.work / ".env")], chat=_chat(),
    )
    assert [o["name"] for o in ok] == ["report.pdf"]
    assert {r["reason"] for r in rejected} >= {"not an existing file"}
    assert len(rejected) == 2


def test_quoted_paths_are_the_paths_inside_the_quotes():
    assert chat_files.normalize_paths(['"/a.pdf"', "'/b.pdf'", "`/c.pdf`"]) == [
        "/a.pdf", "/b.pdf", "/c.pdf",
    ]


def test_a_file_uri_is_its_path(box):
    """The web UI's file chips carry ``file://`` URIs, and models pass them on."""
    path = Path(_file(box))
    ok, rejected = chat_files.check_files("p", [path.as_uri()], chat=_chat())
    assert rejected == [] and ok[0]["name"] == "report.pdf"


def test_a_relative_path_is_read_from_the_turns_working_directory(box):
    _file(box, "reports/q3.pdf")
    ok, rejected = chat_files.check_files(
        "p", ["reports/q3.pdf"], chat=_chat(), working_directory=str(box.work),
    )
    assert rejected == [] and ok[0]["name"] == "q3.pdf"


def test_a_relative_path_still_meets_every_guard(box):
    _file(box, ".env", b"SECRET=1")
    ok, rejected = chat_files.check_files(
        "p", [".env", "../../../etc/passwd"], chat=_chat(),
        working_directory=str(box.work),
    )
    assert ok == [] and len(rejected) == 2


def test_a_web_address_is_refused_with_what_to_do_instead(box):
    ok, rejected = chat_files.check_files(
        "p", ["https://example.com/report.pdf"], chat=_chat(),
    )
    assert ok == [] and "download it into your folder first" in rejected[0]["reason"]


def test_a_folder_is_refused_with_what_to_do_instead(box):
    (box.work / "photos").mkdir(parents=True)
    ok, rejected = chat_files.check_files("p", [str(box.work / "photos")], chat=_chat())
    assert ok == [] and "put it in a .zip first" in rejected[0]["reason"]


def test_the_turns_working_directory_is_a_root(box, tmp_path):
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    (elsewhere / "plan.pdf").write_bytes(b"%PDF")
    ok, rejected = chat_files.check_files(
        "p", [str(elsewhere / "plan.pdf")], chat=_chat(),
        working_directory=str(elsewhere),
    )
    assert rejected == [] and len(ok) == 1


# ── how it went ────────────────────────────────────────────────────────────


def _entries(box, *names):
    paths = [_file(box, n, f"content of {n}".encode()) for n in names]
    ok, rejected = chat_files.check_files("p", paths, chat=_chat())
    assert rejected == []
    return ok


def _deliver(chat, entries, run_id="run-1"):
    return asyncio.run(chat_files.deliver(chat, entries, run_id=run_id))


def test_files_go_into_the_room_by_its_chat_id(box):
    adapter = _Adapter()
    out = _deliver(_chat(adapter), _entries(box, "a.pdf", "b.pdf"))
    assert adapter.sent == [("room", _CHAT_ID, "a.pdf"), ("room", _CHAT_ID, "b.pdf")]
    assert out["sent"] == 2 and out["failed"] == 0
    assert [r["status"] for r in out["results"]] == [SENT, SENT]
    assert "Everyone in the group" in out["note"]


def test_files_go_to_the_person_in_a_private_chat(box):
    adapter = _Adapter()
    out = _deliver(_chat(adapter, kind="private"), _entries(box, "a.pdf"))
    assert adapter.sent == [("dm", "777", "a.pdf")]
    assert out["chat"]["kind"] == "private"
    assert "Everyone in the group" not in out["note"]


def test_one_failure_does_not_cost_the_others(box):
    adapter = _Adapter(fail=[None, RuntimeError("file type not allowed"), None])
    out = _deliver(_chat(adapter), _entries(box, "a.pdf", "b.exe", "c.pdf"))
    assert [r["status"] for r in out["results"]] == [SENT, FAILED, SENT]
    assert "file type not allowed" in out["results"][1]["detail"]
    assert out["sent"] == 2 and out["failed"] == 1


def test_two_failures_in_a_row_stop_the_batch(box):
    adapter = _Adapter(fail=[RuntimeError("down"), RuntimeError("down")])
    out = _deliver(_chat(adapter), _entries(box, "a.pdf", "b.pdf", "c.pdf"))
    assert [r["status"] for r in out["results"]] == [FAILED, FAILED, NOT_SENT]
    assert adapter.attempts == 2
    assert out["failed"] == 3


def test_a_transport_that_turns_out_to_have_no_file_support_stops_at_once(box):
    adapter = _Adapter(fail=[ChannelNotImplemented("no files")])
    out = _deliver(_chat(adapter), _entries(box, "a.pdf", "b.pdf"))
    assert [r["status"] for r in out["results"]] == [FAILED, NOT_SENT]
    assert out["results"][1]["detail"] == "Not sent: Telegram cannot send files into this chat."
    assert adapter.attempts == 1


def test_an_unacknowledged_upload_is_unconfirmed_and_never_resent(box):
    adapter = _Adapter(fail=[DeliveryUnconfirmed("no ack within 180s")])
    chat = _chat(adapter)
    entries = _entries(box, "a.pdf")
    first = _deliver(chat, entries)
    assert first["results"][0]["status"] == UNCONFIRMED
    assert first["unconfirmed"] == 1 and "ask them to check" in first["note"]

    again = _deliver(chat, entries)
    assert again["results"][0]["status"] == UNCONFIRMED
    assert adapter.attempts == 1  # the retry never reached the platform


def test_the_same_file_twice_in_one_turn_is_sent_once(box):
    adapter = _Adapter()
    chat = _chat(adapter)
    entries = _entries(box, "a.pdf")
    _deliver(chat, entries)
    again = _deliver(chat, entries)
    assert again["results"][0]["status"] == ALREADY_SENT
    assert again["already_sent"] == 1 and again["sent"] == 0
    assert len(adapter.sent) == 1


def test_a_new_turn_may_send_the_same_file_again(box):
    adapter = _Adapter()
    chat = _chat(adapter)
    entries = _entries(box, "a.pdf")
    _deliver(chat, entries, run_id="run-1")
    _deliver(chat, entries, run_id="run-2")
    assert len(adapter.sent) == 2


def test_an_edited_file_is_a_new_file(box):
    adapter = _Adapter()
    chat = _chat(adapter)
    _deliver(chat, _entries(box, "a.pdf"))
    path = box.work / "a.pdf"
    path.write_bytes(b"a longer, edited version of the report")
    # Checked afresh rather than through ``_entries``, which would write the
    # original content back over the edit.
    edited, rejected = chat_files.check_files("p", [str(path)], chat=chat)
    assert rejected == []
    _deliver(chat, edited)
    assert len(adapter.sent) == 2


def test_calls_with_no_turn_do_not_share_a_record(box):
    """Pooling every turn-less call under one empty id would make a file sent
    by one of them "already sent" to all the others."""
    adapter = _Adapter()
    chat = _chat(adapter)
    entries = _entries(box, "a.pdf")
    _deliver(chat, entries, run_id="")
    _deliver(chat, entries, run_id="")
    assert len(adapter.sent) == 2


def test_parallel_calls_in_one_turn_send_a_file_once(box):
    """Models emit tool calls in parallel; both must not decide it is new."""
    adapter = _Adapter(delay=0.05)
    chat = _chat(adapter)
    entries = _entries(box, "a.pdf")

    async def both():
        return await asyncio.gather(
            chat_files.deliver(chat, entries, run_id="run-1"),
            chat_files.deliver(chat, entries, run_id="run-1"),
        )

    outs = asyncio.run(both())
    assert len(adapter.sent) == 1
    assert sorted(o["results"][0]["status"] for o in outs) == [ALREADY_SENT, SENT]


def test_the_record_is_per_chat(box):
    adapter = _Adapter()
    entries = _entries(box, "a.pdf")
    _deliver(_chat(adapter), entries)
    _deliver(_chat(adapter, kind="private"), entries)
    assert [s[0] for s in adapter.sent] == ["room", "dm"]


def test_the_call_stops_starting_uploads_before_its_timeout(box, monkeypatch):
    monkeypatch.setattr(chat_files, "_budget_seconds", lambda: 0.0)
    adapter = _Adapter()
    out = _deliver(_chat(adapter), _entries(box, "a.pdf"))
    assert out["results"][0]["status"] == NOT_SENT
    assert "ran out of time" in out["results"][0]["detail"]
    assert adapter.attempts == 0


def test_time_spent_before_delivery_counts_against_the_budget(box, monkeypatch):
    """The tool call's timeout runs from its start — lookups and any wait on a
    parallel call in the same turn included — not from when sending begins."""
    import time

    monkeypatch.setattr(chat_files, "_budget_seconds", lambda: 10.0)
    adapter = _Adapter()
    out = asyncio.run(chat_files.deliver(
        _chat(adapter), _entries(box, "a.pdf"), run_id="run-1",
        started=time.monotonic() - 11.0,
    ))
    assert out["results"][0]["status"] == NOT_SENT
    assert adapter.attempts == 0


def test_an_upload_still_running_at_the_deadline_is_unconfirmed(box, monkeypatch):
    monkeypatch.setattr(chat_files, "_budget_seconds", lambda: 0.2)
    monkeypatch.setattr(chat_files, "_send_one", _send_one_with_floor(0.05))
    adapter = _Adapter(delay=5.0)
    out = _deliver(_chat(adapter), _entries(box, "a.pdf", "b.pdf"))
    assert [r["status"] for r in out["results"]] == [UNCONFIRMED, NOT_SENT]
    assert "may or may not have arrived" in out["results"][0]["detail"]
    # The file after it is told about the clock, not about the first upload.
    assert out["results"][1]["detail"].startswith("Not sent: the call ran out of time")
    assert adapter.sent == []


def _send_one_with_floor(floor):
    """``_send_one`` with a small timeout floor, so a test need not wait out
    the real module's one-second minimum."""
    real = chat_files._send_one

    async def patched(chat, entry, *, timeout):
        return await real(chat, entry, timeout=min(timeout or floor, floor))

    return patched


@pytest.mark.parametrize("timeout, budget", [
    (300, 255.0),   # 45 s kept back
    (40, 30.0),     # a quarter of a short timeout
    (20, 15.0),     # never at or past the timeout itself
    (0, None),      # no timeout, no deadline
])
def test_the_budget_follows_the_tool_call_timeout(monkeypatch, timeout, budget):
    monkeypatch.setattr(cfg.BaseConfig, "MCP_TOOL_CALL_TIMEOUT", timeout)
    assert chat_files._budget_seconds() == budget


def test_one_answer_carries_at_most_so_many_files(box, monkeypatch):
    """Across calls: a model looping on the tool must not flood the group."""
    monkeypatch.setattr(chat_files, "MAX_FILES_PER_TURN", 2)
    adapter = _Adapter()
    chat = _chat(adapter)
    _deliver(chat, _entries(box, "a.pdf"))
    out = _deliver(chat, _entries(box, "b.pdf", "c.pdf"))
    assert [r["status"] for r in out["results"]] == [SENT, NOT_SENT]
    assert "the most one answer may carry" in out["results"][1]["detail"]
    assert len(adapter.sent) == 2


def test_files_into_a_chat_are_paced(box, monkeypatch):
    monkeypatch.setattr(chat_files, "_PACING_SECONDS", {"telegram": 0.7})
    pauses: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *a, **k):
        pauses.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(chat_files.asyncio, "sleep", fake_sleep)
    _deliver(_chat(_Adapter()), _entries(box, "a.pdf", "b.pdf", "c.pdf"))
    assert pauses == [0.7, 0.7]


def test_a_cancelled_call_is_not_swallowed(box):
    adapter = _Adapter(fail=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        _deliver(_chat(adapter), _entries(box, "a.pdf"))


@pytest.mark.parametrize("error, phrase", [
    (RuntimeError("SlackApiError: missing_scope files:write"), "files:write"),
    (RuntimeError("Bad Request: not enough rights to send documents to the chat"),
     "not allowed to post files"),
    (type("Forbidden", (Exception,), {})("Forbidden: bot was kicked from the group chat"),
     "no longer post in this chat"),
    (RuntimeError("Request Entity Too Large"), "too large"),
    (RuntimeError("WhatsApp sidecar not connected"), "connection to the platform is down"),
])
def test_platform_refusals_are_explained(box, error, phrase):
    adapter = _Adapter(fail=[error])
    out = _deliver(_chat(adapter), _entries(box, "a.pdf"))
    detail = out["results"][0]["detail"]
    assert phrase in detail
    assert detail.startswith("Telegram refused it:")


def test_an_unexplained_refusal_still_carries_the_platforms_words(box):
    adapter = _Adapter(fail=[RuntimeError("E_WEIRD_42")])
    out = _deliver(_chat(adapter), _entries(box, "a.pdf"))
    assert out["results"][0]["detail"] == "Telegram refused it: E_WEIRD_42"


def test_the_record_stays_bounded(box, monkeypatch):
    monkeypatch.setattr(chat_files, "_TURNS_REMEMBERED", 3)
    adapter = _Adapter()
    entries = _entries(box, "a.pdf")
    for i in range(6):
        _deliver(_chat(adapter), entries, run_id=f"run-{i}")
    assert len(chat_files._turns) == 3


# ── through a real adapter ─────────────────────────────────────────────────


def _real_adapter(*, rooms=True):
    from app.channels.base import BaseChannelAdapter

    class _Real(BaseChannelAdapter):
        supports_group_chats = True
        supports_file_send = True

        def __init__(self):
            super().__init__(
                {"id": "ch-real", "profile": "p", "channel_type": "telegram",
                 "mode": "bot", "config": {"group_chats_enabled": True}, "state": {}},
                storage=None,
            )
            self.room_files: list[tuple] = []
            self.person_files: list[tuple] = []

        async def _run(self):
            return None

        async def _send_text(self, sender_id, text):
            return None

        async def _send_file(self, sender_id, path, *, name=None, mime=None, caption=None):
            self.person_files.append((sender_id, name))

    if rooms:
        async def _send_file_to_chat(self, chat_id, path, *, name=None, mime=None,
                                     caption=None):
            self.room_files.append((chat_id, name))

        _Real._send_file_to_chat = _send_file_to_chat
    return _Real()


def test_a_real_adapter_sends_rooms_and_people_through_their_own_hooks(box):
    """A room's id is nobody's sender id: the room goes through the room hook
    and the person through the person hook, never the other way round."""
    adapter = _real_adapter()
    entries = _entries(box, "a.pdf")
    _deliver(_chat(adapter), entries, run_id="r1")
    _deliver(_chat(adapter, kind="private"), entries, run_id="r1")
    assert adapter.room_files == [(_CHAT_ID, "a.pdf")]
    assert adapter.person_files == [("777", "a.pdf")]


def test_a_transport_with_no_room_file_hook_says_so_rather_than_posting_a_notice(box):
    """The base room hook raises ``ChannelNotImplemented``; the strict sender
    lets it through, so the agent hears "cannot" instead of the chat getting
    a "this channel can't receive files" notice nobody asked for."""
    adapter = _real_adapter(rooms=False)
    out = _deliver(_chat(adapter), _entries(box, "a.pdf", "b.pdf"))
    assert [r["status"] for r in out["results"]] == [FAILED, NOT_SENT]
    assert adapter.room_files == [] and adapter.person_files == []


# ── the manual says what the code does ─────────────────────────────────────


def test_the_channels_manual_matches_the_limits_in_the_code():
    """The limits the bundled manual quotes are the ones the adapters declare,
    so changing one without the other fails here rather than in front of a
    user who was told the wrong number."""
    import app
    from app.channels.adapters.discord import DiscordAdapter
    from app.channels.adapters.messenger import MessengerAdapter
    from app.channels.adapters.telegram import TelegramAdapter

    manual = (
        Path(app.__file__).parent / "cremind_documents" / "bundled"
        / "[cli]cremind channels.md"
    ).read_text(encoding="utf-8")
    assert "send_files_to_chat" in manual
    assert f"at most {chat_files.MAX_FILES_PER_CALL} files" in manual
    for adapter_cls, label in (
        (TelegramAdapter, "Telegram bot"),
        (DiscordAdapter, "Discord"),
        (MessengerAdapter, "Messenger"),
    ):
        megabytes = adapter_cls.max_file_send_bytes // (1024 * 1024)
        assert f"{label} {megabytes} MB" in manual, label
