import asyncio
import inspect
import json
import os
import sys
from typing import Any, Dict

from dotenv import load_dotenv
from loguru import logger

# Project root for relative paths
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(__file__))
)  # Adjust based on your structure (app/utils/logger.py -> project root)

# Load .env defensively. settings.py also calls load_dotenv, but logger.py is
# imported before settings.py in some module-load orders (config bootstrap,
# install-catalog loader). load_dotenv with override=False is idempotent.
_dotenv_path = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(_dotenv_path, override=False)


_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _resolve_level(raw: str | None, default: str) -> str:
    if not raw:
        return default
    up = raw.strip().upper()
    return up if up in _VALID_LEVELS else default


_disable_log = os.environ.get("DISABLE_LOG", "false").strip().lower() == "true"
_debug_flag = os.environ.get("DEBUG", "false").strip().lower() == "true"
_log_level = _resolve_level(os.environ.get("LOG_LEVEL"), "INFO")

# Under pytest force the file sink to WARNING so test runs don't churn
# logs/app.log. The Developer page (bus sink) is still DEBUG so test code
# that asserts against bus output works unchanged.
_under_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
_file_level = "WARNING" if _under_pytest else _log_level
_stderr_level = "DEBUG" if _debug_flag else _log_level


def get_caller_info(stack_index: int = 2) -> str:
    """Get caller file:line (relative path). stack_index=2 skips cloud and this func."""
    stack = inspect.stack()
    if len(stack) <= stack_index:
        return "unknown:0"
    frame = stack[stack_index]
    filename = frame.filename
    relative_path = os.path.relpath(filename, PROJECT_ROOT).replace("\\", "/")
    line = frame.lineno
    return f"{relative_path}:{line}"


# Remove default sink and add configured ones
logger.remove()

if not _disable_log:
    # Stdout with pretty format (includes file:function:line by default)
    logger.add(
        sys.stderr,
        level=_stderr_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )

    # File logger
    log_file_path = os.path.join(PROJECT_ROOT, "logs", "app.log")
    logger.add(
        log_file_path,
        level=_file_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="1 MB",
        retention="10 days",
        enqueue=True,
    )


# In-memory bus sink — feeds the Developer page's live log viewer.
# Imported lazily to avoid a hard dependency cycle if the bus module
# ever wants to log something itself (the loopback guard below blocks
# records from this module and from the bus). Always at DEBUG so the
# Developer page sees everything regardless of LOG_LEVEL / DISABLE_LOG.
_LOOPBACK_PREFIXES = ("app.events.log_stream_bus", "app.api.logs_stream")
_TRACEBACK_CAP = 8 * 1024


def _bus_sink(message: Any) -> None:
    """Publish a Loguru record to the log stream bus.

    Loguru passes a ``Message`` whose ``.record`` is the structured
    dict. We extract a small JSON-safe shape and drop on any failure —
    a sink that raises breaks every ``logger.X`` call site.
    """
    try:
        record = message.record
        name = record.get("name") or ""
        if name.startswith(_LOOPBACK_PREFIXES):
            return
        time_obj = record.get("time")
        ts = time_obj.isoformat() if time_obj is not None else ""
        level_obj = record.get("level")
        level_name = getattr(level_obj, "name", str(level_obj or "INFO"))
        line = record.get("line") or 0
        function = record.get("function") or ""
        source = f"{name}:{function}:{line}" if name else function
        text = record.get("message") or ""
        if record.get("exception") is not None:
            formatted = str(message)
            if len(formatted) > _TRACEBACK_CAP:
                formatted = formatted[:_TRACEBACK_CAP] + "...[truncated]"
            text = formatted
        entry: Dict[str, Any] = {
            "ts": ts,
            "level": level_name,
            "source": source,
            "message": text,
        }
        from app.events.log_stream_bus import get_log_stream_bus
        get_log_stream_bus().publish(entry)
    except Exception:  # noqa: BLE001
        # Sinks must never raise.
        pass


logger.add(_bus_sink, level="DEBUG")


# Single startup confirmation so support tickets show what was configured.
logger.info(
    f"logger configured: stderr={_stderr_level if not _disable_log else 'off'} "
    f"file={_file_level if not _disable_log else 'off'} bus=DEBUG "
    f"(LOG_LEVEL={_log_level} DEBUG={_debug_flag} DISABLE_LOG={_disable_log}"
    f"{' pytest' if _under_pytest else ''})"
)


# Export
__all__ = ["logger"]
