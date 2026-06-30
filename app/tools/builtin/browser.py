"""Browser built-in tool.

Controls a Chrome browser via Chrome DevTools Protocol (CDP) using Playwright.
Provides a single unified ``BrowserTool`` with an ``action`` parameter that
dispatches to the correct handler in code (navigate, snapshot, screenshot,
click, type, tabs, evaluate).

By default the tool launches the system-installed Google Chrome with a
persistent profile under ``<CREMIND_SYSTEM_DIR>/browser-profile`` so cookies
and logins survive between sessions. To connect to an already-running browser
instead, set the ``BROWSER_CDP_URL`` config variable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from typing import Any, Dict, List, Optional

from app.types import ToolConfig

import httpx

from app.config.settings import BaseConfig
from app.tools.builtin.base import (
    BuiltInTool,
    BuiltInToolResult,
    missing_dependency_result,
    restart_required_result,
)
from app.types import ToolResultFile, ToolResultWithFiles
from app.utils.logger import logger

try:
    from playwright.async_api import (
        async_playwright,
        Browser,
        BrowserContext,
        Page,
        Playwright,
    )
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module exports (required by the built-in tool registration system)
# ---------------------------------------------------------------------------

SERVER_NAME = "Browser"

class Var:
    """Variable keys for the Browser tool (used in TOOL_CONFIG and runtime reads)."""
    CDP_URL = "BROWSER_CDP_URL"
    HEADLESS = "BROWSER_HEADLESS"
    CHANNEL = "BROWSER_CHANNEL"
    USER_DATA_DIR = "BROWSER_USER_DATA_DIR"
    EXECUTABLE_PATH = "BROWSER_EXECUTABLE_PATH"
    MAX_CONTENT_CHARS = "BROWSER_MAX_CONTENT_CHARS"


_DEFAULT_MAX_CONTENT_CHARS = 30000


TOOL_CONFIG: ToolConfig = {
    "name": "browser",
    "display_name": "Browser",
    # Optional Python deps group (see app.features.manifest.FEATURES["browser"]).
    # Read by ``feature_keys_for_tool_ids`` to drive Setup Wizard auto-install
    # and the post-setup enable pre-flight. Tools without optional Python deps
    # omit this key.
    "requires_feature": "browser",
    "required_config": {
        Var.CDP_URL: {
            "description": (
                "Optional. CDP URL to connect to an existing browser "
                "(e.g. http://localhost:9222). "
                "Leave empty to auto-launch Playwright's bundled Chromium."
            ),
            "type": "string",
        },
        Var.HEADLESS: {
            "description": (
                "Run the auto-launched browser in headless mode. "
                "Default: false (visible window)."
            ),
            "type": "boolean",
            "default": False,
        },
        Var.CHANNEL: {
            "description": (
                "Playwright browser channel for auto-launch. 'chrome' uses "
                "system Google Chrome, 'msedge' uses system Edge, 'chromium' "
                "uses Playwright's bundled test build. Default: chrome."
            ),
            "type": "string",
            "enum": ["chrome", "msedge", "chromium"],
            "default": "chrome",
        },
        Var.USER_DATA_DIR: {
            "description": (
                "Directory for the persistent browser profile (cookies, "
                "logins, history). Default: <CREMIND_SYSTEM_DIR>/browser-profile. "
                "Do NOT point this at your real Chrome profile while Chrome "
                "is already running with that profile."
            ),
            "type": "string",
        },
        Var.EXECUTABLE_PATH: {
            "description": (
                "Optional absolute path to the browser executable. Leave empty "
                "to let Playwright auto-discover the binary via the channel."
            ),
            "type": "string",
        },
        Var.MAX_CONTENT_CHARS: {
            "description": (
                "Maximum characters of page snapshot content returned to the "
                "agent before truncation. Default: 30000."
            ),
            "type": "number",
            "default": _DEFAULT_MAX_CONTENT_CHARS,
        },
    },
}


# ---------------------------------------------------------------------------
# Browser session manager (module-level singleton)
# ---------------------------------------------------------------------------

class _BrowserSession:
    """Manages a Playwright browser session.

    Supports two modes:
    - **Auto-launch** (default): launches the system browser identified by
      ``channel`` (default ``chrome``) with a persistent user-data-dir so
      cookies/logins persist across sessions.
    - **CDP-connect**: connects to an already-running browser via a CDP URL
      (``http://``, ``ws://``). Used when ``BROWSER_CDP_URL`` is set.

    Created once by ``get_tools()`` and shared across all tool instances.
    """

    def __init__(
        self,
        cdp_url: str = "",
        headless: bool = False,
        channel: str = "chrome",
        user_data_dir: str = "",
        executable_path: str = "",
    ):
        self._cdp_url = cdp_url.strip() if cdp_url else ""
        self._headless = headless
        self._channel = (channel or "chrome").strip()
        self._user_data_dir = (user_data_dir or "").strip() or os.path.join(
            BaseConfig.CREMIND_SYSTEM_DIR, "browser-profile"
        )
        self._executable_path = (executable_path or "").strip()
        self._playwright: Optional[Playwright] = None
        # Only set in CDP mode — persistent_context returns a BrowserContext
        # whose ``.browser`` may be None. Downstream code reads ``_context``.
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._lock = asyncio.Lock()
        # Set True by Playwright close/disconnected event handlers when the
        # browser app is closed or crashes. connect() relaunches when set.
        self._closed = False

    @property
    def cdp_url(self) -> str:
        return self._cdp_url

    @property
    def mode(self) -> str:
        return "cdp" if self._cdp_url else "auto"

    def update_config(
        self,
        cdp_url: Optional[str] = None,
        headless: Optional[bool] = None,
        channel: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        executable_path: Optional[str] = None,
    ) -> None:
        """Update runtime config. Forces reconnect if values changed."""
        changed = False
        if cdp_url is not None:
            new_url = cdp_url.strip()
            if new_url != self._cdp_url:
                logger.info(f"[Browser] CDP URL updated: {self._cdp_url!r} -> {new_url!r}")
                self._cdp_url = new_url
                changed = True
        if headless is not None and headless != self._headless:
            self._headless = headless
            changed = True
        if channel is not None:
            new_channel = (channel or "chrome").strip()
            if new_channel != self._channel:
                self._channel = new_channel
                changed = True
        if user_data_dir is not None:
            new_dir = (user_data_dir or "").strip() or os.path.join(
                BaseConfig.CREMIND_SYSTEM_DIR, "browser-profile"
            )
            if new_dir != self._user_data_dir:
                self._user_data_dir = new_dir
                changed = True
        if executable_path is not None:
            new_exec = (executable_path or "").strip()
            if new_exec != self._executable_path:
                self._executable_path = new_exec
                changed = True
        if changed:
            self._browser = None
            self._context = None
            self._closed = False

    async def connect(self) -> BrowserContext:
        """Return a connected BrowserContext, launching or connecting as needed.

        Reliability note: Playwright's ``BrowserContext.pages`` is a cached
        property that returns a list without raising even when the underlying
        browser process has died. So we cannot poll it to validate liveness.
        Instead we subscribe to the public ``close`` event on the context (and
        ``disconnected`` on the Browser in CDP mode), flip ``_closed`` to True
        when fired, and relaunch on the next ``connect()``.
        """
        async with self._lock:
            if self._context is not None and not self._closed:
                logger.debug("[Browser] reusing cached BrowserContext")
                return self._context

            self._context = None
            self._browser = None
            self._closed = False

            if not self._playwright:
                self._playwright = await async_playwright().start()

            if self._cdp_url:
                self._context = await self._connect_cdp()
            else:
                self._context = await self._launch_persistent()

            self._context.on("close", self._on_context_closed)
            if self._browser is not None:
                self._browser.on("disconnected", self._on_browser_disconnected)

            return self._context

    def _on_context_closed(self, _context: "BrowserContext") -> None:
        """Mark session stale when the BrowserContext closes externally.

        Fires when the user closes the Chrome window, the browser crashes,
        or our own ``disconnect()`` runs ``context.close()``. Safe in all
        cases — ``connect()`` discards and relaunches on the next call.
        """
        if not self._closed:
            logger.info("[Browser] BrowserContext close event; will relaunch on next call.")
        self._closed = True

    def _on_browser_disconnected(self, _browser: "Browser") -> None:
        """Mark session stale when a CDP-attached Browser disconnects."""
        if not self._closed:
            logger.info("[Browser] Browser disconnected event; will reconnect on next call.")
        self._closed = True

    @staticmethod
    def _is_target_closed_exception(exc: BaseException) -> bool:
        """Heuristic for Playwright's 'browser/context/page closed' errors."""
        msg = str(exc)
        return (
            "has been closed" in msg
            or "Target page, context or browser has been closed" in msg
            or "Browser has been closed" in msg
        )

    async def _launch_persistent(self) -> BrowserContext:
        """Launch system Chrome (or configured channel) with a persistent profile."""
        os.makedirs(self._user_data_dir, exist_ok=True)
        launch_kwargs: Dict[str, Any] = {
            "headless": self._headless,
            "channel": self._channel,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            "no_viewport": True,
        }
        if self._executable_path:
            launch_kwargs["executable_path"] = self._executable_path
        context = await self._playwright.chromium.launch_persistent_context(
            self._user_data_dir, **launch_kwargs
        )
        if not context.pages:
            await context.new_page()
        # Persistent contexts don't have an associated Browser handle we own.
        self._browser = None
        logger.info(
            f"[Browser] Launched {self._channel} (persistent, "
            f"user_data_dir={self._user_data_dir}, headless={self._headless})"
        )
        return context

    async def _connect_cdp(self) -> BrowserContext:
        """Connect to an existing browser via CDP URL and return its first context."""
        ws_url = await self._resolve_ws_url()
        browser = await self._playwright.chromium.connect_over_cdp(ws_url)
        self._browser = browser
        if not browser.contexts:
            raise RuntimeError(
                "CDP browser exposes no contexts. Open a tab in the target browser first."
            )
        logger.info(f"[Browser] Connected via CDP at {ws_url}")
        return browser.contexts[0]

    async def _resolve_ws_url(self) -> str:
        """Resolve the CDP WebSocket URL from the configured endpoint.

        If the user provided a ``ws://`` URL, use it directly.
        Otherwise fetch the WebSocket debugger URL from Chrome's
        ``/json/version`` endpoint.
        """
        url = self._cdp_url.rstrip("/")

        if url.startswith("ws://") or url.startswith("wss://"):
            return url

        # Fetch WS URL from Chrome's JSON endpoint
        version_url = f"{url}/json/version"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(version_url, timeout=5.0)
                resp.raise_for_status()
                data = resp.json()
                ws_debugger_url = data.get("webSocketDebuggerUrl", "")
                if ws_debugger_url:
                    logger.debug(f"[Browser] Resolved WS URL: {ws_debugger_url}")
                    return ws_debugger_url
        except Exception as exc:
            logger.warning(
                f"[Browser] Could not fetch {version_url}: {exc}. "
                f"Falling back to direct connect_over_cdp."
            )

        return url

    async def get_page(self, target_id: Optional[str] = None) -> Page:
        """Return a Page, optionally matching *target_id*.

        When *target_id* is ``None`` the most recently active page is returned.
        Retries once if the cached context died between ``connect()`` and the
        first operation on it — covers the small window where Chrome has
        exited but the ``close`` event has not yet been delivered.
        """
        for attempt in (0, 1):
            context = await self.connect()
            try:
                pages = context.pages
                if not pages:
                    if self.mode == "auto":
                        return await context.new_page()
                    raise RuntimeError("No pages open in the browser.")

                if target_id:
                    for page in pages:
                        if await _page_target_id(page) == target_id:
                            return page
                    raise RuntimeError(f"Tab with target_id '{target_id}' not found.")

                return pages[-1]
            except Exception as exc:
                if attempt == 0 and self._is_target_closed_exception(exc):
                    logger.warning(
                        "[Browser] Context closed mid-operation; forcing relaunch and retrying."
                    )
                    async with self._lock:
                        self._closed = True
                        self._context = None
                        self._browser = None
                    continue
                raise
        raise RuntimeError("get_page exhausted retries.")

    async def get_all_pages(self) -> List[Dict[str, str]]:
        """List all open tabs with their target IDs, URLs, and titles."""
        for attempt in (0, 1):
            context = await self.connect()
            try:
                result: List[Dict[str, str]] = []
                for page in context.pages:
                    entry: Dict[str, str] = {
                        "url": page.url,
                        "title": await page.title(),
                        "target_id": await _page_target_id(page),
                    }
                    result.append(entry)
                return result
            except Exception as exc:
                if attempt == 0 and self._is_target_closed_exception(exc):
                    logger.warning(
                        "[Browser] Context closed during get_all_pages; relaunching."
                    )
                    async with self._lock:
                        self._closed = True
                        self._context = None
                        self._browser = None
                    continue
                raise
        return []

    async def new_page(self, url: Optional[str] = None, wait_until: str = "load") -> Page:
        """Open a new tab and optionally navigate. Retries once on stale context."""
        for attempt in (0, 1):
            context = await self.connect()
            try:
                page = await context.new_page()
                if url:
                    await page.goto(url, wait_until=wait_until, timeout=30000)
                return page
            except Exception as exc:
                if attempt == 0 and self._is_target_closed_exception(exc):
                    logger.warning(
                        "[Browser] Context closed during new_page; relaunching."
                    )
                    async with self._lock:
                        self._closed = True
                        self._context = None
                        self._browser = None
                    continue
                raise
        raise RuntimeError("new_page exhausted retries.")

    async def disconnect(self) -> None:
        """Tear down the browser and Playwright runtime."""
        async with self._lock:
            # Persistent contexts own the browser process; closing the context
            # shuts it down. CDP mode owns a separate Browser handle to close.
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            # Allow a future connect() to start fresh.
            self._closed = False


_DEFAULT_PROFILE = "admin"


def _profile_default_user_data_dir(profile: str) -> str:
    """Return the default browser user-data-dir for an Cremind profile.

    Mirrors the per-profile path convention used elsewhere (e.g. PERSONA.md
    at ``~/.cremind/<profile>/PERSONA.md``).
    """
    return os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, profile, "browser-profile")


