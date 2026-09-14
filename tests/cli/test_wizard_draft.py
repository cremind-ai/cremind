"""The wizard's draft file — where a half-answered profile setup lives.

The draft is the only thing standing between "the agent asked the user for an
API key" and "the API key is on the server": it holds plaintext credentials for
as long as the conversation takes. Three properties matter more than the rest.

**Where it lives.** Under the *acting* profile's own directory, not the target's
— the target may not exist yet, and Cremind keys per-profile state by whoever is
acting. That location is also what puts it inside the `system_file` tool's
allowed roots and outside every other profile's.

**How it is written.** Atomically and 0600, like the token file: the alternative
is a half-written draft that a concurrent read parses into a wrong payload, or a
world-readable API key.

**What it forgets.** A draft nobody finished is deleted after a week rather than
keeping a key on disk forever.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.cli import wizard_draft


@pytest.fixture(autouse=True)
def sysdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path


# ── names ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["javis", "a-b_c", "x1"])
def test_a_legal_profile_name_is_accepted(name) -> None:
    assert wizard_draft.validate_profile_name(name) == name


@pytest.mark.parametrize("name", ["", "Javis", "a b", "a/b", "..", "../etc", "a" * 65])
def test_an_illegal_name_is_refused_before_it_becomes_a_filename(name) -> None:
    """The name goes straight into a path, so the server's own rule is applied
    here first — a separator or a ``..`` must never reach the filesystem."""
    with pytest.raises(ValueError):
        wizard_draft.validate_profile_name(name)


# ── the file ─────────────────────────────────────────────────────────────


def test_a_draft_lives_in_the_acting_profiles_own_directory(sysdir: Path) -> None:
    path = wizard_draft.draft_path("admin", "javis")
    assert path == sysdir / "admin" / "cli-wizards" / "javis.json"


def test_a_draft_round_trips(sysdir: Path) -> None:
    draft = wizard_draft.new_draft("javis", adopt=True)
    draft.set_payload("llm", {"default_provider": "anthropic"})
    wizard_draft.save("admin", draft)

    loaded = wizard_draft.load("admin", "javis")
    assert loaded is not None
    assert loaded.profile == "javis"
    assert loaded.adopt is True
    assert loaded.payload("llm") == {"default_provider": "anthropic"}
    assert loaded.status("tools") == wizard_draft.STATUS_PENDING


def test_no_draft_is_no_error(sysdir: Path) -> None:
    assert wizard_draft.load("admin", "javis") is None


def test_a_save_leaves_no_temp_file_behind(sysdir: Path) -> None:
    wizard_draft.save("admin", wizard_draft.new_draft("javis"))
    wizard_draft.save("admin", wizard_draft.new_draft("javis"))
    names = {p.name for p in (sysdir / "admin" / "cli-wizards").iterdir()}
    assert names == {"javis.json"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_draft_and_its_directory_are_private(sysdir: Path) -> None:
    path = wizard_draft.save("admin", wizard_draft.new_draft("javis"))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


def test_an_unreadable_draft_says_how_to_start_over(sysdir: Path) -> None:
    """Rather than a traceback, or — worse — silently treating a corrupt draft
    as no draft and losing the answers already collected."""
    path = wizard_draft.draft_path("admin", "javis")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(wizard_draft.DraftError) as excinfo:
        wizard_draft.load("admin", "javis")
    assert "cremind profile wizard cancel javis" in str(excinfo.value)


def test_a_draft_from_a_different_version_is_refused(sysdir: Path) -> None:
    path = wizard_draft.draft_path("admin", "javis")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 99, "profile": "javis"}), encoding="utf-8")
    with pytest.raises(wizard_draft.DraftError):
        wizard_draft.load("admin", "javis")


def test_an_abandoned_draft_expires_and_is_deleted(sysdir: Path) -> None:
    path = wizard_draft.save("admin", wizard_draft.new_draft("javis"))
    old = time.time() - wizard_draft.DRAFT_TTL_SECONDS - 60
    os.utime(path, (old, old))

    assert wizard_draft.load("admin", "javis") is None
    assert not path.exists(), "an expired draft holds an API key; it must not linger"


def test_delete_can_spare_the_result_record(sysdir: Path) -> None:
    """``finish`` replaces the draft with its result; ``cancel`` throws away
    both."""
    wizard_draft.save("admin", wizard_draft.new_draft("javis"))
    wizard_draft.save_result("admin", "javis", {"profile": "javis"})

    wizard_draft.delete("admin", "javis", include_result=False)
    assert wizard_draft.load("admin", "javis") is None
    assert wizard_draft.load_result("admin", "javis") == {"profile": "javis"}

    wizard_draft.delete("admin", "javis")
    assert wizard_draft.load_result("admin", "javis") is None


# ── progress ─────────────────────────────────────────────────────────────


def test_steps_are_asked_in_order_and_skipping_counts_as_answered() -> None:
    draft = wizard_draft.new_draft("javis")
    assert wizard_draft.next_step(draft) == "llm"
    assert not wizard_draft.is_ready(draft)

    draft.set_payload("llm", {"default_provider": "anthropic"})
    assert wizard_draft.next_step(draft) == "tools"

    for step in ("tools", "memory", "channels"):
        draft.skip(step)
    assert wizard_draft.next_step(draft) is None
    assert wizard_draft.is_ready(draft)
    assert wizard_draft.skipped_steps(draft) == ["tools", "memory", "channels"]


# ── secrets ──────────────────────────────────────────────────────────────


def test_status_output_never_echoes_a_credential() -> None:
    """The user typed these into a chat window; printing them back puts them in
    the transcript a second time for no gain."""
    masked = wizard_draft.mask_secrets({
        "default_provider": "anthropic",
        "anthropic.api_key": "sk-ant-secret",
        "model_group.high": "anthropic/claude-sonnet-5",
    })
    assert masked["anthropic.api_key"] == wizard_draft.MASK
    assert masked["default_provider"] == "anthropic"
    assert masked["model_group.high"] == "anthropic/claude-sonnet-5"


def test_masking_reaches_into_channel_config_blocks() -> None:
    masked = wizard_draft.mask_secrets([
        {"channel_type": "telegram", "config": {"bot_token": "123:abc", "chat_id": "42"}},
    ])
    assert masked[0]["config"]["bot_token"] == wizard_draft.MASK
    assert masked[0]["config"]["chat_id"] == "42"


def test_the_step_summary_names_the_credential_without_printing_it() -> None:
    draft = wizard_draft.new_draft("javis")
    draft.set_payload("llm", {
        "default_provider": "anthropic",
        "model_group.high": "anthropic/claude-sonnet-5",
        "anthropic.api_key": "sk-ant-secret",
    })
    summary = wizard_draft.summarize_step("llm", draft)
    assert "provider=anthropic" in summary
    assert "credentials=api_key" in summary
    assert "sk-ant-secret" not in summary


# ── the setup body ───────────────────────────────────────────────────────


def test_the_body_carries_only_what_was_answered() -> None:
    """A skipped step contributes no key at all: the server reads "absent" as
    "leave the defaults alone", where an empty object says the same thing less
    clearly and an empty channel list emits a pointless progress line."""
    draft = wizard_draft.new_draft("javis")
    draft.set_payload("llm", {"model_group.high": "anthropic/claude-sonnet-5"})
    draft.set_payload("memory", {"memory.enabled": "true"})
    draft.skip("tools")
    draft.skip("channels")

    body = wizard_draft.build_setup_body(draft)
    assert body == {
        "profile": "javis",
        "llm_config": {"model_group.high": "anthropic/claude-sonnet-5"},
        "user_config": {"memory.enabled": "true"},
    }


def test_adoption_is_the_only_thing_that_adds_the_flag() -> None:
    plain = wizard_draft.build_setup_body(wizard_draft.new_draft("javis"))
    assert "adopt_existing" not in plain
    adopting = wizard_draft.build_setup_body(wizard_draft.new_draft("javis", adopt=True))
    assert adopting["adopt_existing"] is True


def test_an_empty_agent_configs_block_is_left_out() -> None:
    draft = wizard_draft.new_draft("javis")
    draft.set_payload("tools", {"tool_configs": {"t": {"_enabled": "true"}}, "agent_configs": {}})
    body = wizard_draft.build_setup_body(draft)
    assert body["tool_configs"] == {"t": {"_enabled": "true"}}
    assert "agent_configs" not in body


# ── json payloads ────────────────────────────────────────────────────────


def test_a_json_payload_file_is_type_checked(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text('[{"channel_type": "telegram"}]', encoding="utf-8")
    assert wizard_draft.load_json_payload(str(path), allow=(list, dict))[0]["channel_type"] == "telegram"
    with pytest.raises(ValueError, match="JSON object"):
        wizard_draft.load_json_payload(str(path))


def test_an_empty_payload_file_is_an_error_not_an_empty_step(tmp_path: Path) -> None:
    """The failure mode this guards is an agent shell's auto-closed stdin
    quietly storing nothing."""
    path = tmp_path / "p.json"
    path.write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        wizard_draft.load_json_payload(str(path))
