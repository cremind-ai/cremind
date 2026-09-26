"""The ``send_files_to_chat`` built-in tool, end to end through its ``run()``.

The tool is thin — arguments in, :mod:`app.channels.chat_files` does the work —
so these pin the seams: the chat comes from the live run binding (never from an
argument), every refusal is a structured observation rather than an exception,
nothing is sent when any file is refused, and the tool is registered, hidden,
and briefed to send only on request.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path

import pytest

import app.channels.registry as reg
import app.storage as storage_mod
from app.channels import chat_files
from app.config import working_dirs as wd
from app.events import task_result_inbox
from app.tools.builtin.send_files_to_chat import TOOL_CONFIG, SendFilesToChatTool
from app.utils.task_context import current_task_id_var

cfg = importlib.import_module("app.config.settings")

_CHAT_ID = "-1009876"


class _Adapter:
    supports_file_send = True
    supports_group_chats = True
    max_file_send_bytes = None

    def __init__(self, *, fail=None):
        self.channel_id = "ch-tg"
        self.channel_type = "telegram"
        self.profile = "p"
        self.fail = fail
        self.sent: list[tuple[str, str, str]] = []

    def groups_enabled(self):
        return True

    def _subscribe_auth(self):
        return "open"

    async def send_file_strict(self, sender_id, path, *, name=None, mime=None, caption=None):
        self.sent.append(("dm", sender_id, os.path.basename(path)))

    async def send_file_to_chat_strict(self, chat_id, path, *, name=None, mime=None,
                                       caption=None):
        if self.fail is not None:
            raise self.fail
        self.sent.append(("room", chat_id, os.path.basename(path)))


class _ConversationStorage:
    def __init__(self, senders):
        self.senders = senders

    async def get_sender_by_conversation(self, conversation_id):
        return self.senders.get(conversation_id)


class _Registry:
    def __init__(self, adapter, senders=None):
        self.adapter = adapter
        self.storage = _ConversationStorage(senders or {})

    def get_adapter(self, channel_id):
        return self.adapter if channel_id == self.adapter.channel_id else None


class _Groups:
    def __init__(self, rows, *, broken=False):
        self.rows = rows
        self.broken = broken

    async def get_group(self, group_id):
        return self.rows.get(group_id)

    async def get_group_by_conversation(self, conversation_id):
        if self.broken:
            raise RuntimeError("database is locked")
        return next(
            (r for r in self.rows.values() if r["conversation_id"] == conversation_id),
            None,
        )


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
def world(tmp_path, monkeypatch):
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

    adapter = _Adapter()
    groups = _Groups({"g1": {
        "id": "g1", "channel_id": "ch-tg", "profile": "p", "platform_chat_id": _CHAT_ID,
        "title": "Sales team", "status": "approved", "conversation_id": "conv-g1",
    }})
    registry = _Registry(adapter, senders={"conv-dm": {
        "id": "r1", "channel_id": "ch-tg", "sender_id": "777", "display_name": "Lee",
        "authenticated": True, "conversation_id": "conv-dm",
    }})
    monkeypatch.setattr(storage_mod, "get_channel_group_storage", lambda *a, **k: groups)
    monkeypatch.setattr(reg, "get_channel_registry", lambda *a, **k: registry)

    work = Path(cfg.get_user_working_directory("p"))
    yield type("World", (), {
        "adapter": adapter, "groups": groups, "registry": registry, "work": work,
    })
    chat_files.reset_delivery_records()
    wd.invalidate()


def _file(world, name="report.pdf", data=b"%PDF-1.7 report") -> str:
    path = world.work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _call(arguments, *, conversation_id="conv-g1", run_id="run-1"):
    """Run the tool the way a live turn does: inside a bound run."""

    async def scenario():
        token = current_task_id_var.set(run_id)
        if conversation_id:
            task_result_inbox.bind_run(run_id, conversation_id)
        try:
            return (await SendFilesToChatTool().run(arguments)).structured_content
        finally:
            task_result_inbox.unbind_run(run_id)
            current_task_id_var.reset(token)

    return asyncio.run(scenario())


# ── the contract ───────────────────────────────────────────────────────────


def test_it_is_a_registered_hidden_built_in():
    from app.tools.builtin import _BUILTIN_MODULE_NAMES

    assert "send_files_to_chat" in _BUILTIN_MODULE_NAMES
    assert TOOL_CONFIG["name"] == "send_files_to_chat"
    assert TOOL_CONFIG["hidden"] is True


def test_the_model_is_told_to_send_only_on_request():
    tool = SendFilesToChatTool()
    assert "ONLY ON REQUEST" in tool.description
    assert "send_channel_message" in tool.description
    files = tool.parameters["properties"]["files"]
    assert files["maxItems"] == chat_files.MAX_FILES_PER_CALL
    # The destination is never an argument: it is the chat the turn is in.
    assert set(tool.parameters["properties"]) == {"files"}
    assert tool.parameters["additionalProperties"] is False


# ── happy paths ────────────────────────────────────────────────────────────


def test_a_file_asked_for_in_a_group_is_posted_into_that_group(world):
    out = _call({"files": [_file(world)], "_profile": "p"})
    assert world.adapter.sent == [("room", _CHAT_ID, "report.pdf")]
    assert out["sent"] == 1 and out["failed"] == 0
    assert out["chat"] == {"kind": "group", "name": "Sales team", "platform": "Telegram"}
    assert "Do not send them again" in out["note"]


def test_a_file_asked_for_in_a_private_chat_goes_to_that_person(world):
    out = _call({"files": [_file(world)], "_profile": "p"}, conversation_id="conv-dm")
    assert world.adapter.sent == [("dm", "777", "report.pdf")]
    assert out["chat"]["kind"] == "private"


def test_a_single_path_string_is_accepted(world):
    out = _call({"files": _file(world), "_profile": "p"})
    assert out["sent"] == 1


def test_the_group_context_is_the_fallback_without_a_run_binding(world):
    out = _call(
        {"files": [_file(world)], "_profile": "p", "_context_id": "channel_group:g1"},
        conversation_id=None,
    )
    assert world.adapter.sent == [("room", _CHAT_ID, "report.pdf")]
    assert out["sent"] == 1


def test_a_second_call_for_the_same_file_in_one_turn_sends_nothing(world):
    path = _file(world)
    _call({"files": [path], "_profile": "p"})
    again = _call({"files": [path], "_profile": "p"})
    assert again["sent"] == 0 and again["already_sent"] == 1
    assert len(world.adapter.sent) == 1


# ── refusals: structured, and nothing sent ─────────────────────────────────


def test_no_profile_is_refused():
    out = _call({"files": ["/x.pdf"]})
    assert out["error"] == "NoProfile"


@pytest.mark.parametrize("files", [None, [], ["  "], [f"/f{i}.pdf" for i in range(11)]])
def test_bad_file_lists_are_refused(files):
    arguments = {"_profile": "p"}
    if files is not None:
        arguments["files"] = files
    assert _call(arguments)["error"] == "InvalidArgument"


def test_no_channel_subsystem_is_refused(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("not initialized")

    monkeypatch.setattr(reg, "get_channel_registry", _boom)
    out = _call({"files": ["/x.pdf"], "_profile": "p"})
    assert out["error"] == "ChannelsUnavailable"


def test_the_web_ui_is_not_a_chat_to_send_into(world):
    out = _call({"files": [_file(world)], "_profile": "p"}, conversation_id="conv-web")
    assert out["error"] == "NotInAChat"
    assert world.adapter.sent == []


def test_one_refused_file_means_nothing_is_sent(world):
    good = _file(world, "report.pdf")
    secret = _file(world, ".env", b"API_KEY=xyz")
    out = _call({"files": [good, secret], "_profile": "p"})
    assert out["error"] == "InvalidFile"
    assert out["rejected"] == [{"path": secret, "reason": out["rejected"][0]["reason"]}]
    assert "hidden" in out["rejected"][0]["reason"]
    assert out["chat"]["kind"] == "group"
    assert world.adapter.sent == []


def test_a_file_in_another_profiles_folder_is_refused(world, tmp_path):
    other = tmp_path / "sys" / "workspaces" / "q" / "payroll.pdf"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"%PDF")
    out = _call({"files": [str(other)], "_profile": "p"})
    assert out["error"] == "InvalidFile"
    assert world.adapter.sent == []


def test_a_broken_lookup_is_a_structured_failure(world):
    world.groups.broken = True
    out = _call({"files": [_file(world)], "_profile": "p"})
    assert out["error"] == "SendFailed"
    assert "database is locked" in out["message"]
    assert world.adapter.sent == []


def test_a_platform_refusal_is_reported_per_file(world):
    world.adapter.fail = RuntimeError("Bad Request: not enough rights to send documents")
    out = _call({"files": [_file(world)], "_profile": "p"})
    assert out["sent"] == 0 and out["failed"] == 1
    assert "not allowed to post files" in out["results"][0]["detail"]
    assert "Tell them which files did not arrive" in out["note"]


def test_a_crash_inside_delivery_is_a_structured_failure(world, monkeypatch):
    async def _explode(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(chat_files, "deliver", _explode)
    out = _call({"files": [_file(world)], "_profile": "p"})
    assert out["error"] == "SendFailed"
    assert "ask them to check" in out["message"]