def _coerce_headless(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.lower() in ("true", "1", "yes")


def _resolve_session(arguments: Dict[str, Any], defaults: Dict[str, Any]) -> _BrowserSession:
    """Get-or-create the per-profile browser session and apply runtime config.

    The active Cremind profile is read from ``_profile`` (injected by the
    BuiltInToolAdapter). Each profile gets its own ``_BrowserSession`` so
    Chrome windows for different profiles can run concurrently with
    separate user-data-dirs.

    Per-profile variables (``_variables``) override the registration-time
    defaults. If a profile sets ``BROWSER_USER_DATA_DIR`` explicitly, that
    wins over the auto-derived ``~/.cremind/<profile>/browser-profile`` path.

    Both ``_profile`` and ``_variables`` are popped off ``arguments`` so they
    don't leak into the action handlers.
    """
    profile = (arguments.pop("_profile", None) or _DEFAULT_PROFILE).strip() or _DEFAULT_PROFILE
    variables = arguments.pop("_variables", None) or {}
    if not isinstance(variables, dict):
        variables = {}

    # Per-profile defaults: explicit user override wins, else profile-scoped path.
    user_data_dir = (variables.get(Var.USER_DATA_DIR) or "").strip()
    if not user_data_dir:
        user_data_dir = _profile_default_user_data_dir(profile)

    cdp_url = (variables.get(Var.CDP_URL) or defaults.get("cdp_url", "") or "").strip()
    channel = (variables.get(Var.CHANNEL) or defaults.get("channel", "chrome") or "chrome").strip()
    executable_path = (
        variables.get(Var.EXECUTABLE_PATH)
        or defaults.get("executable_path", "")
        or ""
    ).strip()
    headless = _coerce_headless(
        variables.get(Var.HEADLESS), default=defaults.get("headless", False)
    )

    session = _sessions.get(profile)
    if session is None:
        session = _BrowserSession(
            cdp_url=cdp_url,
            headless=headless,
            channel=channel,
            user_data_dir=user_data_dir,
            executable_path=executable_path,
        )
        _sessions[profile] = session
        logger.info(f"[Browser] Created session for profile '{profile}' (user_data_dir={user_data_dir})")
    else:
        logger.debug(f"[Browser] Reusing session for profile '{profile}'")
        # Apply any config drift; update_config is a no-op if nothing changed.
        session.update_config(
            cdp_url=cdp_url,
            headless=headless,
            channel=channel,
            user_data_dir=user_data_dir,
            executable_path=executable_path,
        )
    return session


def _connection_error(cdp_url: str) -> BuiltInToolResult:
    """Standard error result for browser connection failures."""
    if cdp_url:
        msg = (
            f"Could not connect to browser at {cdp_url}. "
            "Make sure the browser is running with --remote-debugging-port."
        )
    else:
        msg = (
            "Could not launch the browser. If BROWSER_CHANNEL='chrome', make "
            "sure Google Chrome is installed. For the bundled Chromium fallback, "
            "set BROWSER_CHANNEL=chromium and run: playwright install chromium."
        )
    logger.error(f"[Browser] connection failed: {msg}")
    return BuiltInToolResult(
        structured_content={"error": "Browser connection failed", "message": msg}
    )


async def _page_target_id(page: Page) -> str:
    """Return the CDP target_id for a Playwright Page, or '' if unavailable.

    Returned in every action response so the agent can address the same tab
    in subsequent calls without having to enumerate ``tabs.list``.
    """
    try:
        cdp = await page.context.new_cdp_session(page)
        info = await cdp.send("Target.getTargetInfo")
        await cdp.detach()
        return info.get("targetInfo", {}).get("targetId", "")
    except Exception:
        return ""


def _resolve_element(page: Page, *, selector: Optional[str], text: Optional[str],
                      role: Optional[str], name: Optional[str]):
    """Return a Playwright locator from the targeting parameters."""
    if selector:
        return page.locator(selector)
    if text:
        return page.get_by_text(text)
    if role:
        kwargs: Dict[str, Any] = {}
        if name:
            kwargs["name"] = name
        return page.get_by_role(role, **kwargs)
    raise ValueError(
        "At least one of 'selector', 'text', or 'role' must be provided "
        "to identify the element."
    )


# ---------------------------------------------------------------------------
# Tool classes
# ---------------------------------------------------------------------------

class BrowserTool(BuiltInTool):
    """Unified browser tool with code-based action dispatch.

    Instead of 7 separate tools behind a child LLM router (which misroutes),
    this single tool takes an ``action`` parameter and dispatches in code.
    """

    name: str = "browser"
    description: str = (
        "Control a Chrome browser via the `action` parameter. Supported actions:\n"
        "- navigate: open a URL (set `url`)\n"
        "- snapshot: read the current page as an accessibility tree — use this to understand page content before interacting\n"
        "- screenshot: capture the page as a PNG image (optionally a `selector` region or `full_page`)\n"
        "- click: click an element by `selector`, visible text, or ARIA role\n"
        "- type: type `text` into an input identified by `selector`, or press keyboard keys\n"
        "- tabs: manage browser tabs (list/switch/close/new)\n"
        "- evaluate: run JavaScript (`expression`) on the page and return the result\n\n"
        "Always run 'snapshot' first to understand the page structure before 'click' or 'type'. "
        "Pass `target_id` to act on a specific tab."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "The browser action to perform. Use 'navigate' to open a URL, "
                    "'snapshot' to read page content, 'screenshot' to capture an image, "
                    "'click' to click an element, 'type' to enter text, "
                    "'tabs' to manage tabs, 'evaluate' to run JavaScript."
                ),
                "enum": ["navigate", "snapshot", "screenshot", "click", "type", "tabs", "evaluate"],
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (for 'navigate') or open in new tab (for 'tabs' with tab_action='new').",
            },
            "target_id": {
                "type": "string",
                "description": "Optional target ID of a specific tab.",
            },
            "wait_until": {
                "type": "string",
                "description": "When to consider navigation complete: 'load', 'domcontentloaded', or 'networkidle'. Default: 'load'.",
                "enum": ["load", "domcontentloaded", "networkidle"],
            },
            "selector": {
                "type": "string",
                "description": "CSS selector (for snapshot scope, screenshot element, click/type target).",
            },
            "full_page": {
                "type": "boolean",
                "description": "If true, capture the entire scrollable page in screenshot. Default: false.",
            },
            "text": {
                "type": "string",
                "description": "Visible text of the element to target (for click/type).",
            },
            "role": {
                "type": "string",
                "description": "ARIA role of the element (e.g. 'button', 'link', 'textbox'). Use with 'name'.",
            },
            "name": {
                "type": "string",
                "description": "Accessible name of the element (used with 'role').",
            },
            "double_click": {
                "type": "boolean",
                "description": "If true, perform a double-click. Default: false.",
            },
            "button": {
                "type": "string",
                "description": "Mouse button: 'left', 'right', or 'middle'. Default: 'left'.",
                "enum": ["left", "right", "middle"],
            },
            "input_text": {
                "type": "string",
                "description": "The text to type into the element.",
            },
            "press_key": {
                "type": "string",
                "description": (
                    "A keyboard key to press (e.g. 'Enter', 'Tab', 'Escape', "
                    "'ArrowDown', 'Backspace'). Pressed after typing input_text if both provided."
                ),
            },
            "clear_first": {
                "type": "boolean",
                "description": "If true, clear the input field before typing. Default: true.",
            },
            "tab_action": {
                "type": "string",
                "description": "Tab sub-action for 'tabs': 'list', 'switch', 'close', or 'new'. Default: 'list'.",
                "enum": ["list", "switch", "close", "new"],
            },
            "expression": {
                "type": "string",
                "description": (
                    "JavaScript expression to evaluate (for 'evaluate'). "
                    "E.g. 'document.title' or '() => document.querySelectorAll(\"a\").length'."
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(self, defaults: Dict[str, Any]):
        # Registration-time fallback values; per-profile overrides arrive
        # through ``_variables`` at request time and are merged in
        # ``_resolve_session()``.
        self._defaults = defaults

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        if not _PLAYWRIGHT_AVAILABLE:
            # ``_PLAYWRIGHT_AVAILABLE`` is captured once at module load
            # time. If pip-install ran AFTER the server started, the
            # wheel is on disk but the running process still sees False.
            # Distinguish "truly missing" from "stale process" so the
            # agent suggests the right remediation (restart vs install).
            try:
                spec = importlib.util.find_spec("playwright")
            except (ImportError, ValueError):
                spec = None
            if spec is not None:
                return restart_required_result(
                    tool="browser",
                    feature_key="browser",
                    detail=(
                        "Playwright is installed but was not importable "
                        "when this Cremind process started."
                    ),
                )
            return missing_dependency_result(
                tool="browser",
                feature_key="browser",
                extras=("browser",),
                detail="playwright is not importable in this process.",
                install_commands=(
                    "pip install 'cremind[browser]'",
                    "python -m playwright install --with-deps chromium",
                ),
            )
        # Pop _profile and _variables off arguments and get the per-profile
        # session. After this call, arguments is safe to forward to handlers.
        session = _resolve_session(arguments, self._defaults)
        action = arguments.get("action", "").strip()
        logger.debug(f"[Browser] dispatch action={action!r} session_mode={session.mode}")

        dispatch = {
            "navigate": self._navigate,
            "snapshot": self._snapshot,
            "screenshot": self._screenshot,
            "click": self._click,
            "type": self._type,
            "tabs": self._tabs,
            "evaluate": self._evaluate,
        }
        handler = dispatch.get(action)
        if not handler:
            logger.warning(f"[Browser] unknown action {action!r}")
            return BuiltInToolResult(
                structured_content={
                    "error": "Invalid action",
                    "message": (
                        f"Unknown action '{action}'. "
                        "Use one of: navigate, snapshot, screenshot, click, type, tabs, evaluate."
                    ),
                }
            )
        return await handler(arguments, session)

    # -- navigate -----------------------------------------------------------

    async def _navigate(self, arguments: Dict[str, Any], session: _BrowserSession) -> BuiltInToolResult:
        url = arguments.get("url", "").strip()
        target_id = arguments.get("target_id")
        wait_until = arguments.get("wait_until", "load")

        if not url:
            logger.warning("[Browser] navigate: missing required parameter 'url'")
            return BuiltInToolResult(
                structured_content={"error": "Missing parameter", "message": "url is required."}
            )

        logger.info(f"[Browser] navigate url={url!r} wait_until={wait_until} target_id={target_id}")
        try:
            page = await session.get_page(target_id)
            response = await page.goto(url, wait_until=wait_until, timeout=30000)
            status = response.status if response else None
            title = await page.title()
            final_url = page.url
            tid = await _page_target_id(page)
            logger.info(
                f"[Browser] navigate ok: status={status} url={final_url!r} title={title!r} target_id={tid}"
            )
            return BuiltInToolResult(
                structured_content={
                    "url": final_url,
                    "title": title,
                    "status": status,
                    "target_id": tid,
                }
            )
        except RuntimeError as e:
            logger.error(f"[Browser] navigate runtime error: {e}")
            return _connection_error(session.cdp_url)
        except Exception as e:
            logger.error(f"[Browser] navigate failed url={url!r}: {e}", exc_info=True)
            return BuiltInToolResult(
                structured_content={"error": "Navigation error", "message": str(e)}
            )

    # -- snapshot -----------------------------------------------------------

    async def _snapshot(self, arguments: Dict[str, Any], session: _BrowserSession) -> BuiltInToolResult:
        target_id = arguments.get("target_id")
        selector = arguments.get("selector")
        variables = arguments.get("_variables") or {}
        try:
            max_chars = int(variables.get(Var.MAX_CONTENT_CHARS) or _DEFAULT_MAX_CONTENT_CHARS)
        except (TypeError, ValueError):
            max_chars = _DEFAULT_MAX_CONTENT_CHARS

        logger.info(
            f"[Browser] snapshot selector={selector!r} target_id={target_id} max_chars={max_chars}"
        )
        try:
            page = await session.get_page(target_id)
            title = await page.title()
            url = page.url
            tid = await _page_target_id(page)

            # Use the locator-based aria_snapshot() API (Playwright 1.49+).
            # The old page.accessibility.snapshot() was removed in newer versions.
            root = page.locator(selector) if selector else page.locator(":root")
            snapshot_text = await root.aria_snapshot(timeout=10000)

            if not snapshot_text or not snapshot_text.strip():
                logger.info(f"[Browser] snapshot empty: url={url!r}")
                return BuiltInToolResult(
                    structured_content={
                        "url": url,
                        "title": title,
                        "target_id": tid,
                        "snapshot": "(empty — page may still be loading)",
                    }
                )

            original_len = len(snapshot_text)
            truncated = original_len > max_chars
            if truncated:
                snapshot_text = snapshot_text[:max_chars] + "\n... (snapshot truncated)"
            logger.info(
                f"[Browser] snapshot ok: url={url!r} chars={original_len} truncated={truncated}"
            )

            return BuiltInToolResult(
                structured_content={
                    "url": url,
                    "title": title,
                    "target_id": tid,
                    "snapshot": snapshot_text,
                }
            )
        except RuntimeError as e:
            logger.error(f"[Browser] snapshot runtime error: {e}")
            return _connection_error(session.cdp_url)
        except Exception as e:
            logger.error(f"[Browser] snapshot failed selector={selector!r}: {e}", exc_info=True)
            return BuiltInToolResult(
                structured_content={"error": "Snapshot error", "message": str(e)}
            )

    # -- screenshot ---------------------------------------------------------

    async def _screenshot(self, arguments: Dict[str, Any], session: _BrowserSession) -> BuiltInToolResult:
        target_id = arguments.get("target_id")
        full_page = arguments.get("full_page", False)
        selector = arguments.get("selector")

        logger.info(
            f"[Browser] screenshot selector={selector!r} full_page={full_page} target_id={target_id}"
        )
        try:
            page = await session.get_page(target_id)

            out_dir = os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, "browser_screenshots")
            os.makedirs(out_dir, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            file_name = f"screenshot_{timestamp}.png"
            file_path = os.path.join(out_dir, file_name)

            if selector:
                element = page.locator(selector)
                await element.screenshot(path=file_path, timeout=10000)
            else:
                await page.screenshot(path=file_path, full_page=full_page, timeout=10000)

            tid = await _page_target_id(page)
            file_uri = file_path.replace(os.sep, "/")
            file_entry: ToolResultFile = {
                "uri": file_uri,
                "name": file_name,
                "mime_type": "image/png",
            }
            payload: ToolResultWithFiles = {
                "text": f"Screenshot saved: {file_name} (target_id: {tid})",
                "_files": [file_entry],
            }
            logger.info(f"[Browser] screenshot ok: {file_path}")
            return BuiltInToolResult(structured_content=payload)

        except RuntimeError as e:
            logger.error(f"[Browser] screenshot runtime error: {e}")
            return _connection_error(session.cdp_url)
        except Exception as e:
            logger.error(f"[Browser] screenshot failed selector={selector!r}: {e}", exc_info=True)
            return BuiltInToolResult(
                structured_content={"error": "Screenshot error", "message": str(e)}
            )

    # -- click --------------------------------------------------------------

    async def _click(self, arguments: Dict[str, Any], session: _BrowserSession) -> BuiltInToolResult:
        target_id = arguments.get("target_id")
        double_click = arguments.get("double_click", False)
        button = arguments.get("button", "left")
        selector = arguments.get("selector")
        text = arguments.get("text")
        role = arguments.get("role")
        name = arguments.get("name")

        logger.info(
            f"[Browser] click selector={selector!r} text={text!r} role={role!r} "
            f"name={name!r} button={button} double_click={double_click} target_id={target_id}"
        )
        try:
            page = await session.get_page(target_id)
            locator = _resolve_element(
                page, selector=selector, text=text, role=role, name=name,
            )

            if double_click:
                await locator.dblclick(button=button, timeout=10000)
            else:
                await locator.click(button=button, timeout=10000)

            await page.wait_for_load_state("domcontentloaded", timeout=5000)

            title = await page.title()
            tid = await _page_target_id(page)
            logger.info(f"[Browser] click ok: url={page.url!r} title={title!r} target_id={tid}")
            return BuiltInToolResult(
                structured_content={
                    "clicked": True,
                    "url": page.url,
                    "title": title,
                    "target_id": tid,
                }
            )
        except ValueError as e:
            logger.warning(f"[Browser] click: missing target ({e})")
            return BuiltInToolResult(
                structured_content={"error": "Missing target", "message": str(e)}
            )
        except RuntimeError as e:
            logger.error(f"[Browser] click runtime error: {e}")
            return _connection_error(session.cdp_url)
        except Exception as e:
            logger.error(
                f"[Browser] click failed selector={selector!r} text={text!r} role={role!r}: {e}",
                exc_info=True,
            )
            return BuiltInToolResult(
                structured_content={
                    "error": "Click error",
                    "message": str(e),
                    "hint": "Use action='snapshot' first to find the correct selector or text.",
                }
            )

    # -- type ---------------------------------------------------------------

    async def _type(self, arguments: Dict[str, Any], session: _BrowserSession) -> BuiltInToolResult:
        target_id = arguments.get("target_id")
        input_text = arguments.get("input_text")
        press_key = arguments.get("press_key")
        clear_first = arguments.get("clear_first", True)
        selector = arguments.get("selector")

        if not input_text and not press_key:
            logger.warning("[Browser] type: missing both 'input_text' and 'press_key'")
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "At least one of 'input_text' or 'press_key' must be provided.",
                }
            )

        # Don't log input_text content — it may be a password.
        input_len = len(input_text) if input_text else 0
        logger.info(
            f"[Browser] type selector={selector!r} input_text_len={input_len} "
            f"press_key={press_key!r} clear_first={clear_first} target_id={target_id}"
        )
        try:
            page = await session.get_page(target_id)

            if input_text:
                locator = _resolve_element(
                    page,
                    selector=selector,
                    text=arguments.get("text"),
                    role=arguments.get("role"),
                    name=arguments.get("name"),
                )
                if clear_first:
                    await locator.fill(input_text, timeout=10000)
                else:
                    await locator.press_sequentially(input_text, timeout=10000)

            if press_key:
                await page.keyboard.press(press_key)

            tid = await _page_target_id(page)
            logger.info(
                f"[Browser] type ok: input_text_len={input_len} press_key={press_key!r} url={page.url!r} target_id={tid}"
            )
            return BuiltInToolResult(
                structured_content={
                    "typed": True,
                    "input_text": input_text,
                    "press_key": press_key,
                    "url": page.url,
                    "target_id": tid,
                }
            )
        except ValueError as e:
            logger.warning(f"[Browser] type: missing target ({e})")
            return BuiltInToolResult(
                structured_content={"error": "Missing target", "message": str(e)}
            )
        except RuntimeError as e:
            logger.error(f"[Browser] type runtime error: {e}")
            return _connection_error(session.cdp_url)
        except Exception as e:
            logger.error(
                f"[Browser] type failed selector={selector!r} press_key={press_key!r}: {e}",
                exc_info=True,
            )
            return BuiltInToolResult(
                structured_content={
                    "error": "Type error",
                    "message": str(e),
                    "hint": "Use action='snapshot' first to find the correct input selector.",
                }
            )

    # -- tabs ---------------------------------------------------------------

    async def _tabs(self, arguments: Dict[str, Any], session: _BrowserSession) -> BuiltInToolResult:
        tab_action = arguments.get("tab_action", "list")
        target_id = arguments.get("target_id")
        url = arguments.get("url")

        logger.info(f"[Browser] tabs tab_action={tab_action!r} target_id={target_id} url={url!r}")
        try:
            if tab_action == "list":
                tabs = await session.get_all_pages()
                logger.info(f"[Browser] tabs.list ok: count={len(tabs)}")
                return BuiltInToolResult(
                    structured_content={"tabs": tabs, "count": len(tabs)}
                )

            elif tab_action == "switch":
                if not target_id:
                    logger.warning("[Browser] tabs.switch: missing required parameter 'target_id'")
                    return BuiltInToolResult(
                        structured_content={
                            "error": "Missing parameter",
                            "message": "target_id is required for 'switch' tab_action.",
                        }
                    )
                page = await session.get_page(target_id)
                await page.bring_to_front()
                title = await page.title()
                tid = await _page_target_id(page)
                logger.info(f"[Browser] tabs.switch ok: url={page.url!r} title={title!r} target_id={tid}")
                return BuiltInToolResult(
                    structured_content={
                        "switched": True,
                        "url": page.url,
                        "title": title,
                        "target_id": tid,
                    }
                )

            elif tab_action == "close":
                if not target_id:
                    logger.warning("[Browser] tabs.close: missing required parameter 'target_id'")
                    return BuiltInToolResult(
                        structured_content={
                            "error": "Missing parameter",
                            "message": "target_id is required for 'close' tab_action.",
                        }
                    )
                page = await session.get_page(target_id)
                closed_url = page.url
                closed_tid = await _page_target_id(page)
                await page.close()
                logger.info(f"[Browser] tabs.close ok: url={closed_url!r} target_id={closed_tid}")
                return BuiltInToolResult(
                    structured_content={"closed": True, "url": closed_url, "target_id": closed_tid}
                )

            elif tab_action == "new":
                page = await session.new_page(url=url)
                title = await page.title()
                tid = await _page_target_id(page)
                logger.info(f"[Browser] tabs.new ok: url={page.url!r} title={title!r} target_id={tid}")
                return BuiltInToolResult(
                    structured_content={
                        "opened": True,
                        "url": page.url,
                        "title": title,
                        "target_id": tid,
                    }
                )

            else:
                logger.warning(f"[Browser] tabs: unknown tab_action {tab_action!r}")
                return BuiltInToolResult(
                    structured_content={
                        "error": "Invalid tab_action",
                        "message": f"Unknown tab_action '{tab_action}'. Use: list, switch, close, new.",
                    }
                )

        except RuntimeError as e:
            if "target_id" in str(e):
                logger.warning(f"[Browser] tabs.{tab_action}: tab not found ({e})")
                return BuiltInToolResult(
                    structured_content={"error": "Tab not found", "message": str(e)}
                )
            logger.error(f"[Browser] tabs.{tab_action} runtime error: {e}")
            return _connection_error(session.cdp_url)
        except Exception as e:
            logger.error(f"[Browser] tabs.{tab_action} failed: {e}", exc_info=True)
            return BuiltInToolResult(
                structured_content={"error": "Tab error", "message": str(e)}
            )

    # -- evaluate -----------------------------------------------------------

    async def _evaluate(self, arguments: Dict[str, Any], session: _BrowserSession) -> BuiltInToolResult:
        expression = arguments.get("expression", "").strip()
        target_id = arguments.get("target_id")

        if not expression:
            logger.warning("[Browser] evaluate: missing required parameter 'expression'")
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "expression is required.",
                }
            )

        expr_preview = expression if len(expression) <= 200 else expression[:200] + "..."
        logger.info(
            f"[Browser] evaluate expression={expr_preview!r} target_id={target_id}"
        )
        try:
            page = await session.get_page(target_id)
            result = await page.evaluate(expression)
            tid = await _page_target_id(page)
            logger.info(f"[Browser] evaluate ok: url={page.url!r} result_type={type(result).__name__} target_id={tid}")
            return BuiltInToolResult(
                structured_content={
                    "result": result,
                    "url": page.url,
                    "target_id": tid,
                }
            )
        except RuntimeError as e:
            logger.error(f"[Browser] evaluate runtime error: {e}")
            return _connection_error(session.cdp_url)
        except Exception as e:
            logger.error(f"[Browser] evaluate failed: {e}", exc_info=True)
            return BuiltInToolResult(
                structured_content={"error": "Evaluate error", "message": str(e)}
            )




# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Per-profile sessions: each Cremind profile gets its own _BrowserSession so
# distinct profiles can keep separate Chrome windows open concurrently with
# their own user-data-dirs. Populated lazily by _resolve_session().
_sessions: Dict[str, _BrowserSession] = {}


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for the Browser tool group.

    The tool is always registered so the Reasoning Agent can discover it.
    If playwright is missing, ``BrowserTool.run()`` returns a
    ``MissingDependency`` structured error on first invocation — that lets
    the agent surface remediation to the user instead of having the tool
    disappear from the catalog.
    """
    # Registration-time defaults. Per-profile values come through _variables
    # at request time via _resolve_session(); these are only fallbacks.
    headless_str = config.get(Var.HEADLESS, "false") or "false"
    defaults: Dict[str, Any] = {
        "cdp_url": config.get(Var.CDP_URL, "") or "",
        "headless": headless_str.lower() in ("true", "1", "yes"),
        "channel": config.get(Var.CHANNEL, "chrome") or "chrome",
        "executable_path": config.get(Var.EXECUTABLE_PATH, "") or "",
    }

    return [
        BrowserTool(defaults),
    ]
