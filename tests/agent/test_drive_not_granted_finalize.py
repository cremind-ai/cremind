"""An unattended run that hits an ungranted Drive file must finalize as failed.

Under per-file Drive access an automation can meet a file nobody granted. The
agent is told to notify and stop rather than open a consent URL no one is present
to complete — which, before this, left the run reporting a normal-priority
success. The runner now recognises the skill's structured error itself.

The stderr fixtures are built from the gdrive skill's own ``not_granted_payload``
so a rename there breaks these tests rather than silently disarming detection.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import app.agent.stream_runner as sr

# Loaded by path, not import: the skill ships its own top-level ``app`` package,
# which would collide with the backend's.
_GDRIVE_ERRORS = (
    Path(__file__).resolve().parents[2]
    / "app" / "skills" / "builtin" / "gdrive" / "scripts" / "app" / "errors.py"
)


def _skill_errors():
    spec = importlib.util.spec_from_file_location("_gdrive_errors", _GDRIVE_ERRORS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(file_id: str = "F1", status: int = 404) -> str:
    """The exact JSON the gdrive skill prints to stderr for an ungranted file."""
    payload = _skill_errors().not_granted_payload(file_id=file_id, status=status)
    return json.dumps(payload, indent=2)


def test_the_detector_matches_the_skills_exit_code():
    assert _skill_errors().EXIT_NOT_GRANTED == 3


def _data_part(**data) -> dict:
    return {"kind": "data", "data": data}


# --- detection ---


def test_the_skills_payload_is_detected_and_names_the_file():
    parts = [_data_part(stdout="", stderr=_payload("ABC123"), return_code=3)]
    message = sr._detect_drive_not_granted(parts)
    assert message is not None
    assert "ABC123" in message
    assert "cremind drive grant --file ABC123" in message


def test_a_successful_command_that_merely_mentions_the_marker_is_not_a_failure():
    # An agent reading gdrive's own errors.py or SKILL.md in an event run would
    # echo the marker; the exit code is what separates that from a real failure.
    parts = [_data_part(stdout="", stderr=_payload(), return_code=0)]
    assert sr._detect_drive_not_granted(parts) is None


def test_a_missing_return_code_is_not_a_failure():
    parts = [_data_part(stdout="", stderr=_payload())]
    assert sr._detect_drive_not_granted(parts) is None


def test_a_clean_observation_detects_nothing():
    parts = [_data_part(stdout="ok", stderr="", return_code=0)]
    assert sr._detect_drive_not_granted(parts) is None


def test_the_marker_on_stdout_alone_is_not_a_failure():
    parts = [_data_part(stdout=_payload(), stderr="", return_code=3)]
    assert sr._detect_drive_not_granted(parts) is None


def test_a_text_part_carrying_the_marker_is_detected():
    parts = [{"kind": "text", "text": _payload("XYZ")}]
    message = sr._detect_drive_not_granted(parts)
    assert message is not None
    assert "XYZ" in message


def test_a_payload_without_a_file_id_still_produces_advice():
    parts = [_data_part(stdout="", stderr='{"error": "drive_file_not_granted"}', return_code=3)]
    message = sr._detect_drive_not_granted(parts)
    assert message is not None
    assert "a Google Drive file" in message
    assert "--file <id>" in message


def test_junk_parts_are_tolerated():
    assert sr._detect_drive_not_granted([None, "nope", 3, {}, {"data": "str"}]) is None


def test_the_first_ungranted_file_wins():
    parts = [
        _data_part(stdout="", stderr=_payload("FIRST"), return_code=3),
        _data_part(stdout="", stderr=_payload("SECOND"), return_code=3),
    ]
    assert "FIRST" in sr._detect_drive_not_granted(parts)


# --- status ladder ---


def _status(**over):
    kwargs = dict(
        cancelled=False, errored=False, pending_question=None,
        todos=[], drive_not_granted_error=None,
    )
    kwargs.update(over)
    return sr._event_run_final_status(**kwargs)


def test_an_ungranted_file_fails_the_run():
    status, _ = _status(drive_not_granted_error="grant it")
    assert status == "failed"


def test_a_clean_run_still_completes():
    assert _status()[0] == "completed"


def test_cancelled_outranks_the_ungranted_file():
    assert _status(cancelled=True, drive_not_granted_error="x")[0] == "cancelled"


def test_a_real_exception_outranks_the_ungranted_file():
    assert _status(errored=True, drive_not_granted_error="x")[0] == "failed"


def test_a_pending_question_survives_the_ungranted_file():
    # Pending keeps a live continuation channel: the user can grant the file and
    # reply to resume. Failing here would clear that and strand the work.
    status, question = _status(pending_question="which one?", drive_not_granted_error="x")
    assert status == "pending"
    assert question == "which one?"


def test_incomplete_todos_stay_pending_despite_the_ungranted_file():
    todos = [{"status": "completed"}, {"status": "pending"}]
    status, question = _status(todos=todos, drive_not_granted_error="x")
    assert status == "pending"
    assert "1 of 2 tasks completed" in question


def test_completed_todos_allow_the_ungranted_file_to_fail_the_run():
    todos = [{"status": "completed"}, {"status": "completed"}]
    assert _status(todos=todos, drive_not_granted_error="x")[0] == "failed"


def test_completed_todos_with_no_problem_complete():
    todos = [{"status": "completed"}]
    assert _status(todos=todos)[0] == "completed"
