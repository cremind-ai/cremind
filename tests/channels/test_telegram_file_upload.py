"""The Telegram bot's file upload retries only what provably sent nothing.

An upload retried after it landed posts the file twice — into a group, in front
of everyone. PTB files two very different errors under ``NetworkError``:
``BadRequest`` is Telegram refusing this exact request, which a retry only
repeats, and ``TimedOut`` can arrive after Telegram already has the file. So a
refusal is raised at once, a timeout after the request left is reported as
unconfirmed rather than retried, and only a timeout before anything left (a
pooled or connecting request) or a plain dead connection is tried again. Flood
control is waited out once when the pause is short.

python-telegram-bot is an optional extra that CI does not install, so the error
module is stood up as fakes (like ``test_telegram_group_routing.py``), and the
premise those fakes encode is checked against the real library wherever it is
installed.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from types import ModuleType

import pytest

from app.channels.adapters import telegram as tg
from app.channels.adapters.telegram import TelegramAdapter
from app.channels.exceptions import DeliveryUnconfirmed


class _NetworkError(Exception):
    pass


class _BadRequest(_NetworkError):
    pass


class _TimedOut(_NetworkError):
    pass


class _RetryAfter(Exception):
    def __init__(self, retry_after):
        super().__init__(f"Flood control exceeded. Retry in {retry_after} seconds")
        self.retry_after = retry_after


class ConnectTimeout(Exception):
    """Named like httpx's, which is all the adapter looks at."""


@pytest.fixture(autouse=True)
def _ptb(monkeypatch):
    error = ModuleType("telegram.error")
    error.NetworkError = _NetworkError
    error.BadRequest = _BadRequest
    error.TimedOut = _TimedOut
    error.RetryAfter = _RetryAfter
    monkeypatch.setitem(sys.modules, "telegram", ModuleType("telegram"))
    monkeypatch.setitem(sys.modules, "telegram.error", error)


class _Bot:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def send_document(self, **kw):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


class _Storage:
    async def update_channel(self, *a, **k):
        return None


def _adapter(bot, monkeypatch):
    adapter = TelegramAdapter(
        {"id": "c1", "profile": "p", "channel_type": "telegram", "mode": "bot",
         "config": {"bot_token": "t"}, "state": {}},
        _Storage(),
    )
    adapter._bot = bot
    adapter.resets = 0

    async def _reset():
        adapter.resets += 1

    monkeypatch.setattr(adapter, "_reset_bot", _reset)
    return adapter


@pytest.fixture
def pauses(monkeypatch):
    waited: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *a, **k):
        waited.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(tg.asyncio, "sleep", fake_sleep)
    return waited


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7 report")
    return str(path)


def _send(adapter, doc):
    return asyncio.run(
        adapter._send_document_with_retry(-1001234, doc, "report.pdf", None),
    )


def test_a_refusal_is_raised_at_once_and_the_pool_left_alone(monkeypatch, doc, pauses):
    bot = _Bot(_BadRequest("Bad Request: not enough rights to send documents"))
    adapter = _adapter(bot, monkeypatch)
    with pytest.raises(_BadRequest):
        _send(adapter, doc)
    assert bot.calls == 1 and adapter.resets == 0


def test_a_timeout_after_the_upload_left_is_unconfirmed_not_retried(monkeypatch, doc, pauses):
    bot = _Bot(_TimedOut("Timed out"))
    adapter = _adapter(bot, monkeypatch)
    with pytest.raises(DeliveryUnconfirmed, match="may or may not have arrived"):
        _send(adapter, doc)
    assert bot.calls == 1


def test_a_timeout_while_connecting_is_retried(monkeypatch, doc, pauses):
    timed_out = _TimedOut("Timed out")
    timed_out.__cause__ = ConnectTimeout()
    bot = _Bot(timed_out, None)
    adapter = _adapter(bot, monkeypatch)
    _send(adapter, doc)
    assert bot.calls == 2 and adapter.resets == 1


def test_a_pool_timeout_is_retried(monkeypatch, doc, pauses):
    bot = _Bot(_TimedOut("Pool timeout: All connections in the connection pool are occupied."), None)
    adapter = _adapter(bot, monkeypatch)
    _send(adapter, doc)
    assert bot.calls == 2


def test_a_dead_connection_is_reset_and_retried(monkeypatch, doc, pauses):
    bot = _Bot(_NetworkError("httpx.RemoteProtocolError"), None)
    adapter = _adapter(bot, monkeypatch)
    _send(adapter, doc)
    assert bot.calls == 2 and adapter.resets == 1


@pytest.mark.parametrize("retry_after", [3, timedelta(seconds=3)], ids=["int", "timedelta"])
def test_a_short_flood_pause_is_waited_out_once(monkeypatch, doc, pauses, retry_after):
    bot = _Bot(_RetryAfter(retry_after), None)
    adapter = _adapter(bot, monkeypatch)
    _send(adapter, doc)
    assert bot.calls == 2
    assert pauses == [3.5]


def test_flood_control_twice_is_raised(monkeypatch, doc, pauses):
    bot = _Bot(_RetryAfter(2), _RetryAfter(2))
    adapter = _adapter(bot, monkeypatch)
    with pytest.raises(_RetryAfter):
        _send(adapter, doc)
    assert bot.calls == 2


def test_a_long_flood_pause_is_raised_rather_than_sat_through(monkeypatch, doc, pauses):
    bot = _Bot(_RetryAfter(120))
    adapter = _adapter(bot, monkeypatch)
    with pytest.raises(_RetryAfter):
        _send(adapter, doc)
    assert bot.calls == 1 and pauses == []


def test_a_file_over_the_bot_cap_never_reaches_telegram(monkeypatch, tmp_path, pauses):
    big = tmp_path / "big.bin"
    with open(big, "wb") as handle:
        handle.truncate(tg._TG_BOT_UPLOAD_LIMIT + 1)
    bot = _Bot()
    adapter = _adapter(bot, monkeypatch)
    with pytest.raises(ValueError, match="Telegram bots can only upload"):
        _send(adapter, str(big))
    assert bot.calls == 0


def test_the_cap_is_declared_for_checks_before_a_batch():
    assert TelegramAdapter.max_file_send_bytes == tg._TG_BOT_UPLOAD_LIMIT


def test_the_error_hierarchy_the_fakes_encode_is_the_real_one():
    # The autouse fixture has put fakes in ``sys.modules``; they go first, or
    # ``importorskip`` would find a fake and never skip where the library is
    # not installed (CI). The fixture's monkeypatch restores the originals.
    sys.modules.pop("telegram.error", None)
    sys.modules.pop("telegram", None)
    pytest.importorskip("telegram", reason="python-telegram-bot is an optional extra")
    import importlib

    error = importlib.import_module("telegram.error")
    assert issubclass(error.BadRequest, error.NetworkError)
    assert issubclass(error.TimedOut, error.NetworkError)
    assert not issubclass(error.RetryAfter, error.NetworkError)
