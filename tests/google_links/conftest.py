"""A fake multi-profile skills tree, with every DB and network seam stubbed.

Unlinking touches a process registry, two storage tables, the OAuth broker and
two Google endpoints. None of those belong in a unit test, so the harness
replaces each with a recorder — and records them into one ordered ``events`` list,
because the *order* of the teardown is the thing most worth pinning.

``_delete_files`` and ``_forget`` are wrapped rather than replaced: what the wipe
actually removes from disk, and which caches it actually empties, are the
assertions these tests exist for.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import app.google.unlink as U


class Harness:
    """Builds fake linked skills and records what the unlink did."""

    def __init__(self, root: Path):
        self.root = root
        self.events: List[str] = []
        self.revoke_result: Tuple[bool, str] = (True, U.REVOKED)
        self.watch_result: Tuple[bool, Optional[str]] = (True, None)
        self.teardown_result: Dict[str, Any] = {"stopped": [4242], "removed_autostart": 1}
        self.revoked_tokens: List[str] = []
        self.stopped_channels: List[Tuple[str, str]] = []

    # ── building state ──
    def scripts(self, profile: str, skill: str) -> Path:
        path = self.root / profile / "skills" / skill / "scripts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def install(self, profile: str, skill: str) -> Path:
        """An installed but unlinked skill, with the ``.env`` a real one carries."""
        scripts = self.scripts(profile, skill)
        (scripts / ".env").write_text("GOOGLE_SCOPES=\n", encoding="utf-8")
        return scripts

    def link(
        self,
        profile: str,
        skill: str,
        *,
        email: str = "u@example.com",
        client_id: str = "shared-cid",
        refresh: str = "rt-1",
        account_key: Optional[str] = None,
        scopes: Optional[List[str]] = None,
        expiry: Optional[float] = None,
    ) -> Path:
        scripts = self.install(profile, skill)
        payload = {
            "access_token": "at-1",
            "refresh_token": refresh,
            "id_token": "hdr.body.sig",
            "client_id": client_id,
            "client_secret": "csecret",
            "scopes": scopes if scopes is not None else ["openid", "email"],
            "email": email,
            "account_key": account_key if account_key is not None else f"ak-{email}",
            "expiry": expiry if expiry is not None else time.time() + 3600,
        }
        (scripts / ".google_token.json").write_text(json.dumps(payload), encoding="utf-8")
        return scripts

    def watch_state(
        self, profile: str, skill: str, *, channel_id: str = "cm-abc", resource_id: str = "res-abc"
    ) -> Path:
        scripts = self.scripts(profile, skill)
        (scripts / ".listener_state.json").write_text(
            json.dumps(
                {
                    "account_key": "ak-u@example.com",
                    "sync_token": "sync-1",
                    "channel_id": channel_id,
                    "resource_id": resource_id,
                    "watch_expiration": 1787181142000,
                }
            ),
            encoding="utf-8",
        )
        return scripts

    def event_payload(self, profile: str, skill: str, folder: str, name: str = "one.md") -> Path:
        path = self.root / profile / "skills" / skill / "events" / folder
        path.mkdir(parents=True, exist_ok=True)
        payload = path / name
        payload.write_text("# an event from the linked account\n", encoding="utf-8")
        return payload

    # ── reading state back ──
    def token_file(self, profile: str, skill: str) -> Path:
        return self.root / profile / "skills" / skill / "scripts" / ".google_token.json"

    def exists(self, profile: str, skill: str, rel: str) -> bool:
        return (self.root / profile / "skills" / skill / rel).exists()

    def index(self, prefix: str) -> int:
        for position, entry in enumerate(self.events):
            if entry.startswith(prefix):
                return position
        raise AssertionError(f"no recorded event starting with {prefix!r} in {self.events}")


@pytest.fixture
def google(tmp_path, monkeypatch):
    harness = Harness(tmp_path)

    monkeypatch.setattr(
        "app.skills.sync.profile_skills_dir", lambda profile: tmp_path / profile / "skills"
    )

    async def fake_teardown(profile, base):
        harness.events.append(f"teardown:{base.name}")
        return dict(harness.teardown_result)

    def fake_stop_watch(spec, data, state):
        harness.events.append(f"watch_stop:{spec.dir_name}")
        channel = str((state or {}).get("channel_id") or "")
        if channel:
            harness.stopped_channels.append((spec.dir_name, channel))
        return harness.watch_result

    def fake_revoke(data):
        harness.events.append("revoke")
        harness.revoked_tokens.append(str(data.get("refresh_token") or ""))
        return harness.revoke_result

    real_delete = U._delete_files
    real_forget = U._forget

    def spy_delete(profile, spec):
        harness.events.append(f"delete:{spec.dir_name}")
        return real_delete(profile, spec)

    def spy_forget(profile):
        harness.events.append("forget")
        return real_forget(profile)

    monkeypatch.setattr(U, "_teardown_listener", fake_teardown)
    monkeypatch.setattr(U, "_stop_watch", fake_stop_watch)
    monkeypatch.setattr(U, "revoke_grant", fake_revoke)
    monkeypatch.setattr(U, "_delete_files", spy_delete)
    monkeypatch.setattr(U, "_forget", spy_forget)

    # Storage- and broker-backed lookups. Neutral here; tests that care override.
    monkeypatch.setattr(U, "skill_enabled", lambda profile, spec: True)
    monkeypatch.setattr(U, "_idle_subscriptions", lambda profile, spec: 0)
    monkeypatch.setattr(U, "_autostart_row_count", lambda profile, base: 0)
    monkeypatch.setattr(U, "_app_credential_present", lambda profile: False)
    monkeypatch.setattr(U, "_calendar_source", lambda profile: "app")
    monkeypatch.setattr(U, "_shared_client_id", lambda: "shared-cid")

    return harness
