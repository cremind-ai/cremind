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
    ClaudeCodeHostError,
    _as_int,
    credential_info,
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
    "description": (
        "Delegates all software-engineering work to Claude Code, Anthropic's "
        "autonomous coding agent, running in the current working directory. When "
        "enabled, use it for ANY task involving source code or a code project — "
        "reading, understanding, or explaining a codebase as much as creating, "
        "writing, refactoring, debugging, or testing code — instead of your own "
        "file/shell tools: start a task with run, poll long-running tasks with "
        "wait, and abort with stop."
    ),
    "default": False,
    "requires_feature": "claude_code",
    "required_config": {
        Var.MODEL: {
            "description": (
                "Claude model for coding tasks — pick from the account's live model "
                "list or type a model id (e.g. 'claude-sonnet-4-5') or an alias "
                "(sonnet, opus, haiku, opusplan). Empty = Claude Code's default model."
            ),
            "type": "string",
            "default": "",
            "dynamic_options": True,
        },
        Var.PERMISSION_MODE: {
            "description": (
                "Claude Code permission mode — pick from the installed Claude Agent "
                "SDK's live mode list (`cremind tools options claude_code`), the same "
                "modes the Claude Code CLI cycles through with Shift+Tab. "
                "'bypassPermissions' (the default) runs fully autonomously — the same "
                "trust level as the Shell Executor tool. 'acceptEdits' auto-approves "
                "file edits only; modes that pause for human approval (e.g. 'default') "
                "stall headless because no one is present to approve them."
            ),
            "type": "string",
            "default": "bypassPermissions",
            "dynamic_options": True,
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
                "Anthropic API key for Claude Code. Empty = fall back to the server "
                "environment (ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN), then the "
                "CLI's own login - this profile's `claude auth login`, else the "
                "server's shared one. A CLAUDE_CODE_OAUTH_TOKEN set below wins over "
                "this key, because that is which of the two the CLI itself uses. "
                "Independent of Settings -> LLM Providers: the "
                "Anthropic provider configured there is Cremind's own reasoning "
                "credential and is never used for coding tasks."
            ),
            "type": "string",
            "secret": True,
            "default": "",
        },
        Var.OAUTH_TOKEN: {
            "description": (
                "Long-lived Claude Code authentication token, from running `claude "
                "setup-token` on any machine that has a browser (it needs a Claude "
                "subscription) and pasting the result here. This is how you sign in "
                "on a server where no browser can be opened - `claude auth login` "
                "has no headless mode. The CLI prefers this token over an API key "
                "when both are set. Empty = fall back to the tiers below."
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


async def get_variable_options(
    *, variables: Dict[str, Any], profile: str, refresh: bool = False
) -> Dict[str, Any]:
    """Live option lists for ``dynamic_options`` variables (Settings dropdown +
    ``cremind tools options``). Module-level hook discovered by
    :func:`app.tools.builtin.get_builtin_variable_options_hook`.

    Returns ``{Var.MODEL: {...}, Var.PERMISSION_MODE: {...}}`` where each value is
    ``{"options": [{"id", "label"}...], "error": str|None, "source": str|None}``:

    - ``Var.MODEL`` — the account's models (from the Anthropic ``/v1/models`` API
      via the same credential chain the coding task uses) plus the CLI aliases.
    - ``Var.PERMISSION_MODE`` — the installed Claude Agent SDK's ``PermissionMode``
      Literal (introspected locally; ``refresh`` is a no-op for it).

    Never raises.
    """
    listing = await runner.list_models(variables, profile, force_refresh=refresh)
    options = [
        {"id": m["id"], "label": m.get("display_name") or m["id"]}
        for m in listing.get("models", [])
    ]
    if options:  # only offer aliases once the account list actually resolved
        options += [
            {"id": alias, "label": f"{alias} (alias)"} for alias in runner._MODEL_ALIASES
        ]

    modes = runner.list_permission_modes()
    return {
        Var.MODEL: {
            "options": options,
            "error": listing.get("error"),
            "source": listing.get("source"),
        },
        Var.PERMISSION_MODE: {
            "options": [
                {"id": m, "label": runner._PERMISSION_MODE_LABELS.get(m, m)}
                for m in modes.get("modes", [])
            ],
            "error": modes.get("error"),
            "source": modes.get("source"),
        },
    }


def _final_result(task) -> BuiltInToolResult:
    """Return the task's frozen final payload.

    Claude Code is a *delegated sub-agent* running on Anthropic — a separate account
    from Cremind's own reasoning model. Its token/cost usage is deliberately NOT folded
    into the turn's Cremind accounting (it would otherwise dwarf the turn's real cost
    and mis-attribute Anthropic spend to the parent model). It is surfaced only in the
    Agent Activity panel, which reads Claude Code's own ``total_cost_usd`` + context off
    the SDK stream. The model-visible ``usage`` field still rides ``structured_content``
    (``task.result``) so the delegating LLM can see it — it just isn't counted.
    """
    return BuiltInToolResult(structured_content=task.result)


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
        "agent working in the conversation's working directory. Use it for ALL "
        "coding work — including reading, understanding, and exploring existing "
        "code, explaining or reviewing a codebase, creating projects/apps, "
        "writing/refactoring/debugging code, and running and fixing tests. Write "
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
        except ClaudeCodeHostError as exc:
            # Not a failed task - a task that was never started. Nothing is in
            # the registry and no SDK client exists, because the binary the
            # client would spawn is precisely what cannot run here: starting it
            # would hang this coding task until the SDK's own timeout. The
            # advisory rides along whole so the model can quote the CPU model
            # and the hypervisor setting to the user instead of retrying.
            return BuiltInToolResult(structured_content={
                "error": "HostCannotRunClaudeCode",
                "message": exc.blocker["message"],
                "remediation": exc.blocker["remedy"],
                "host_advisory": exc.blocker,
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
        heartbeat: Dict[str, Any] = {
            "status": "running",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "working_directory": task.cwd,
            "elapsed_seconds": task.elapsed_seconds(),
            "effective_permission_mode": task.permission_mode,
            "message": (
                "Claude Code is working. Call claude_code__wait with this task_id to "
                "get the result; call claude_code__stop to abort."
            ),
        }
        advisory = runner._permission_advisory(task.permission_mode)
        if advisory is not None:
            heartbeat["permission_advisory"] = advisory
        return BuiltInToolResult(structured_content=heartbeat)


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
        heartbeat: Dict[str, Any] = {
            "status": "running",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "elapsed_seconds": task.elapsed_seconds(),
            "activity_events": task.activity.total_steps if task.activity else 0,
            "effective_permission_mode": task.permission_mode,
            "message": (
                "Still working (progress is streaming to the user's Claude Code "
                "panel). Call claude_code__wait again, or claude_code__stop to abort."
            ),
        }
        advisory = runner._permission_advisory(task.permission_mode)
        if advisory is not None:
            heartbeat["permission_advisory"] = advisory
        return BuiltInToolResult(structured_content=heartbeat)


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
        "Report whether Claude Code is ready to use, and list the Claude models "
        "available to the resolved account, WITHOUT starting a coding task. Shows "
        "whether the SDK is installed, which credential source is configured "
        "(the CLAUDE_CODE_OAUTH_TOKEN or CLAUDE_CODE_API_KEY tool variable, the "
        "server environment, or the `claude` CLI's own login: this profile's, or "
        "the server's shared one), "
        "which CLI home that login lives in, and the account's available `models`. "
        "Use it to answer 'is Claude Code set up?' AND 'which models can Claude "
        "Code use?'. For the full list plus how to change the model, run "
        "`cremind tools options claude_code` / `cremind tools set-var claude_code "
        "CLAUDE_CODE_MODEL=<id>` via the Shell Executor. Pass probe=true to ask "
        "the CLI itself who is signed in (local, unbilled, no file changes)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "probe": {
                "type": "boolean",
                "description": (
                    "When true, run `claude auth status` (and, for an API-key "
                    "credential, one unbilled model listing) to definitively confirm "
                    "Claude Code can authenticate. Local and free - use it to answer "
                    "'is Claude logged in?' for sure."
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
        info = credential_info(variables, profile)
        source = info["source"]
        configured = source is not None

        mode = variables.get(Var.PERMISSION_MODE) or "bypassPermissions"
        payload: Dict[str, Any] = {
            "available": True,
            "sdk_installed": True,
            "credential_source": source,
            # Always reported, credential or not: "which home does this profile
            # authenticate from?" is the question behind every confusing answer
            # here (a profile borrowing the server's login, a session that only
            # resumes under the home it was created in), and the model cannot
            # ask a follow-up question about a field that isn't there.
            "credential_scope": info["scope"],
            "cli_home": info["cli_home"],
            "account_hint": info["account_hint"],
            "credentials_configured": configured,
            "effective_permission_mode": mode,
        }
        # Surface a blocking permission mode BEFORE a coding task is even started,
        # so "is Claude Code set up?" reveals a mode that would stall coding work.
        advisory = runner._permission_advisory(mode)
        if advisory is not None:
            payload["permission_advisory"] = advisory
        # The same shape one step further out: a permission mode can stop a task
        # from changing anything, and this stops it from starting at all. Said
        # here because "is Claude Code set up?" is the question a user asks
        # BEFORE a coding task hangs, and because every credential answer below
        # is beside the point on a host that cannot run the binary - so
        # ``available`` flips to False and the message becomes the host's.
        blocker = runner.host_blocker(variables)
        if blocker is not None:
            payload["host_advisory"] = blocker
            payload["available"] = False
            payload["message"] = blocker["message"] + " " + blocker["remedy"]
        elif configured:
            scope_note = f", {info['scope']} scope" if info["scope"] else ""
            payload["message"] = (
                f"Claude Code is installed and a credential is configured "
                f"({source}{scope_note}, CLI home {info['cli_home']}). Pass probe=true "
                "to confirm it actually authenticates (local check, costs nothing)."
            )
        else:
            payload["message"] = (
                "Claude Code is installed, but no credential is visible to Cremind: "
                f"the CLI home {info['cli_home']} holds no login, there is no "
                "CLAUDE_CODE_OAUTH_TOKEN and no CLAUDE_CODE_API_KEY tool variable, "
                "and no key in the server "
                "environment. Pass probe=true to check for certain (on macOS the "
                "login lives in the Keychain, where Cremind cannot see it). "
                + runner._SIGN_IN_REMEDIATION
            )

        # List the account's available models (cached, never raises) so the agent
        # can answer "which models can Claude Code use?" without a separate tool.
        listing = await runner.list_models(variables, profile)
        payload["models"] = [
            {"id": m["id"], "display_name": m.get("display_name") or m["id"]}
            for m in listing.get("models", [])
        ]
        if listing.get("error"):
            payload["models_error"] = listing["error"]
        payload["models_hint"] = (
            "Full list + change flow: `cremind tools options claude_code`, then "
            "`cremind tools set-var claude_code CLAUDE_CODE_MODEL=<id>` "
            "(run via the Shell Executor tool)."
        )

        if arguments.get("probe"):
            # No working directory is prepared any more: the probe asks the CLI
            # who is signed in (and, for a key, lists models) rather than running
            # a real one-turn coding query, so there is nothing to run it *in*.
            result = await probe_auth(sdk, cwd="", variables=variables, profile=profile)
            payload["logged_in"] = result.get("logged_in")
            payload["probe_detail"] = result.get("detail")
            # The verdict is ABOUT a credential, and it is not always the one
            # `credential_info` guessed: that is Cremind's ranking, while the
            # probe asks `claude auth status` which credential the CLI would
            # really use. Overwrite the guess with the answer, or a card would
            # show "API key (tool variable)" next to a tick earned by the OAuth
            # login. `credential_verified` keeps the two claims apart - True
            # only when the API itself answered, so a held-but-expired login
            # cannot read as a working one.
            if result.get("credential_source") is not None:
                payload["credential_source"] = result["credential_source"]
            payload["credential_verified"] = result.get("credential_verified")
            # The account the CLI itself reported, not the hint read off disk:
            # on the one call that actually asked, say who the login belongs to.
            # Always present on a probe (None when there is nothing to report),
            # matching the Codex leaf so one card can render both.
            account = result.get("account")
            payload["account"] = account
            # On a blocked host the probe checked nothing - it refused before
            # asking the CLI and before validating any key - so the host message
            # stays and none of the three verdicts below is spoken. Both
            # ``logged_in`` and ``credential_verified`` are already None, which
            # is the "not checked" reading; sending the user off to sign in
            # again would be a wasted trip, and there is no login to fix.
            if blocker is None:
                if result.get("logged_in") is True:
                    who = (account or {}).get("email") or info["cli_home"]
                    payload["message"] = (
                        f"Claude Code is authenticated and ready to use ({who})."
                    )
                elif result.get("logged_in") is False:
                    payload["message"] = (
                        "Claude Code is NOT authenticated. "
                        + runner._SIGN_IN_REMEDIATION
                    )
                else:
                    payload["message"] = (
                        "Could not determine Claude Code's login status (this is not "
                        "the same as being signed out): "
                        + str(result.get("detail") or "the check did not complete.")
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
