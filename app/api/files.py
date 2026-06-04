"""File serving API for CREMIND_SYSTEM_DIR.

Serves files from the configured data directory with path-traversal prevention.
Protected by the application's existing JWT authentication middleware.
"""

import asyncio
import json
import os
import shutil

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.config.settings import BaseConfig, get_user_working_directory
from app.events import get_event_stream_bus
from app.utils.context_storage import get_context
from app.utils.logger import logger
from app.utils.working_directory import (
    WORKING_DIR_OVERRIDE_KEY as _WORKING_DIR_OVERRIDE_KEY,
    persist_working_directory,
    set_in_memory_override,
)


def _allowed_bases() -> list[str]:
    """Directories the file-serving routes are allowed to read from.

    Includes both the internal Cremind working dir and the user's
    working dir (where the agent operates and produces files).
    """
    bases = [os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR)]
    user_dir = os.path.realpath(get_user_working_directory())
    if user_dir not in bases:
        bases.append(user_dir)
    return bases


def _allowed_bases_for_conversation(conversation_id: str | None) -> list[str]:
    """Same as ``_allowed_bases`` plus the active conversation's cwd override.

    The ``change_working_directory`` tool may switch a conversation into an
    arbitrary directory outside the static bases via ``target='custom'``.
    File-tree clients pass the originating ``conversation_id`` so the API
    can widen the allowlist for that one request without weakening the
    sandbox for unattributed callers.
    """
    bases = _allowed_bases()
    if not conversation_id:
        return bases
    override = get_context(conversation_id, _WORKING_DIR_OVERRIDE_KEY)
    if not override:
        return bases
    extra = os.path.realpath(override)
    if extra not in bases:
        bases.append(extra)
    return bases


def _is_inside_allowed(target: str, conversation_id: str | None = None) -> bool:
    for base in _allowed_bases_for_conversation(conversation_id):
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def _safe_resolve(relative_path: str) -> str | None:
    """Resolve a relative path inside CREMIND_SYSTEM_DIR.

    Returns the absolute path if safe, or None if traversal is detected.
    """
    base = os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR)
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


async def _serve_file(request: Request):
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    relative_path = request.path_params.get("path", "")
    if not relative_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)

    logger.debug(f"File request: relative_path={relative_path}, CREMIND_SYSTEM_DIR={BaseConfig.CREMIND_SYSTEM_DIR}")
    target = _safe_resolve(relative_path)
    if target is None:
        return JSONResponse({"error": "Access denied"}, status_code=403)

    logger.debug(f"Resolved file path: {target}, exists={os.path.isfile(target)}")
    if not os.path.isfile(target):
        return JSONResponse(
            {"error": "File not found", "resolved_path": target, "data_dir": BaseConfig.CREMIND_SYSTEM_DIR},
            status_code=404,
        )

    return FileResponse(target)


async def _serve_file_by_path(request: Request):
    """Serve a file given its absolute path.

    The path must resolve inside one of the allowed bases
    (CREMIND_SYSTEM_DIR or the user working directory).
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    abs_path = request.query_params.get("path", "")
    if not abs_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)
    conversation_id = request.query_params.get("conversation_id") or None

    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, conversation_id):
        logger.debug(
            f"File access denied: target={target}, "
            f"allowed_bases={_allowed_bases_for_conversation(conversation_id)}"
        )
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not os.path.isfile(target):
        return JSONResponse({"error": "File not found"}, status_code=404)

    return FileResponse(target)


_DIRECTORY_LIST_CAP = 2000


async def _list_directory(request: Request):
    """List entries in a directory.

    Used by the right-side file tree to render the workspace under the
    current shell working directory. Honors the same path-allowlist as the
    file-serving endpoints.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    abs_path = request.query_params.get("path", "")
    if not abs_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)
    show_hidden = request.query_params.get("show_hidden", "0") == "1"
    conversation_id = request.query_params.get("conversation_id") or None

    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, conversation_id):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(target):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    entries: list[dict] = []
    try:
        with os.scandir(target) as it:
            for de in it:
                if not show_hidden and de.name.startswith("."):
                    continue
                try:
                    is_dir = de.is_dir(follow_symlinks=False)
                    is_file = de.is_file(follow_symlinks=False)
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({
                    "name": de.name,
                    "path": os.path.join(target, de.name),
                    "is_dir": is_dir,
                    "size": st.st_size if is_file else None,
                    "modified": st.st_mtime,
                })
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    truncated = len(entries) > _DIRECTORY_LIST_CAP
    if truncated:
        entries = entries[:_DIRECTORY_LIST_CAP]
    return JSONResponse({"path": target, "entries": entries, "truncated": truncated})


