"""Files crossing a channel, in either direction.

Inbound, a platform attachment becomes an :class:`IncomingFile` — a cheap
descriptor whose bytes have NOT been fetched. Adapters build descriptors in
their receive handlers (no I/O there) and the base adapter stages them only
after the sender has passed the channel's auth gate and the conversation is
known, by calling :func:`stage_incoming_files`. That ordering is the abuse
guard: an unauthenticated stranger can make us read an event, never download
a payload. Staged files land in the same per-conversation temp directory the
web composer uses (``app.utils.uploads_tmp``), so the agent reads them with
the same tools, under the same per-profile trust boundary, and the same boot
wipe + idle pruner clean them up.

Outbound, :func:`validate_outbound_paths` is the gate between "a path someone
supplied" and "a file this profile's agent may send": it accepts absolute
paths only inside the profile's own roots — the same roots the ``system_file``
tool group enforces — so one profile can never be talked into shipping another
profile's files (or the server's) out over a chat platform.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

from app.utils.logger import logger
from app.utils.uploads_tmp import (
    conversation_tmp_dir,
    max_upload_bytes,
    safe_basename,
    unique_dest,
)


@dataclass
class IncomingFile:
    """One attachment as the transport reports it — bytes NOT fetched yet.

    ``fetch(dest_dir)`` downloads the payload into ``dest_dir`` and returns
    the absolute path it saved to (adapters should name the file with
    :func:`dest_for` so every transport names files identically). It must be
    cheap to *create* and safe to *never call* — a dropped message never
    fetches. ``discard`` releases anything the transport already holds (a
    sidecar's spooled file); it too must be safe to skip.
    """

    name: str
    mime: Optional[str] = None
    size: Optional[int] = None
    fetch: Optional[Callable[[str], Awaitable[str]]] = None
    discard: Optional[Callable[[], Awaitable[None]]] = None


def dest_for(dest_dir: str, name: str) -> str:
    """The collision-free destination path for one incoming file."""
    return unique_dest(dest_dir, safe_basename(name))


async def stage_incoming_files(
    adapter: Any, conversation_id: str, files: Sequence[IncomingFile],
) -> list[dict]:
    """Download descriptors into the conversation's temp dir.

    Returns ``[{"name", "path"}]`` entries in the exact shape
    ``enqueue_user_message`` / ``try_park_user_message`` accept. Never raises:
    a file that fails or exceeds the cap is skipped (logged) and the rest
    still land — a broken attachment must not swallow the message it rode in
    on.
    """
    staged: list[dict] = []
    if not files:
        return staged
    try:
        dest_dir = conversation_tmp_dir(adapter.profile, conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channels:{adapter.channel_type}] could not create the upload "
            f"dir for {conversation_id}; attachments dropped"
        )
        await discard_incoming_files(files)
        return staged

    cap = max_upload_bytes()
    for f in files:
        # Platform-reported size first, so an over-cap file is refused without
        # moving a byte. Platforms omit or understate sizes, hence the
        # re-check after the download.
        if f.size is not None and f.size > cap:
            logger.warning(
                f"[channels:{adapter.channel_type}] attachment '{f.name}' "
                f"({f.size} bytes) exceeds the {cap}-byte upload cap; skipped"
            )
            await _discard_one(f)
            continue
        if f.fetch is None:
            await _discard_one(f)
            continue
        try:
            saved = await f.fetch(dest_dir)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"[channels:{adapter.channel_type}] failed to fetch "
                f"attachment '{f.name}'"
            )
            continue
        if not saved or not os.path.isfile(saved):
            logger.warning(
                f"[channels:{adapter.channel_type}] attachment '{f.name}' "
                f"fetch returned no file; skipped"
            )
            continue
        try:
            actual = os.path.getsize(saved)
        except OSError:
            actual = 0
        if actual > cap:
            logger.warning(
                f"[channels:{adapter.channel_type}] attachment '{f.name}' is "
                f"{actual} bytes after download (cap {cap}); removed"
            )
            try:
                os.remove(saved)
            except OSError:
                pass
            continue
        staged.append({"name": os.path.basename(saved), "path": saved})
    return staged


def files_from_sidecar_frame(msg: dict) -> list[IncomingFile]:
    """Descriptors for a Node-sidecar frame's spooled media files.

    The WhatsApp and Zalo sidecars download media AT RECEIPT into a spool
    directory (their media handles expire too fast to defer) and put
    ``files: [{path, name, mime, size}]`` in the frame — paths, never bytes,
    because the WS caps frames at 4 MiB and sidecar and server share the
    filesystem. ``fetch`` claims a file by moving it into the conversation's
    upload dir; ``discard`` deletes it, so every drop path leaves no stray
    disk behind.
    """
    import asyncio
    import shutil

    found: list[IncomingFile] = []
    for entry in msg.get("files") or ():
        if not isinstance(entry, dict):
            continue
        spool_path = str(entry.get("path") or "").strip()
        if not spool_path:
            continue
        name = str(entry.get("name") or "") or os.path.basename(spool_path)
        size = entry.get("size")

        def _make_callbacks(spool_path: str = spool_path, name: str = name):
            async def fetch(dest_dir: str) -> str:
                dest = dest_for(dest_dir, name)
                await asyncio.to_thread(shutil.move, spool_path, dest)
                return dest

            async def discard() -> None:
                try:
                    os.remove(spool_path)
                except OSError:
                    pass

            return fetch, discard

        fetch, discard = _make_callbacks()
        found.append(IncomingFile(
            name=name,
            mime=entry.get("mime"),
            size=int(size) if isinstance(size, (int, float)) else None,
            fetch=fetch,
            discard=discard,
        ))
    return found


async def discard_incoming_files(files: Optional[Sequence[IncomingFile]]) -> None:
    """Release every descriptor's held resource; used on all drop paths."""
    for f in files or ():
        await _discard_one(f)


async def _discard_one(f: IncomingFile) -> None:
    if f.discard is None:
        return
    try:
        await f.discard()
    except Exception:  # noqa: BLE001
        logger.debug(
            f"channels: discard of incoming file '{f.name}' failed", exc_info=True,
        )


# ── outbound ─────────────────────────────────────────────────────────────

def validate_outbound_paths(
    profile: str,
    paths: Sequence[Any],
    *,
    extra_roots: Optional[Sequence[str]] = None,
) -> tuple[list[dict], list[dict]]:
    """Split candidate outbound paths into sendable files and rejections.

    Returns ``(ok, rejected)`` where ``ok`` entries are
    ``{"path", "name", "mime", "size"}`` and ``rejected`` entries are
    ``{"path", "reason"}``. A path is accepted only when it is an existing
    file inside one of the profile's own roots — the same trust boundary the
    ``system_file`` tool group enforces (skills tree, user working dir, the
    profile's slice of the system dir), plus any ``extra_roots`` the caller
    vouches for (e.g. the turn's working-directory override).
    """
    ok: list[dict] = []
    rejected: list[dict] = []
    roots = _profile_roots(profile, extra_roots)
    for raw in paths or ():
        path = str(raw or "").strip()
        if not path:
            continue
        expanded = os.path.expanduser(path)
        is_abs = os.path.isabs(expanded) or (
            len(expanded) >= 2 and expanded[1] == ":"
        )
        if not is_abs:
            rejected.append({"path": path, "reason": "not an absolute path"})
            continue
        target = os.path.realpath(expanded)
        if not any(
            target == root or target.startswith(root + os.sep) for root in roots
        ):
            rejected.append(
                {"path": path, "reason": "outside this profile's allowed directories"},
            )
            continue
        if not os.path.isfile(target):
            rejected.append({"path": path, "reason": "not an existing file"})
            continue
        mime, _ = mimetypes.guess_type(target)
        try:
            size = os.path.getsize(target)
        except OSError:
            size = None
        ok.append({
            "path": target,
            "name": os.path.basename(target),
            "mime": mime or "application/octet-stream",
            "size": size,
        })
    return ok, rejected


def _profile_roots(
    profile: str, extra_roots: Optional[Sequence[str]] = None,
) -> list[str]:
    """The profile-scoped roots an outbound file may come from (realpaths)."""
    candidates: list[str] = []
    try:
        from app.config.system_vars import build_system_env

        env = build_system_env(profile)
    except Exception:  # noqa: BLE001
        logger.debug("channels: build_system_env failed for outbound roots", exc_info=True)
        env = {}
    if env.get("CREMIND_SKILL_DIR"):
        candidates.append(env["CREMIND_SKILL_DIR"])
    if env.get("CREMIND_USER_WORKING_DIR"):
        candidates.append(env["CREMIND_USER_WORKING_DIR"])
    sys_dir = env.get("CREMIND_SYSTEM_DIR")
    if sys_dir and profile:
        candidates.append(os.path.join(sys_dir, profile))
    for extra in extra_roots or ():
        if extra:
            candidates.append(str(extra))
    return [os.path.realpath(c) for c in candidates if c]


# ── user-facing text ─────────────────────────────────────────────────────

def file_fallback_text(name: str, size: Optional[int] = None) -> str:
    """The notice sent instead of a file on a transport that can't carry one.

    Deliberately names the file and nothing else — never the server path.
    """
    detail = f" ({_human_size(size)})" if size else ""
    return (
        f"📎 {safe_basename(name)}{detail} — this channel can't receive files, "
        "so only this notice was sent. Ask in the app to get the file another way."
    )


def placeholder_text(names: Sequence[str]) -> str:
    """Synthesised message text for an attachment with no caption.

    A file-only message must still start a turn; this is the text that stands
    in for the caption the sender didn't write.
    """
    cleaned = [safe_basename(n) for n in names if n]
    if not cleaned:
        return "[sent a file]"
    if len(cleaned) == 1:
        return f"[sent a file: {cleaned[0]}]"
    return f"[sent {len(cleaned)} files: {', '.join(cleaned)}]"


def _human_size(size: Optional[int]) -> str:
    if not size or size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
