"""Preparing the server's gateway components, with progress (``host_prepare``).

Three steps, each reported on the operation (``stage``, ``stage_detail`` and
a bounded ``log``), so the page shows progress and offers a retry:

1. **packages** — the ``tags`` extra, through the feature installer (the same
   pip run as every other optional feature; nothing is downgraded, and the
   installed Cremind version is pinned);
2. **fonts** — a verified font asset bundle into ``<SYS>/.tag-runtime/assets``
   (:mod:`.fonts`); never built on the user's computer, except from a
   developer checkout that already has the built pack;
3. **start** — the hardware host starts (or restarts, to load new fonts: also
   when it still draws with another pack than the pinned one, installed
   meanwhile). Once it draws with the pinned pack, the packs nothing uses any
   more are removed — best effort, and never a pack a bridge behind this
   computer's gateways still shows.

The operation succeeds when the host runs; otherwise it fails with what is
still missing.
"""

from __future__ import annotations

import asyncio
import gc
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, update

from app.utils.logger import logger

LOG_KEEP = 60
_tasks: set[asyncio.Task[Any]] = set()


def run_in_background(op_id: str) -> None:
    task = asyncio.get_running_loop().create_task(prepare(op_id), name=f"tags prepare {op_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _update(op_id: str, *, log_line: str | None = None, **values: Any) -> None:
    from app.tags.ownership import OPERATIONS
    from app.tags.storage import get_tag_storage, now_ms

    async with get_tag_storage().engine.begin() as conn:
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
        if row is None:
            return
        result = dict(row.result or {})
        if log_line:
            result["log"] = [*(result.get("log") or []), log_line[:300]][-LOG_KEEP:]
        if "result_extra" in values:
            result.update(values.pop("result_extra"))
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op_id).values(
            result=result, updated_at=now_ms(), **values))


async def prepare(op_id: str) -> None:
    from app.tags.storage import now_ms

    from . import fonts
    from .components import readiness
    from .host import get_host, start_hosting, stop_hosting

    loop = asyncio.get_running_loop()
    host = get_host()

    def say(line: str) -> None:
        asyncio.run_coroutine_threadsafe(_update(op_id, log_line=line), loop)

    try:
        await _update(op_id, stage="packages", stage_detail="Installing the gateway components",
                      log_line="Installing the gateway packages…")
        ok, message = await asyncio.to_thread(_install_packages, say)
        if not ok:
            raise PrepareFailed("packages_failed", message)
        await _update(op_id, stage="fonts", stage_detail="Installing the fonts tags are drawn with",
                      log_line="Installing the font pack…")
        installed = await asyncio.to_thread(fonts.ensure_installed, host.paths.assets_dir, say)
        await _update(op_id, log_line=installed.message)
        await _update(op_id, stage="starting", stage_detail="Starting gateway support")
        pinned = fonts.pinned_pack()
        if host.running and installed.changed:
            await stop_hosting("new fonts were installed")
        elif host.running and pinned and installed.pack_id == pinned and host.fonts_pack != pinned:
            await _update(op_id, log_line=f"Switching to font pack {pinned}…")
            await stop_hosting(f"switching to font pack {pinned}")
        result = await start_hosting()
        if result["state"] != "running":
            raise PrepareFailed("components_unavailable", result.get("reason") or "Gateway support did not start.",
                                readiness=readiness(host.paths.assets_dir, pinned))
        if pinned and host.fonts_pack == pinned:
            await _remove_old_packs(op_id, host, pinned)
        doc = readiness(host.paths.assets_dir, pinned, host.fonts_pack)
        await _update(op_id, state="succeeded", stage="done", stage_detail=None, finished_at=now_ms(),
                      result_extra={"readiness": doc}, log_line="Gateway support is running.")
    except PrepareFailed as exc:
        await _update(op_id, state="failed", stage="done", finished_at=now_ms(),
                      error={"code": exc.code, "message": str(exc)},
                      result_extra={"readiness": exc.readiness or readiness(host.paths.assets_dir,
                                                                            fonts.pinned_pack())},
                      log_line=str(exc))
    except Exception as exc:  # noqa: BLE001 - reported on the operation, the server goes on
        logger.exception("[tags] preparing the gateway components failed")
        await _update(op_id, state="failed", stage="done", finished_at=now_ms(),
                      error={"code": "prepare_failed", "message": f"Preparing the components failed: {exc}"},
                      log_line=f"Failed: {exc}")


async def _remove_old_packs(op_id: str, host: Any, pinned: str) -> None:
    """The host draws with the pinned pack: remove the packs nothing uses any more — but never one a bridge
    behind this computer's gateways still shows (unknown: nothing is removed). Best effort: a pack that cannot
    go now (a file still open, on Windows) stays until the next preparation, and the preparation goes on."""
    from app.tags.hosts import bridge_font_packs

    from . import fonts

    lines: list[str] = []
    try:
        keep = {pinned, *await bridge_font_packs(host.host_id or "")}

        def remove() -> list[str]:
            gc.collect()  # the fonts the stopped host had open (Windows keeps open files from being moved)
            return fonts.remove_other_packs(host.paths.assets_dir, keep, lines.append)

        await asyncio.to_thread(remove)
    except Exception:  # noqa: BLE001 - older packs only take room
        logger.warning("[tags] removing the font packs nothing uses any more failed", exc_info=True)
    for line in lines:
        logger.info(f"[tags] {line}")
        await _update(op_id, log_line=line)


class PrepareFailed(Exception):
    def __init__(self, code: str, message: str, *, readiness: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.readiness = readiness


def _install_packages(say: Callable[[str], None]) -> tuple[bool, str]:
    """The ``tags`` extra through the feature installer; ``(ok, message)``."""
    from .components import FEATURE_KEY, packages

    if packages().state == "ready":
        say("The gateway packages are already installed.")
        return True, "already installed"
    from app.features.installer import install_features

    def emit(event: Any) -> None:
        text = getattr(event, "message", "") or ""
        if text and getattr(event, "kind", "") in ("start", "log", "error", "done", "post_install"):
            say(text)

    result = install_features([FEATURE_KEY], emit)
    if result.error or FEATURE_KEY in (result.failed or []):
        return False, result.error or "The gateway packages could not be installed (see the log above)."
    if result.restart_required:
        return False, "The gateway packages were updated; restart Cremind to use them."
    return True, "installed"


__all__ = ["PrepareFailed", "prepare", "run_in_background"]
