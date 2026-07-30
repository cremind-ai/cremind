"""Turn Drive's per-file access failures into instructions the agent can act on.

Under ``drive.file`` a file the user has not granted is indistinguishable from a
file that does not exist — Google answers 404 either way, and 403 for a file that
exists but is out of scope. The raw ``HttpError`` traceback tells an agent nothing
about the fix, so every by-id verb funnels failures through here.

The remedy differs by context, and getting that wrong is expensive:

* Interactive turn — run ``grant``, surface the printed URL, wait for the user.
* Event / scheduled run — **never** run ``grant``. Nobody is present to complete
  consent, and the shell tool's default timeout is far shorter than the consent
  wait, so the command would be backgrounded and abandoned while the run stalls.
  Notify the user and stop instead.

Both are emitted on every error so the agent picks by its own context rather than
guessing from a single generic hint.
"""
from __future__ import annotations

import json
import sys
from typing import Any

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
LEGACY_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

# Distinct from the exit code used for auth failures, so callers can tell
# "not linked" from "linked but this file was never granted".
EXIT_NOT_GRANTED = 3


def http_status(exc: Exception) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def not_granted_payload(
    *, file_id: str, status: int | None, stale_scopes: bool = False
) -> dict[str, Any]:
    """Build the structured error for an unreachable file."""
    payload: dict[str, Any] = {
        "error": "drive_file_not_granted",
        "file_id": file_id,
        "http_status": status,
        "message": (
            f"Cremind cannot access Drive file '{file_id}'. Cremind holds per-file "
            "Drive access (the drive.file scope), so it can only reach files the user "
            "explicitly granted through the Google file picker, plus files Cremind "
            "itself created. A 404 here means 'not granted to Cremind' OR 'does not "
            "exist' — Google does not distinguish the two."
        ),
        "interactive_fix": (
            "If a user is present in this conversation: run "
            f"`uv run scripts/__main__.py grant --file {file_id}`, show them the URL it "
            "prints, and ask them to pick and approve that file. Then retry the command."
        ),
        "unattended_fix": (
            "If this is an event, scheduled, or otherwise unattended run: do NOT run "
            "`grant` — no one can complete the browser consent and the run would stall. "
            f"Send the user a notification asking them to grant access to '{file_id}' "
            "(Settings -> Google Drive in the web UI, or `cremind drive grant --file "
            f"{file_id}`), then stop this run."
        ),
    }
    if stale_scopes:
        payload["scopes_stale"] = True
        payload["message"] += (
            " This account is still linked with the old whole-Drive scope, which is no "
            "longer issued. Re-link first (`uv run scripts/__main__.py link`), then grant "
            "the file."
        )
    return payload


def emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
    return EXIT_NOT_GRANTED


def scopes_are_stale(granted: list[str] | None) -> bool:
    """True when a linked account predates the per-file migration."""
    scopes = set(granted or [])
    return LEGACY_DRIVE_SCOPE in scopes and DRIVE_FILE_SCOPE not in scopes
