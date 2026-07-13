"""Claude Code delegation built-in tool.

Delegates software-engineering work to Claude Code — Anthropic's autonomous
coding agent — driven through the Claude Agent SDK. Cremind is a general-purpose
harness, so when this tool is enabled it prefers handing coding-expertise tasks
(creating projects, writing/refactoring/debugging, reviewing/explaining code) to
Claude Code instead of editing files itself.

Disabled by default (``TOOL_CONFIG["default"] = False``): with the tool off,
Cremind keeps coding with its own file/shell tools. Enabling it requires the
``claude_code`` feature (the Claude Agent SDK, whose wheel bundles the Claude
Code CLI binary) — the enable pre-flight returns HTTP 409 ``FeatureNotInstalled``
until the feature is installed.

A coding session can outlast ``MCP_TOOL_CALL_TIMEOUT``, so the work runs in a
background task (see :mod:`app.tools.builtin.claude_code_runner`). ``run`` starts
it and blocks for a short grace window (fast tasks finish in one call); longer
tasks return a ``task_id`` the model polls with ``wait`` and aborts with
``stop``. The model only ever sees Claude Code's final result + stats — never its
intermediate reasoning, which streams exclusively to the user's Agent Activity
panel.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from app.config.settings import BaseConfig, get_user_working_directory
from app.tools.builtin.base import (
    BuiltInTool,
    BuiltInToolResult,
    missing_dependency_result,
)
from app.tools.builtin import claude_code_runner as runner
from app.tools.builtin.claude_code_runner import (
    Var,
    ClaudeCodeConcurrencyError,
    _as_int,
    credential_source,
    get_task,
    known_task_ids,
    load_sdk,
    merge_variables,
    probe_auth,
    start_task,
    stop_task,
    wait_for_task,
)
from app.types import ToolConfig
from app.utils.logger import logger

SERVER_NAME = "Claude Code"

_FEATURE_KEY = "claude_code"
_EXTRAS = ("claude-code",)


TOOL_CONFIG: ToolConfig = {
    "name": "claude_code",
    "display_name": "Claude Code",
    "default": False,
    "requires_feature": "claude_code",
    "required_config": {
        Var.MODEL: {
            "description": (
                "Claude model override for coding tasks (e.g. 'claude-sonnet-4-5'). "
                "Empty = Claude Code's default model."
            ),
            "type": "string",
            "default": "",
        },
        Var.PERMISSION_MODE: {
            "description": (
                "Claude Code permission mode. 'bypassPermissions' runs fully "
                "autonomously (recommended headless — same trust level as the Shell "
                "Executor tool). 'acceptEdits' auto-approves file edits only; other "
                "actions may be denied because no human is present to approve them."
            ),
            "type": "string",
            "enum": ["bypassPermissions", "acceptEdits", "default", "plan"],
            "default": "bypassPermissions",
        },
        Var.MAX_TURNS: {
            "description": "Maximum agent turns per task. 0 = unlimited.",
            "type": "number",
            "default": 0,
        },
        Var.MAX_BUDGET_USD: {
            "description": "Maximum API spend (USD) per task. 0 = unlimited.",
            "type": "number",
            "default": 0,
        },
        Var.API_KEY: {
            "description": (
                "Anthropic API key for Claude Code. Empty = fall back to the "
                "profile's Anthropic LLM credentials, then the server environment "
                "or `claude login`."
            ),
            "type": "string",
            "secret": True,
            "default": "",
        },
        Var.CLI_PATH: {
            "description": (
                "Absolute path to an external Claude Code CLI binary. Empty = the "
                "SDK's bundled CLI."
            ),
            "type": "string",
            "default": "",
        },
        Var.ALLOWED_TOOLS: {
            "description": (
                "Comma-separated allowlist of Claude Code tools (e.g. "
                "'Read,Edit,Bash'). Empty = all standard tools."
            ),
            "type": "string",
            "default": "",
        },
        Var.DISALLOWED_TOOLS: {
            "description": (
                "Comma-separated denylist of Claude Code tools. Empty = none denied."
            ),
            "type": "string",
            "default": "",
        },
        Var.MAX_CONCURRENT_TASKS: {
            "description": (
                "Maximum Claude Code tasks running at once across all conversations. "
                "Default 2."
            ),
            "type": "number",
            "default": 2,
        },
    },
}


def _final_result(task) -> BuiltInToolResult:
    """Return the task's frozen final payload, folding token usage exactly once."""
    usage = None
    if task.token_usage and not task.token_usage_reported:
        usage = task.token_usage
        task.token_usage_reported = True
    return BuiltInToolResult(structured_content=task.result, token_usage=usage)


def _missing_sdk(detail: str) -> BuiltInToolResult:
    return missing_dependency_result(
        tool="claude_code",
        feature_key=_FEATURE_KEY,
        extras=_EXTRAS,
        detail=detail,
    )