async def _get_cwd(request: Request):
    """Return the seed working directory for the file tree.

    The live cwd flows in via terminal WebSocket ``status`` messages once a
    terminal is open; this endpoint just provides the initial value before
    any terminal exists.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    return JSONResponse({"cwd": get_user_working_directory()})


_WATCH_QUEUE_SIZE = 2048


async def _watch_directory(request: Request):
    """Stream filesystem-change events for a directory as Server-Sent Events.

    Uses watchdog's recursive Observer so the entire subtree under ``path``
    is monitored. Events are emitted as JSON frames with ``type`` of
    ``ready`` (handshake), ``created``, ``deleted``, ``modified``, or
    ``moved``. The same allowlist applies as the file-serving endpoints.

    Both the Vue UI and the Go CLI consume this via ordinary SSE clients —
    no extra protocol or dependency needed on the consumer side.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    abs_path = request.query_params.get("path", "")
    if not abs_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)
    conversation_id = request.query_params.get("conversation_id") or None
    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, conversation_id):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(target):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_WATCH_QUEUE_SIZE)

    def _enqueue(payload: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except RuntimeError:
            # Loop closed during shutdown; drop the event.
            pass
        except asyncio.QueueFull:
            # Bursty changes (e.g. ``npm install``) overflow; drop oldest.
            try:
                queue.get_nowait()
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:
                pass

    class _Handler(FileSystemEventHandler):
        def on_created(self, event: FileSystemEvent) -> None:
            _enqueue({
                "type": "created",
                "path": event.src_path,
                "is_dir": event.is_directory,
            })

        def on_deleted(self, event: FileSystemEvent) -> None:
            _enqueue({
                "type": "deleted",
                "path": event.src_path,
                "is_dir": event.is_directory,
            })

        def on_modified(self, event: FileSystemEvent) -> None:
            # Modifications fire on directories whenever a child changes —
            # that's noisy and the consumer can't act on it (its child list
            # is unaffected). Only forward file-level modifications.
            if event.is_directory:
                return
            _enqueue({
                "type": "modified",
                "path": event.src_path,
                "is_dir": False,
            })

        def on_moved(self, event: FileSystemEvent) -> None:
            _enqueue({
                "type": "moved",
                "path": event.src_path,
                "dest_path": getattr(event, "dest_path", ""),
                "is_dir": event.is_directory,
            })

    observer = Observer()
    try:
        observer.schedule(_Handler(), target, recursive=True)
        observer.start()
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to start filesystem watcher for %s", target)
        return JSONResponse({"error": f"Failed to watch: {e}"}, status_code=500)

    async def generator():
        def _frame(payload: dict) -> bytes:
            return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

        try:
            yield _frame({"type": "ready", "path": target})
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                yield _frame(ev)
        finally:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                logger.debug("Observer cleanup raised", exc_info=True)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        generator(), media_type="text/event-stream", headers=headers,
    )


def _resolve_safe(abs_path: str, conversation_id: str | None) -> str | None:
    """Realpath-resolve and verify ``abs_path`` is within the allowlist.

    Returns the resolved absolute path on success, or ``None`` if the path is
    outside any allowed base. Used by the write endpoints (upload/delete/move/
    mkdir/cwd) so callers don't repeat the validate-and-resolve dance.
    """
    if not abs_path:
        return None
    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, conversation_id):
        return None
    return target


def _is_allowed_base(path: str) -> bool:
    """``True`` iff ``path`` (already realpath'd) is exactly an allowed base.

    Used to refuse delete/move operations that would clobber a configured
    root (CREMIND_SYSTEM_DIR or the user working dir).
    """
    return path in _allowed_bases()


def _unique_dest(target_dir: str, basename: str) -> str:
    """Return ``target_dir + sep + basename`` with a numeric suffix that
    avoids collisions, mimicking the OS file managers' "(1)", "(2)" style.

    ``foo.txt`` → ``foo (1).txt`` → ``foo (2).txt`` …
    """
    candidate = os.path.join(target_dir, basename)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(basename)
    n = 1
    while True:
        renamed = f"{stem} ({n}){ext}"
        candidate = os.path.join(target_dir, renamed)
        if not os.path.exists(candidate):
            return candidate
        n += 1


_UPLOAD_CHUNK = 1 << 20  # 1 MiB


