"""Codex delegation built-in tool.

Delegates software-engineering work to Codex — OpenAI's autonomous coding agent —
driven through the OpenAI Codex SDK. The Codex counterpart of the ``claude_code``
built-in: when enabled it prefers handing coding-expertise tasks (creating
projects, writing/refactoring/debugging, reviewing/explaining code) to Codex
instead of editing files itself.

Disabled by default (``TOOL_CONFIG["default"] = False``): with the tool off,
Cremind keeps coding with its own file/shell tools. Enabling it requires the
``codex`` feature (the OpenAI Codex SDK, whose wheel bundles the codex binary) —
the enable pre-flight returns HTTP 409 ``FeatureNotInstalled`` until the feature
is installed.

A coding session can outlast ``MCP_TOOL_CALL_TIMEOUT``, so the work runs in a
background task (see :mod:`app.tools.builtin.codex_runner`). ``run`` starts it and
blocks for a short grace window (fast tasks finish in one call); longer tasks
return a ``task_id`` the model polls with ``wait`` and aborts with ``stop``. The
model only ever sees Codex's final result + stats — never its intermediate
reasoning, which streams exclusively to the user's Agent Activity panel.
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
from app.tools.builtin import codex_runner as runner
from app.tools.builtin.codex_runner import (
    Var,
    CodexConcurrencyError,
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

SERVER_NAME = "Codex"

_FEATURE_KEY = "codex"
_EXTRAS = ("codex",)


TOOL_CONFIG: ToolConfig = {
    "name": "codex",
    "display_name": "Codex",
    "description": (
        "Delegates all software-engineering work to Codex, OpenAI's autonomous "
        "coding agent, running in the current working directory. When enabled, "
        "use it for ANY task involving source code or a code project — reading, "
        "understanding, or explaining a codebase as much as creating, writing, "
        "refactoring, debugging, or testing code — instead of your own file/shell "
        "tools: start a task with run, poll long-running tasks with wait, and "
        "abort with stop."
    ),
    "default": False,
    "requires_feature": "codex",
    "required_config": {
        Var.MODEL: {
            "description": (
                "Codex model for coding tasks — pick from the account's live model "
                "list or type a model id (e.g. 'gpt-5.1-codex'). Empty = Codex's "
                "default model."
            ),
            "type": "string",
            "default": "",
            "dynamic_options": True,
        },
        Var.SANDBOX: {
            "description": (
                "Codex filesystem sandbox — pick from the installed Codex SDK's live "
                "list (`cremind tools options codex`). 'full-access' (the default) "
                "runs fully autonomously with no filesystem restrictions — the same "
                "trust level as the Shell Executor tool. 'workspace-write' confines "
                "edits and commands to the working directory; 'read-only' allows "
                "exploring/answering but no changes. Codex never pauses for approval "
                "(it runs headless), so the sandbox is the safety knob."
            ),
            "type": "string",
            "default": "full-access",
            "dynamic_options": True,
        },
        Var.REASONING_EFFORT: {
            "description": (
                "Reasoning effort for coding tasks (none, minimal, low, medium, "
                "high, xhigh). Empty = the model's default. Higher effort is slower "
                "and costs more tokens."
            ),
            "type": "string",
            "default": "",
        },
        Var.API_KEY: {
            "description": (
                "OpenAI API key for Codex. Empty = fall back to the profile's OpenAI "
                "LLM credentials, then the server environment (CODEX_API_KEY / "
                "OPENAI_API_KEY) or a host `codex login`. A key supplied here is "
                "installed into a Cremind-managed CODEX_HOME, never your own "
                "~/.codex."
            ),
            "type": "string",
            "secret": True,
            "default": "",
        },
        Var.BIN_PATH: {
            "description": (
                "Absolute path to an external codex binary. Empty = the SDK's "
                "bundled binary."
            ),
            "type": "string",
            "default": "",
        },
        Var.CONFIG_OVERRIDES: {
            "description": (
                "Comma-separated Codex `--config` overrides (e.g. "
                "'model_reasoning_effort=high, sandbox_mode=workspace-write'). "
                "Empty = none."
            ),
            "type": "string",
            "default": "",
        },
        Var.MAX_CONCURRENT_TASKS: {
            "description": (
                "Maximum Codex tasks running at once across all conversations. "
                "Default 2."
            ),
            "type": "number",
            "default": 2,
        },
    },
}


async def get_variable_options(
    *, variables: Dict[str, Any], profile: str, refresh: bool = False
) -> Dict[str, Any]:
    """Live option lists for ``dynamic_options`` variables (Settings dropdown +
    ``cremind tools options``). Module-level hook discovered by
    :func:`app.tools.builtin.get_builtin_variable_options_hook`.

    Returns ``{Var.MODEL: {...}, Var.SANDBOX: {...}}`` where each value is
    ``{"options": [{"id", "label"}...], "error": str|None, "source": str|None}``:

    - ``Var.MODEL`` — the account's models (from the Codex SDK's ``models()`` via
      the same credential chain the coding task uses).
    - ``Var.SANDBOX`` — the installed Codex SDK's ``Sandbox`` enum (introspected
      locally; ``refresh`` is a no-op for it).

    Never raises.
    """
    listing = await runner.list_models(variables, profile, force_refresh=refresh)
    options = [
        {"id": m["id"], "label": m.get("display_name") or m["id"]}
        for m in listing.get("models", [])
    ]

    modes = runner.list_sandbox_modes()
    return {
        Var.MODEL: {
            "options": options,
            "error": listing.get("error"),
            "source": listing.get("source"),
        },
        Var.SANDBOX: {
            "options": [
                {"id": m, "label": runner._SANDBOX_LABELS.get(m, m)}
                for m in modes.get("modes", [])
            ],
            "error": modes.get("error"),
            "source": modes.get("source"),
        },
    }


def _final_result(task) -> BuiltInToolResult:
    """Return the task's frozen final payload.

    Codex is a *delegated sub-agent* running on OpenAI — a separate account from
    Cremind's own reasoning model. Its token/cost usage is deliberately NOT folded into
    the turn's Cremind accounting; it is surfaced only in the Agent Activity panel,
    which reads context off the SDK stream (the Codex SDK reports no cost). The
    model-visible ``usage`` field still rides ``structured_content`` (``task.result``)
    so the delegating LLM can see it — it just isn't counted.
    """
    return BuiltInToolResult(structured_content=task.result)


def _missing_sdk(detail: str) -> BuiltInToolResult:
    return missing_dependency_result(
        tool="codex",
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
            f"No Codex task with id '{task_id}'. It may have finished and been "
            "cleaned up, or the server restarted (which kills running tasks). If you "
            "have a session_id, resume the coding session with codex__run."
        ),
    })


def _wait_cap() -> float:
    return max(5.0, float(BaseConfig.MCP_TOOL_CALL_TIMEOUT or 300) - runner._WAIT_MARGIN_SECONDS)


class CodexRunTool(BuiltInTool):
    name: str = "run"
    description: str = (
        "Start a Codex coding task — an expert autonomous software-engineering "
        "agent (OpenAI Codex) working in the conversation's working directory. Use "
        "it for ALL coding work — including reading, understanding, and exploring "
        "existing code, explaining or reviewing a codebase, creating projects/apps, "
        "writing/refactoring/debugging code, and running and fixing tests. Write "
        "'prompt' as a complete task brief (goal, constraints, relevant paths) — "
        "Codex sees only that text plus the working directory, not this "
        "conversation. If the task finishes within the grace window the final result "
        "is returned directly; otherwise you get status 'running' with a task_id — "
        "call codex__wait with it until completion. Pass session_id (a Codex thread "
        "id from a previous result) to CONTINUE that coding session with a "
        "follow-up. Only one task runs per conversation at a time."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The full task brief for Codex: what to build/fix/review, "
                    "constraints, and relevant file paths. Be complete — it sees only "
                    "this text plus the working directory."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "OPTIONAL. A session_id (Codex thread id) returned by a previous "
                    "codex result. Resumes that coding session so Codex keeps its full "
                    "prior context. Leave empty to start fresh."
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
                    "OPTIONAL Codex model override for this task. Default: the "
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
        except CodexConcurrencyError as exc:
            return BuiltInToolResult(structured_content={
                "error": exc.code,
                "message": exc.message,
                "task_id": exc.running_task_id,
            })
        except RuntimeError as exc:
            return _missing_sdk(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("codex: start_task failed")
            return BuiltInToolResult(structured_content={
                "error": "CodexError",
                "message": f"Failed to start Codex: {exc}",
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
                "Codex is working. Call codex__wait with this task_id to get the "
                "result; call codex__stop to abort."
            ),
        })


class CodexWaitTool(BuiltInTool):
    name: str = "wait"
    description: str = (
        "Wait for a running Codex task to finish and return its final result. "
        "Long-polls up to 'timeout' seconds (default 120); if still running it "
        "returns a status 'running' heartbeat — immediately call codex__wait again "
        "with the same task_id (no sleeping needed). Returns the completed result "
        "as soon as it is available."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task_id returned by codex__run.",
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
                "Still working (progress is streaming to the user's Codex panel). "
                "Call codex__wait again, or codex__stop to abort."
            ),
        })


class CodexStopTool(BuiltInTool):
    name: str = "stop"
    description: str = (
        "Stop a running Codex task. Interrupts it gracefully (the session is "
        "preserved and can be resumed later via session_id), force-cancelling if it "
        "does not stop promptly."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task_id returned by codex__run.",
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


class CodexStatusTool(BuiltInTool):
    name: str = "status"
    description: str = (
        "Report whether Codex is ready to use, and list the Codex models available "
        "to the resolved account — WITHOUT starting a coding task. Shows whether the "
        "SDK is installed, which OpenAI credential source is configured (tool "
        "variable, this profile's LLM settings, the server environment, or a host "
        "`codex login`), and the account's available `models`. Use it to answer 'is "
        "Codex set up?' AND 'which models can Codex use?'. For the full list plus how "
        "to change the model, run `cremind tools options codex` / `cremind tools "
        "set-var codex CODEX_MODEL=<id>` via the Shell Executor. Pass probe=true to "
        "check the active account credential (no coding task, no token spend)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "probe": {
                "type": "boolean",
                "description": (
                    "When true, read the active Codex account to confirm a "
                    "credential is present (no coding task, no edits, no token "
                    "spend). Use this to answer 'is Codex logged in?'."
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
                    "The codex feature (OpenAI Codex SDK) is not installed. "
                    "Install it with: cremind features install codex."
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
                f"Codex is installed and a credential is configured ({source}). "
                "Pass probe=true to confirm the account credential is active."
            )
        else:
            payload["message"] = (
                "Codex is installed, but no OpenAI credential is visible to Cremind "
                "(no CODEX_API_KEY tool variable, no OpenAI provider in this "
                "profile's LLM settings, no key in the server environment, and no "
                "host `codex login`). Pass probe=true to check for certain."
            )

        # List the account's available models (cached, never raises) so the agent
        # can answer "which models can Codex use?" without a separate tool.
        listing = await runner.list_models(variables, profile)
        payload["models"] = [
            {"id": m["id"], "display_name": m.get("display_name") or m["id"]}
            for m in listing.get("models", [])
        ]
        if listing.get("error"):
            payload["models_error"] = listing["error"]
        payload["models_hint"] = (
            "Full list + change flow: `cremind tools options codex`, then "
            "`cremind tools set-var codex CODEX_MODEL=<id>` "
            "(run via the Shell Executor tool)."
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
                payload["message"] = "Codex has an active account credential and is ready to use."
            elif result.get("logged_in") is False:
                payload["message"] = (
                    "Codex is NOT authenticated. Set the CODEX_API_KEY tool variable, "
                    "configure the OpenAI provider under Settings → LLM, or run `codex "
                    "login` on the server host."
                )
            else:
                payload["message"] = (
                    "Could not determine Codex's login status: "
                    + str(result.get("detail") or "the check did not complete.")
                )
        return BuiltInToolResult(structured_content=payload)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return the Codex leaves. Config/variables arrive per-call via
    ``arguments['_variables']`` (the adapter injects them), so no constructor
    wiring is needed here."""
    return [
        CodexRunTool(),
        CodexWaitTool(),
        CodexStopTool(),
        CodexStatusTool(),
    ]
