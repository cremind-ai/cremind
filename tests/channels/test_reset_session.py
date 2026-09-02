"""``reset_session()`` — erasing a channel's persisted pairing session.

The recovery path for a session the platform invalidated behind our back (the
same account paired in another environment, a device revoked from the phone).
Such a session still looks valid on disk, so every session-based adapter keeps
restoring it instead of pairing and no QR/code is ever produced; deleting it is
the only way back into the pairing flow.

These pin the two things that matter: the right directory is removed, and it is
removed for every pairing adapter (a miss on one channel type means that
platform still needs a delete-and-recreate, losing its groups).
"""

from __future__ import annotations

import os

import pytest

from app.channels.adapters.telegram_userbot import TelegramUserbotAdapter
from app.channels.adapters.whatsapp import WhatsappAdapter
from app.channels.adapters.zalo_userbot import ZaloUserbotAdapter
from app.channels.base import BaseChannelAdapter
from app.config.settings import BaseConfig


class _FakeStorage:
    async def update_channel(self, channel_id: str, **kwargs):
        return None


def _channel(channel_type: str, mode: str, cid: str = "chan-1") -> dict:
    return {
        "id": cid,
        "profile": "admin",
        "channel_type": channel_type,
        "mode": mode,
        "enabled": True,
        "config": {},
        "state": {},
    }


@pytest.fixture()
def system_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point ``CREMIND_SYSTEM_DIR`` at a throwaway tree.

    Every adapter derives its session path from this, so nothing here can
    reach a real installation's credentials.
    """
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return str(tmp_path)


# (adapter class, channel_type, mode, the file the platform leaves behind)
_CASES = [
    (ZaloUserbotAdapter, "zalo", "userbot", "credentials.json"),
    (WhatsappAdapter, "whatsapp", "userbot", os.path.join("session", "creds.json")),
    (TelegramUserbotAdapter, "telegram", "userbot", "session.session"),
]


@pytest.mark.parametrize(
    ("adapter_cls", "channel_type", "mode", "session_file"),
    _CASES,
    ids=[c[1] for c in _CASES],
)
def test_reset_session_removes_the_saved_session(
    system_dir, adapter_cls, channel_type, mode, session_file,
):
    channel = _channel(channel_type, mode)
    session_dir = os.path.join(system_dir, "admin", channel_type, channel["id"])
    target = os.path.join(session_dir, session_file)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write('{"cookie": "stale"}')

    adapter = adapter_cls(channel, _FakeStorage())
    adapter.reset_session()

    assert not os.path.exists(target)
    # The whole per-channel slice goes, not just the one file: WhatsApp's
    # Baileys state is a directory of key files and half-deleting it leaves
    # the library in a half-valid state that still refuses to show a QR.
    assert not os.path.exists(session_dir)


@pytest.mark.parametrize(
    ("adapter_cls", "channel_type", "mode", "_session_file"),
    _CASES,
    ids=[c[1] for c in _CASES],
)
def test_reset_session_is_idempotent(
    system_dir, adapter_cls, channel_type, mode, _session_file,
):
    """Resetting a channel that never paired is a no-op, not an error."""
    adapter = adapter_cls(_channel(channel_type, mode), _FakeStorage())
    adapter.reset_session()
    adapter.reset_session()


def test_telegram_reset_does_not_recreate_the_directory(system_dir):
    """``_session_path`` mkdirs as a side effect; the reset must not use it.

    Building the path through ``_session_path()`` would re-create the very
    directory being deleted, leaving an empty session dir behind — harmless
    here, but the same mistake in ``reset_session`` would mask a failed delete.
    """
    channel = _channel("telegram", "userbot")
    adapter = TelegramUserbotAdapter(channel, _FakeStorage())
    session_dir = os.path.join(system_dir, "admin", "telegram", channel["id"])
    os.makedirs(session_dir, exist_ok=True)
    for name in ("session.session", "session.session-journal"):
        with open(os.path.join(session_dir, name), "w", encoding="utf-8") as fh:
            fh.write("x")

    adapter.reset_session()

    assert not os.path.exists(session_dir)


def test_bot_adapters_have_no_session_to_reset():
    """A token-configured channel keeps the inherited no-op.

    Its credentials live in ``config``, not on disk, so the repair endpoint has
    nothing to do for it — which is why it refuses those channels outright.
    """
    from app.channels.adapters.telegram import TelegramAdapter

    assert TelegramAdapter.reset_session is BaseChannelAdapter.reset_session
    TelegramAdapter(
        _channel("telegram", "bot"), _FakeStorage(),
    ).reset_session()  # must not raise