async def _upload_files(request: Request):
    """Multipart upload of one or more files into a target directory.

    Form fields:
      - ``path`` (required): absolute path of the destination directory.
      - ``conversation_id`` (optional): widens the path allowlist for paths
        the active conversation has been switched into.
      - Files: any number of file parts (any field name).
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"error": f"Failed to parse multipart form: {exc}"}, status_code=400
        )

    target_dir = form.get("path", "")
    if not isinstance(target_dir, str) or not target_dir:
        return JSONResponse({"error": "No path specified"}, status_code=400)
    conversation_id = form.get("conversation_id") or None
    if isinstance(conversation_id, str) and not conversation_id:
        conversation_id = None

    resolved = _resolve_safe(target_dir, conversation_id if isinstance(conversation_id, str) else None)
    if resolved is None:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(resolved):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    results: list[dict] = []
    for field, value in form.multi_items():
        if not hasattr(value, "filename") or not getattr(value, "filename", None):
            continue
        # Strip any path components from the client-supplied filename — we
        # only ever write into the validated target dir.
        basename = os.path.basename(value.filename)
        if not basename or basename in (".", ".."):
            results.append(
                {"name": value.filename, "saved_as": "", "status": "error",
                 "error": "Invalid filename"}
            )
            continue
        dest = _unique_dest(resolved, basename)
        try:
            with open(dest, "wb") as out:
                while True:
                    chunk = await value.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Upload failed for %s", basename)
            results.append(
                {"name": basename, "saved_as": "", "status": "error",
                 "error": str(exc)}
            )
            continue
        results.append({
            "name": basename,
            "saved_as": os.path.basename(dest),
            "status": "renamed" if os.path.basename(dest) != basename else "ok",
        })

    return JSONResponse({"results": results})


async def _delete_entry(request: Request):
    """Delete a file or recursively delete a directory."""
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    path = body.get("path") if isinstance(body, dict) else None
    conversation_id = body.get("conversation_id") if isinstance(body, dict) else None
    if not isinstance(path, str) or not path:
        return JSONResponse({"error": "No path specified"}, status_code=400)

    resolved = _resolve_safe(path, conversation_id if isinstance(conversation_id, str) else None)
    if resolved is None:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if _is_allowed_base(resolved):
        return JSONResponse({"error": "Refusing to delete an allowed base"}, status_code=400)
    if not os.path.exists(resolved):
        return JSONResponse({"error": "Not found"}, status_code=404)

    try:
        if os.path.isdir(resolved) and not os.path.islink(resolved):
            shutil.rmtree(resolved)
        else:
            os.remove(resolved)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Delete failed for %s", resolved)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True})


async def _move_entry(request: Request):
    """Move (or rename) a file or directory.

    JSON body: ``{src, dest, conversation_id?}``. ``dest`` is the full target
    path including the new basename — callers wishing to move *into* a folder
    must compose ``dest`` themselves.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    src = body.get("src") if isinstance(body, dict) else None
    dest = body.get("dest") if isinstance(body, dict) else None
    conversation_id = body.get("conversation_id") if isinstance(body, dict) else None
    if not isinstance(src, str) or not src or not isinstance(dest, str) or not dest:
        return JSONResponse({"error": "src and dest are required"}, status_code=400)

    cid = conversation_id if isinstance(conversation_id, str) else None
    src_resolved = _resolve_safe(src, cid)
    if src_resolved is None:
        return JSONResponse({"error": "Access denied (src)"}, status_code=403)
    if _is_allowed_base(src_resolved):
        return JSONResponse({"error": "Refusing to move an allowed base"}, status_code=400)
    if not os.path.exists(src_resolved):
        return JSONResponse({"error": "src not found"}, status_code=404)

    # Resolve the dest's *parent* against the allowlist (the dest itself
    # doesn't exist yet — realpath would resolve through the missing leaf).
    dest_parent = os.path.dirname(dest)
    if not dest_parent:
        return JSONResponse({"error": "dest must include a parent directory"}, status_code=400)
    dest_parent_resolved = _resolve_safe(dest_parent, cid)
    if dest_parent_resolved is None:
        return JSONResponse({"error": "Access denied (dest)"}, status_code=403)
    if not os.path.isdir(dest_parent_resolved):
        return JSONResponse({"error": "dest parent is not a directory"}, status_code=404)

    dest_resolved = os.path.join(dest_parent_resolved, os.path.basename(dest))
    if os.path.exists(dest_resolved):
        return JSONResponse({"error": "dest already exists"}, status_code=409)

    # Refuse drop-into-self / descendant moves.
    if dest_resolved == src_resolved or dest_resolved.startswith(src_resolved + os.sep):
        return JSONResponse(
            {"error": "Cannot move a path into itself or a descendant"},
            status_code=400,
        )

    try:
        shutil.move(src_resolved, dest_resolved)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Move failed: %s -> %s", src_resolved, dest_resolved)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "dest": dest_resolved})


