"""Import-session state: the wizard's server-side, restart-tolerant state.

One import is a staged session under ``<sys>/blueprints/staging/<id>/``:

- ``upload.blueprint`` — the raw uploaded archive
- ``payload/``        — the extracted manifest + component docs + skill trees
- ``session.json``    — the state machine (this module), rewritten atomically
                        after every step so a browser refresh or a server
                        restart resumes exactly where it stopped.

**Secrets are never written to ``session.json``.** API keys / skill env vars
arrive in a step-apply request body, are applied to storage in that same
request, and discarded. The session records only *which* requirements were
satisfied or skipped, never their values.

At most one non-terminal session exists per server (import creates a profile and
spawns OS processes — two interleaved wizards are not worth supporting).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.blueprint.store import session_dir, staging_root

# Session lifecycle states.
STATE_STAGED = "staged"
STATE_APPLYING = "applying"
STATE_DONE = "done"
STATE_ABORTED = "aborted"
STATE_FAILED = "failed"
TERMINAL_STATES = frozenset({STATE_DONE, STATE_ABORTED, STATE_FAILED})

# Per-step statuses.
STEP_PENDING = "pending"
STEP_APPLIED = "applied"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"


@dataclass
class ImportSession:
    id: str
    owner: str
    created_at: float
    updated_at: float
    state: str
    manifest: dict[str, Any]
    plan: list[dict[str, Any]] = field(default_factory=list)
    target_profile: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def dir(self) -> Path:
        return session_dir(self.id)

    @property
    def payload_dir(self) -> Path:
        return self.dir / "payload"

    def step(self, key: str) -> dict[str, Any] | None:
        for s in self.steps:
            if s.get("key") == key:
                return s
        return None

    def step_status(self, key: str) -> str:
        s = self.step(key)
        return s.get("status", STEP_PENDING) if s else STEP_PENDING

    def ordered_step_keys(self) -> list[str]:
        return [s["key"] for s in self.steps]

    def previous_incomplete(self, key: str) -> str | None:
        """The first step before ``key`` that is not applied/skipped, else None."""
        for s in self.steps:
            if s["key"] == key:
                return None
            if s.get("status") not in (STEP_APPLIED, STEP_SKIPPED):
                return s["key"]
        return None

    def set_step_result(self, key: str, status: str, result: dict[str, Any]) -> None:
        s = self.step(key)
        if s is None:
            s = {"key": key, "status": STEP_PENDING, "requirements": [], "result": {}}
            self.steps.append(s)
        s["status"] = status
        s["result"] = result

    def add_warning(self, message: str, *, kind: str = "info", **extra: Any) -> None:
        self.warnings.append({"kind": kind, "message": message, **extra})

    # ── persistence (atomic) ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """The session as the API/UI sees it (manifest kept as its summary)."""
        d = self.to_dict()
        return d

    def save(self) -> None:
        self.updated_at = time.time()
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "session.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, session_id: str) -> "ImportSession | None":
        try:
            path = session_dir(session_id) / "session.json"
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return cls(
            id=d["id"],
            owner=d.get("owner", ""),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            state=d.get("state", STATE_STAGED),
            manifest=d.get("manifest") or {},
            plan=d.get("plan") or [],
            target_profile=d.get("target_profile"),
            steps=d.get("steps") or [],
            warnings=d.get("warnings") or [],
            report=d.get("report"),
        )


def find_active_session() -> ImportSession | None:
    """Return the single non-terminal session, if one exists."""
    root = staging_root()
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        sess = ImportSession.load(child.name)
        if sess is not None and sess.state not in TERMINAL_STATES:
            return sess
    return None


__all__ = [
    "STATE_ABORTED",
    "STATE_APPLYING",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_STAGED",
    "STEP_APPLIED",
    "STEP_FAILED",
    "STEP_PENDING",
    "STEP_SKIPPED",
    "TERMINAL_STATES",
    "ImportSession",
    "find_active_session",
]