def _task_not_found(task_id: str) -> BuiltInToolResult:
    return BuiltInToolResult(structured_content={
        "error": "TaskNotFound",
        "task_id": task_id,
        "known_task_ids": known_task_ids(),
        "message": (
            f"No Claude Code task with id '{task_id}'. It may have finished and been "
            "cleaned up, or the server restarted (which kills running tasks). If you "
            "have a session_id, resume the coding session with claude_code__run."
        ),
    })


def _wait_cap() -> float:
    return max(5.0, float(BaseConfig.MCP_TOOL_CALL_TIMEOUT or 300) - runner._WAIT_MARGIN_SECONDS)


class ClaudeCodeRunTool(BuiltInTool):
    name: str = "run"
    description: str = (
        "Start a Claude Code coding task — an expert autonomous software-engineering "
        "agent working in the conversation's working directory. Use it for "
        "coding-expertise work: creating projects/apps, writing/refactoring/debugging "
        "code, reviewing or explaining a codebase, running and fixing tests. Write "
        "'prompt' as a complete task brief (goal, constraints, relevant paths) — "
        "Claude Code sees only that text plus the working directory, not this "
        "conversation. If the task finishes within the grace window the final result "
        "is returned directly; otherwise you get status 'running' with a task_id — "
        "call claude_code__wait with it until completion. Pass session_id (from a "
        "previous result) to CONTINUE that coding session with a follow-up. Only one "
        "task runs per conversation at a time."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The full task brief for Claude Code: what to build/fix/review, "
                    "constraints, and relevant file paths. Be complete — it sees only "
                    "this text plus the working directory."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "OPTIONAL. A session_id returned by a previous claude_code result. "
                    "Resumes that coding session so Claude Code keeps its full prior "
                    "context. Leave empty to start fresh."
                ),
            },
            "working_directory": {
                "type": "string",
                "description": (
                    "OPTIONAL absolute path override. Default: the conversation's "
                    "current working directory. When resuming a session, use the same "
                    "directory it was started in."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "OPTIONAL Claude model override for this task. Default: the "
                    "configured/default model."
                ),
            },
        },
        "required": ["prompt"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        sdk, err = load_sdk()
        if sdk is None:
            return _missing_sdk(err or "")

        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return BuiltInToolResult(structured_content={
                "error": "MissingParameter",
                "message": "'prompt' is required.",
            })

        raw_cwd = (
            arguments.get("working_directory")
            or arguments.get("_working_directory")
            or get_user_working_directory()
        )
        cwd = os.path.abspath(os.path.expanduser(str(raw_cwd)))
        try:
            os.makedirs(cwd, exist_ok=True)
        except OSError as exc:
            return BuiltInToolResult(structured_content={
                "error": "WorkingDirectoryError",
                "message": f"Could not use working directory '{cwd}': {exc}",
            })

        profile = arguments.get("_profile") or "default"
        context_id = arguments.get("_context_id") or ""
        session_id = str(arguments.get("session_id") or "").strip() or None
        model = str(arguments.get("model") or "").strip() or None
        variables = merge_variables(arguments.get("_variables"))

        try:
            task = await start_task(
                prompt=prompt,
                cwd=cwd,
                profile=profile,
                context_id=context_id,
                variables=variables,
                session_id=session_id,
                model=model,
            )
        except ClaudeCodeConcurrencyError as exc:
            return BuiltInToolResult(structured_content={
                "error": exc.code,
                "message": exc.message,
                "task_id": exc.running_task_id,
            })
        except RuntimeError as exc:
            return _missing_sdk(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("claude_code: start_task failed")
            return BuiltInToolResult(structured_content={
                "error": "ClaudeCodeError",
                "message": f"Failed to start Claude Code: {exc}",
            })

        grace = min(runner._RUN_GRACE_SECONDS, _wait_cap())
        if await wait_for_task(task, grace):
            return _final_result(task)
        return BuiltInToolResult(structured_content={
            "status": "running",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "working_directory": task.cwd,
            "elapsed_seconds": task.elapsed_seconds(),
            "message": (
                "Claude Code is working. Call claude_code__wait with this task_id to "
                "get the result; call claude_code__stop to abort."
            ),
        })


class ClaudeCodeWaitTool(BuiltInTool):
    name: str = "wait"
    description: str = (
        "Wait for a running Claude Code task to finish and return its final result. "
        "Long-polls up to 'timeout' seconds (default 120); if still running it "
        "returns a status 'running' heartbeat — immediately call claude_code__wait "
        "again with the same task_id (no sleeping needed). Returns the completed "
        "result as soon as it is available."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task_id returned by claude_code__run.",
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "OPTIONAL seconds to block waiting for completion. Default 120."
                ),
            },
        },
        "required": ["task_id"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            return BuiltInToolResult(structured_content={
                "error": "MissingParameter",
                "message": "'task_id' is required.",
            })
        task = get_task(task_id)
        if task is None:
            return _task_not_found(task_id)
        if task.done.is_set():
            return _final_result(task)

        raw_timeout = arguments.get("timeout")
        timeout = _as_int(raw_timeout) if raw_timeout else runner._WAIT_DEFAULT_SECONDS
        wait_s = min(float(timeout) or runner._WAIT_DEFAULT_SECONDS, _wait_cap())
        if await wait_for_task(task, wait_s):
            return _final_result(task)
        return BuiltInToolResult(structured_content={
            "status": "running",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "elapsed_seconds": task.elapsed_seconds(),
            "activity_events": task.activity.total_steps if task.activity else 0,
            "message": (
                "Still working (progress is streaming to the user's Claude Code "
                "panel). Call claude_code__wait again, or claude_code__stop to abort."
            ),
        })


class ClaudeCodeStopTool(BuiltInTool):
    name: str = "stop"
    description: str = (
        "Stop a running Claude Code task. Interrupts it gracefully (the session is "
        "preserved and can be resumed later via session_id), force-cancelling if it "
        "does not stop promptly."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task_id returned by claude_code__run.",
            },
        },
        "required": ["task_id"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            return BuiltInToolResult(structured_content={
                "error": "MissingParameter",
                "message": "'task_id' is required.",
            })
        task = get_task(task_id)
        if task is None:
            return _task_not_found(task_id)
        if task.done.is_set():
            return _final_result(task)

        await stop_task(task)
        if task.status in ("completed", "failed") and task.result:
            return _final_result(task)
        return BuiltInToolResult(structured_content=task.result or {
            "status": task.status,
            "task_id": task.task_id,
            "session_id": task.session_id,
        })


class ClaudeCodeStatusTool(BuiltInTool):
    name: str = "status"
    description: str = (
        "Report whether Claude Code is ready to use — WITHOUT starting a coding "
        "task. Shows whether the SDK is installed and which Anthropic credential "
        "source is configured (tool variable, this profile's LLM settings, or the "
        "server environment). Use it to answer questions like 'is Claude Code set "
        "up?'. Note: the server host may also be authenticated via `claude login`, "
        "which Cremind cannot see directly — pass probe=true to run a tiny live "
        "check that definitively confirms whether Claude Code is logged in and "
        "authenticated (one minimal API call, no file changes)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "probe": {
                "type": "boolean",
                "description": (
                    "When true, run a minimal live check to definitively confirm "
                    "Claude Code can authenticate (a single tiny request, no tools, "
                    "no edits). Use this to answer 'is Claude logged in?' for sure."
                ),
            },
        },
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        sdk, err = load_sdk()
        if sdk is None:
            return BuiltInToolResult(structured_content={
                "available": False,
                "sdk_installed": False,
                "message": (
                    "The claude_code feature (Claude Agent SDK) is not installed. "
                    "Install it with: cremind features install claude_code."
                ),
                "detail": err or "",
            })

        profile = arguments.get("_profile") or "default"
        variables = merge_variables(arguments.get("_variables"))
        source = credential_source(variables, profile)
        configured = source is not None

        payload: Dict[str, Any] = {
            "available": True,
            "sdk_installed": True,
            "credential_source": source,
            "credentials_configured": configured,
        }
        if configured:
            payload["message"] = (
                f"Claude Code is installed and a credential is configured "
                f"({source}). Pass probe=true to confirm it actually authenticates."
            )
        else:
            payload["message"] = (
                "Claude Code is installed, but no Anthropic credential is visible to "
                "Cremind (no CLAUDE_CODE_API_KEY tool variable, no Anthropic provider "
                "in this profile's LLM settings, and no key in the server environment). "
                "It may still be logged in via `claude login` on the server host — pass "
                "probe=true to check for certain."
            )

        if arguments.get("probe"):
            raw_cwd = arguments.get("_working_directory") or get_user_working_directory()
            cwd = os.path.abspath(os.path.expanduser(str(raw_cwd)))
            try:
                os.makedirs(cwd, exist_ok=True)
            except OSError:
                pass
            result = await probe_auth(sdk, cwd=cwd, variables=variables, profile=profile)
            payload["logged_in"] = result.get("logged_in")
            payload["probe_detail"] = result.get("detail")
            if result.get("logged_in") is True:
                payload["message"] = "Claude Code is authenticated and ready to use."
            elif result.get("logged_in") is False:
                payload["message"] = (
                    "Claude Code is NOT authenticated. Set the CLAUDE_CODE_API_KEY tool "
                    "variable, configure the Anthropic provider under Settings → LLM, or "
                    "run `claude login` on the server host."
                )
            else:
                payload["message"] = (
                    "Could not determine Claude Code's login status: "
                    + str(result.get("detail") or "the live check did not complete.")
                )
        return BuiltInToolResult(structured_content=payload)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return the Claude Code leaves. Config/variables arrive per-call via
    ``arguments['_variables']`` (the adapter injects them), so no constructor
    wiring is needed here."""
    return [
        ClaudeCodeRunTool(),
        ClaudeCodeWaitTool(),
        ClaudeCodeStopTool(),
        ClaudeCodeStatusTool(),
    ]