async def _mkdir(request: Request):
    """Create a new directory at an absolute path inside the allowlist."""
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    path = body.get("path") if isinstance(body, dict) else None
    conversation_id = body.get("conversation_id") if isinstance(body, dict) else None
    if not isinstance(path, str) or not path:
        return JSONResponse({"error": "No path specified"}, status_code=400)

    cid = conversation_id if isinstance(conversation_id, str) else None
    parent = os.path.dirname(path)
    if not parent:
        return JSONResponse({"error": "path must include a parent"}, status_code=400)
    parent_resolved = _resolve_safe(parent, cid)
    if parent_resolved is None:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(parent_resolved):
        return JSONResponse({"error": "Parent is not a directory"}, status_code=404)

    target = os.path.join(parent_resolved, os.path.basename(path))
    if os.path.exists(target):
        return JSONResponse({"error": "Already exists"}, status_code=409)
    try:
        os.makedirs(target, exist_ok=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception("mkdir failed for %s", target)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "path": target})


async def _set_cwd(request: Request):
    """Set the conversation's working-directory override.

    Mirrors the write+publish performed by the
    ``change_working_directory`` tool ([app.tools.builtin.change_working_directory])
    so the UI can change the agent's effective cwd without a tool round-trip.

    JSON body: ``{conversation_id, path}``.

    The path must be an existing absolute directory; we deliberately do **not**
    require it to be inside ``_allowed_bases()`` because the tool's
    ``target='custom'`` branch likewise allows arbitrary user-supplied dirs,
    and once the override is set, ``_allowed_bases_for_conversation`` widens
    the allowlist for subsequent reads from the same conversation.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    conversation_id = body.get("conversation_id") if isinstance(body, dict) else None
    path = body.get("path") if isinstance(body, dict) else None
    if not isinstance(conversation_id, str) or not conversation_id:
        return JSONResponse({"error": "conversation_id is required"}, status_code=400)
    if not isinstance(path, str) or not path:
        return JSONResponse({"error": "path is required"}, status_code=400)

    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return JSONResponse({"error": "path must be absolute"}, status_code=400)
    new_path = os.path.realpath(expanded)
    if not os.path.isdir(new_path):
        return JSONResponse({"error": "path does not exist or is not a directory"},
                            status_code=400)

    set_in_memory_override(conversation_id, new_path)

    # Persist alongside the in-memory write so the override survives a
    # server restart and reopening the conversation restores the same cwd
    # the user last selected (validated for existence in
    # ``hydrate_working_directory`` on next load).
    try:
        from app.events.runner import get_conversation_storage
        await persist_working_directory(
            conversation_id, new_path, get_conversation_storage(),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to persist cwd override for %s", conversation_id
        )

    try:
        await get_event_stream_bus().publish(
            conversation_id, "cwd", {"working_directory": new_path}
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to publish cwd change for %s", conversation_id)

    return JSONResponse({"working_directory": new_path})


def get_file_routes() -> list[Route]:
    """Return routes for the file serving API."""
    # Specific routes must come before the catch-all ``/api/files/{path:path}``
    # so Starlette doesn't dispatch ``/list``, ``/cwd``, ``/watch`` to
    # ``_serve_file``.
    return [
        Route("/api/files/list", endpoint=_list_directory, methods=["GET"]),
        Route("/api/files/cwd", endpoint=_get_cwd, methods=["GET"]),
        Route("/api/files/cwd", endpoint=_set_cwd, methods=["POST"]),
        Route("/api/files/watch", endpoint=_watch_directory, methods=["GET"]),
        Route("/api/files/open", endpoint=_serve_file_by_path, methods=["GET"]),
        Route("/api/files/upload", endpoint=_upload_files, methods=["POST"]),
        Route("/api/files/delete", endpoint=_delete_entry, methods=["DELETE"]),
        Route("/api/files/move", endpoint=_move_entry, methods=["POST"]),
        Route("/api/files/mkdir", endpoint=_mkdir, methods=["POST"]),
        Route("/api/files/{path:path}", endpoint=_serve_file, methods=["GET"]),
    ]
