"""File serving API for CREMIND_SYSTEM_DIR and the caller's working directory.

Serves files from the configured data directory and the calling profile's own
User Working Directory, with path-traversal prevention. Another profile's
working directory is never served (see :mod:`app.config.working_dirs`).
Protected by the application's existing JWT authentication middleware.
"""

import asyncio
import json
import os
import shutil
import stat

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.api._auth import is_admin
from app.config import working_dirs
from app.config.coding_cli_homes import shared_claude_config_dir, shared_codex_home
from app.config.settings import BaseConfig, get_user_working_directory
from app.events import get_event_stream_bus
from app.utils.context_storage import get_context
from app.utils.logger import logger
from app.utils.uploads_tmp import conversation_tmp_dir, max_upload_bytes
from app.utils.working_directory import (
    WORKING_DIR_OVERRIDE_KEY as _WORKING_DIR_OVERRIDE_KEY,
    persist_working_directory,
    set_in_memory_override,
)


# Directory names under the System Directory that hold coding-agent
# credentials, and which these routes must therefore never serve to anybody.
#
# The sandbox below is a path-traversal guard, not an authorization boundary:
# it stops a caller escaping the System Directory, but every authenticated
# profile may read anything *inside* it. That was survivable while the
# directory held only per-profile working data. It stopped being survivable
# when the coding agents moved their sign-in here: ``coding-cli`` holds long
# lived OAuth refresh tokens for a user's Claude and ChatGPT accounts, at the
# fully predictable path ``<system dir>/<profile>/coding-cli/...``, and
# ``codex-home`` holds the ``auth.json`` written for an API-key credential. A
# member profile could list one and download another profile's account.
#
# Nothing legitimately browses these: the SPA never asks for them, the runners
# read them straight off disk, and the sign-in flows write them. So deny them
# outright rather than trying to work out who is asking. Kept as a name set
# because there is one store per profile, they are created on demand, and the
# same names appear both per profile and at the shared root -- there is no list
# of live paths to enumerate, so the name is the rule. The set itself lives in
# :mod:`app.utils.credential_paths` so Documentation search can share it.
from app.utils.credential_paths import CREDENTIAL_DIR_NAMES as _CREDENTIAL_DIR_NAMES  # noqa: E402
from app.utils.credential_paths import (  # noqa: E402
    DocumentTreeGuard,
    authored_docs_owner,
    holds_authored_docs,
    holds_documents_index,
    is_documents_index_path,
    same_uid,
)

# Directory names directly under a profile's own directory that only that
# profile may reach through these routes: ``<system dir>/<profile>/<name>/...``.
#
# Same reasoning as the credential stores above, one step milder. ``exports``
# holds the configuration file ``cremind config export`` writes, which embeds
# that profile's live JWT — so the name rule alone would be wrong (its owner
# *must* be able to download it; that is the whole point of the Download chip in
# chat) and no rule at all would be worse (the path is fully predictable, and
# every authenticated profile could fetch it).
#
# Deliberately narrow. Widening this to the whole ``<system dir>/<profile>/``
# subtree is the boundary the sandbox arguably should have had all along, but a
# group-chat room renders every member agent's file tree from that subtree, so
# that change needs its own audit rather than riding along here.
_PRIVATE_PROFILE_DIR_NAMES = frozenset({"exports"})


def _private_path_matcher(profile: str | None):
    """Predicate over *resolved* paths: is it inside some other profile's
    private directory? :func:`_is_other_profiles_private_path` without the
    ``realpath``, for the per-entry listing filter and the per-event watch
    filter, whose paths are built from a root resolved before the route
    accepted it."""
    base = os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR)
    own = profile or ""

    def _matches(resolved: str) -> bool:
        try:
            relative = os.path.relpath(resolved, base)
        except ValueError:
            return False  # different drive on Windows: not under the System Directory
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return False
        segments = relative.replace("\\", "/").split("/")
        if len(segments) < 2 or segments[1] not in _PRIVATE_PROFILE_DIR_NAMES:
            return False
        return segments[0] != own

    return _matches


def _is_other_profiles_private_path(target: str, profile: str | None) -> bool:
    """Is ``target`` inside some *other* profile's private directory?

    ``profile`` is the authenticated caller. ``None`` means "no caller known",
    which denies every private path: a route that forgets to pass one fails
    closed rather than serving another profile's export.

    Admin is not exempt. Its own exports live in its own slice, so the exemption
    would buy nothing and would hand the one profile most likely to be
    prompt-injected a reader for everyone else's tokens.
    """
    return _private_path_matcher(profile)(os.path.realpath(target))


# Each profile's User Working Directory is its own (see
# :mod:`app.config.working_dirs`). A path inside another profile's folder -- or
# inside an entry of the workspaces root that no live profile owns, such as a
# deleted profile's archive under ``.deleted`` -- is off limits to every route
# here, however the caller arrived at it: a conversation override never widens
# it. Admin is not exempt, for the reason given for ``exports`` above. The rule
# matters twice over because the default workspaces root sits inside the System
# Directory, which every profile may otherwise browse.
_FOREIGN_WORKSPACE_ERROR = "That location belongs to another profile's working directory"
_FOREIGN_WORKSPACE_CODE = "foreign_working_directory"


def _is_foreign_workspace(target: str, profile: str | None) -> bool:
    """Is ``target`` inside a working directory that is not the caller's?

    ``None`` (no caller known) denies every owned path -- failing closed, like
    the private-directory rule."""
    return working_dirs.is_foreign(target, profile)


def _foreign_matcher(root: str, profile: str | None):
    """Predicate over *resolved* paths below the resolved ``root``: does the
    path lie in another profile's working directory (or an unowned workspaces
    entry)? For the per-entry listing filter and the per-event watch filter.

    Built once per request. The working directories inside ``root`` are asked
    for up front, so an ordinary path costs a prefix comparison; only a path
    under the workspaces root -- where a new profile's folder may appear while
    a watch is open -- is resolved and asked about individually."""
    foreign = tuple(
        d for d in working_dirs.foreign_dirs_inside(root, profile or "")
        # ``foreign_dirs_inside`` judges a workspaces entry by its name alone;
        # an explicit ``<name>-2`` folder belongs to its profile all the same.
        if working_dirs.is_foreign(d, profile)
    )
    ws = os.path.normcase(os.path.realpath(working_dirs.workspaces_root()))
    ws_below = working_dirs.is_inside(ws, root)

    def _matches(resolved: str) -> bool:
        p = os.path.normcase(resolved)
        if any(p == d or p.startswith(d + os.sep) for d in foreign):
            return True
        if ws_below and p.startswith(ws + os.sep):
            return working_dirs.is_foreign(resolved, profile)
        return False

    return _matches


def _holds_foreign_workspace(target: str, profile: str | None) -> bool:
    """Does the resolved ``target`` contain the workspaces root, or another
    profile's working directory? Deleting or moving such a parent would take
    other profiles' files with it -- or carry them out from under the rule
    that keeps them to their owners -- so neither route accepts it."""
    if working_dirs.is_inside(os.path.realpath(working_dirs.workspaces_root()), target):
        return True
    return any(
        working_dirs.is_foreign(d, profile)
        for d in working_dirs.foreign_dirs_inside(target, profile or "")
    )


def _is_workspaces_container(target: str) -> bool:
    """Is the resolved ``target`` the workspaces root or its ``.deleted``
    folder? Both hold nothing but profiles' folders: an entry created there
    belongs to no profile, so nobody -- its creator included -- could reach it
    again, and one wearing a future profile's name would be that profile's."""
    norm = os.path.normcase(target)
    return any(
        norm == os.path.normcase(os.path.realpath(root))
        for root in (working_dirs.workspaces_root(), working_dirs.deleted_root())
    )


def _access_denied(path: str | None, profile: str | None, suffix: str = "") -> JSONResponse:
    """The 403 for a path a route refused. One inside another profile's
    working directory says so (with a ``code`` the file panel keys on), so the
    panel can explain the refusal instead of showing a bare "Access denied";
    every other refusal keeps the generic message, which names no rule."""
    if path and _is_foreign_workspace(os.path.realpath(path), profile):
        return JSONResponse(
            {"error": _FOREIGN_WORKSPACE_ERROR + suffix, "code": _FOREIGN_WORKSPACE_CODE},
            status_code=403,
        )
    return JSONResponse({"error": "Access denied" + suffix}, status_code=403)


def _is_documents_index_path(target: str) -> bool:
    """Documentation search's index store — every profile's indexed text —
    is never served here, to anyone (see ``is_documents_index_path``)."""
    return is_documents_index_path(target, BaseConfig.CREMIND_SYSTEM_DIR)


# A profile's own Cremind manual pages live at
# ``<system dir>/storage/cremind_documents/profiles/<profile uuid>/`` — outside
# the name-keyed ``<system dir>/<profile>/`` slice, keyed by the uuid so no
# profile name can collide with it. Only the owner reaches its directory here
# (admin is not exempt, for the reason given for ``exports`` above); the
# ``profiles`` directory itself lists only the caller's own entry.


def _caller_manual_uid(profile: str | None) -> str | None:
    """The caller's uuid, read from its profile row. Only asked when a path is
    actually inside the manual root, so ordinary requests never pay for it."""
    if not profile:
        return None
    from app.cremind_documents.paths import resolve_profile_uid

    return resolve_profile_uid(profile)


def _is_other_profiles_authored_docs(target: str, profile: str | None) -> bool:
    """Is ``target`` inside some *other* profile's manual directory?

    ``None`` (no caller known) and a caller whose uuid cannot be resolved both
    deny every such path — failing closed, like the private-directory rule."""
    owner = authored_docs_owner(target, BaseConfig.CREMIND_SYSTEM_DIR)
    if owner is None:
        return False
    return not same_uid(owner, _caller_manual_uid(profile))


def _document_trees_matcher(profile: str | None):
    """Predicate over *resolved* paths: the index store, or another profile's
    manual directory. For per-entry listing filters and per-event watch
    filters — built once per request, no ``realpath`` per call (the paths are
    built from a root that was resolved before the route accepted it), and the
    caller's uuid is looked up at most once, and only if a path needs it.

    The rule itself (case-insensitive filesystems included) is
    :class:`~app.utils.credential_paths.DocumentTreeGuard`, shared with the
    agent's file tool so the two can never disagree about what is hidden."""
    guard = DocumentTreeGuard(
        BaseConfig.CREMIND_SYSTEM_DIR, own_uid=lambda: _caller_manual_uid(profile),
    )
    return guard.hides


def _holds_protected_document_tree(target: str) -> bool:
    """Does ``target`` contain every profile's manual pages or the index
    store? Moving or deleting such a parent (``storage``,
    ``storage/cremind_documents``, …) would carry off or destroy other
    profiles' data wholesale, so neither route accepts it."""
    system_dir = BaseConfig.CREMIND_SYSTEM_DIR
    return holds_authored_docs(target, system_dir) or holds_documents_index(target, system_dir)


def _shared_credential_homes() -> tuple[str, ...]:
    """The install-wide CLI logins, resolved wherever they actually point.

    The per-profile stores are recognisable by name because Cremind chooses
    their path. The *server's own* login is not: on a native install it is
    ``~/.claude`` / ``~/.codex``, and the operator (or the Dockerfile, or the
    Helm chart) can move either with ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``.
    Neither wears a name this module could match and neither need be under the
    System Directory at all, so a name-only rule left the shared home -- the
    one credential every profile without its own login runs on -- outside the
    guard entirely, in a directory (the user's home) that the file tree
    routinely browses.

    Asked of :mod:`app.config.coding_cli_homes` rather than re-derived here so
    the guard cannot drift from the code that writes the tokens, and resolved
    live on every call for the same reason that module reads its environment
    live: move a home and the guard moves with it.
    """
    homes: list[str] = []
    for resolve in (shared_claude_config_dir, shared_codex_home):
        try:
            homes.append(os.path.realpath(str(resolve())))
        except Exception:  # noqa: BLE001
            # A home we cannot even name is one we cannot guard; it must not
            # also take a file listing down with it.
            logger.debug("files: could not resolve a shared CLI home", exc_info=True)
    return tuple(homes)


def _credential_matcher():
    """Build the "is this a credential store?" predicate over *resolved* paths.

    Returned as a closure, not called per path, because a directory listing
    asks it up to ``_DIRECTORY_LIST_CAP`` times and the watch stream once per
    filesystem event: the shared homes cost a ``realpath`` (and an install-mode
    lookup) each to resolve, and this way they cost that once per request.

    Callers must pass an already-resolved path -- ``_is_credential_path`` is
    the entry point that resolves for them. Listing and watch skip that step
    deliberately: their paths are built from a target that was resolved before
    the route accepted it.
    """
    base = os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR)
    homes = _shared_credential_homes()

    def _matches(resolved: str) -> bool:
        for home in homes:
            if resolved == home or resolved.startswith(home + os.sep):
                return True
        try:
            relative = os.path.relpath(resolved, base)
        except ValueError:
            # Different drive on Windows: not under the System Directory.
            return False
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return False
        segments = relative.replace("\\", "/").split("/")
        return any(segment in _CREDENTIAL_DIR_NAMES for segment in segments)

    return _matches


def _is_credential_path(target: str) -> bool:
    """Is ``target`` a credential store, or inside one?

    Resolves first so a symlink or a ``..`` cannot walk in sideways, then
    applies both halves of the rule: any path segment named after a store
    *under the System Directory* (covering ``<system dir>/coding-cli``,
    ``<system dir>/codex-home`` and every ``<system dir>/<profile>/coding-cli``
    without enumerating them), and the shared homes by location.
    """
    return _credential_matcher()(os.path.realpath(target))


def _child_dirs(parent: str) -> list[os.DirEntry]:
    """Immediate sub-directories of ``parent``; ``[]`` if it cannot be read.

    ``follow_symlinks=False`` so a link is never descended: a store reached
    through one is still refused by path, and following links here would turn a
    bounded scan into an unbounded one.
    """
    try:
        with os.scandir(parent) as it:
            return [de for de in it if de.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _holds_credential_store(target: str) -> bool:
    """Does a credential store live strictly *below* the resolved ``target``?

    ``_is_credential_path`` answers "is this a store"; this answers "does this
    contain one", which is the question a move has to ask. Moving a store was
    already refused, but moving its *parent* was not -- and a profile directory
    only has to reach the user working directory for the name rule to stop
    applying, after which ``/open`` serves the tokens like any other file.

    The System Directory side is a bounded scan rather than a recursive walk:
    ``coding_cli_homes`` writes exactly ``<sysdir>/<name>`` (the install-wide
    login and the managed Codex homes) and ``<sysdir>/<profile>/<name>``, so
    two levels find every store a sign-in can have created, while a walk would
    descend a whole profile's working data -- ``node_modules`` and all -- on
    every rename in the file tree.
    """
    # ``os.sep`` alone: every path compared here has been through ``realpath``,
    # which normalises the separator, so tolerating the other one would only
    # mangle a directory whose name legitimately ends in it.
    prefix = target.rstrip(os.sep) + os.sep
    if any(home.startswith(prefix) for home in _shared_credential_homes()):
        return True
    base = os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR)
    # Only scan when the two trees actually overlap: target above the System
    # Directory, at it, or inside it. Anything else cannot contain a store the
    # name rule knows about.
    if not (base == target or base.startswith(prefix) or target.startswith(base + os.sep)):
        return False
    for child in _child_dirs(base):
        if child.name in _CREDENTIAL_DIR_NAMES:
            if child.path.startswith(prefix):
                return True
            continue
        for grandchild in _child_dirs(child.path):
            if grandchild.name in _CREDENTIAL_DIR_NAMES and grandchild.path.startswith(prefix):
                return True
    return False


def _creates_credential_name(path: str) -> bool:
    """Would creating ``path`` put a store's *name* on disk?

    This is the half ``_resolve_safe`` cannot see: ``/move`` and ``/mkdir``
    validate the target's parent, and the parent of a brand-new ``coding-cli``
    is an ordinary directory. By name alone and everywhere -- not only under
    the System Directory -- because the listing filter is by name and
    everywhere too: a directory created under this name vanishes from the file
    tree along with whatever the user then puts in it, and under the System
    Directory it is the exact path the next sign-in writes a token to.
    """
    return os.path.basename(path.rstrip("\\/")) in _CREDENTIAL_DIR_NAMES


def _allowed_bases(profile: str | None) -> list[str]:
    """Directories the file-serving routes let ``profile`` read from.

    The internal Cremind System Directory and the caller's *own* working
    directory (where its agent operates and produces files) -- never another
    profile's. No caller known means no working directory at all.
    """
    bases = [os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR)]
    if not profile:
        return bases
    try:
        user_dir = os.path.realpath(get_user_working_directory(profile))
    except ValueError:
        # A name that is no directory name has no working directory.
        return bases
    if user_dir not in bases:
        bases.append(user_dir)
    return bases


def _allowed_bases_for_conversation(
    context_key: str | None, profile: str | None,
) -> list[str]:
    """Same as ``_allowed_bases`` plus the active conversation's cwd override.

    The ``change_working_directory`` tool may switch a conversation into an
    arbitrary directory outside the static bases via ``target='custom'``.
    File-tree clients pass the originating ``conversation_id`` so the API
    can widen the allowlist for that one request without weakening the
    sandbox for unattributed callers.

    ``context_key`` is that conversation's ``context_id`` — the key the agent
    writes the override under, resolved once per request by
    :func:`_conversation_scope`. Looking it up by the client-supplied row id
    instead finds nothing for a group-chat seat, whose context_id is
    ``group:<gid>:<profile>``.

    ``profile`` is the caller, whose own working directory is the other base:
    an admin reading a member seat's tree gets the seat's override, not the
    member's working directory (and the foreign-workspace rule, checked before
    any of this, still refuses the member's folder).
    """
    bases = _allowed_bases(profile)
    if not context_key:
        return bases
    override = get_context(context_key, _WORKING_DIR_OVERRIDE_KEY)
    if not override:
        return bases
    extra = os.path.realpath(override)
    if extra not in bases:
        bases.append(extra)
    return bases


def _is_inside_allowed(
    target: str, context_key: str | None = None, profile: str | None = None,
) -> bool:
    # Checked before the bases, and never widened by a conversation override:
    # a credential store is off limits however the caller arrived at it, and so
    # are another profile's private directory and its working directory.
    if _is_credential_path(target):
        return False
    if _is_other_profiles_private_path(target, profile):
        return False
    if _is_foreign_workspace(target, profile):
        return False
    if _is_documents_index_path(target):
        return False
    if _is_other_profiles_authored_docs(target, profile):
        return False
    for base in _allowed_bases_for_conversation(context_key, profile):
        if target == base or target.startswith(base + os.sep):
            return True
    return False


async def _conversation_scope(
    request: Request, conversation_id: str | None, *, write: bool,
) -> tuple[str | None, str | None, JSONResponse | None]:
    """Resolve a request's ``conversation_id`` into ``(row_id, context_key, denial)``.

    Two problems are handled in one place, because both come from the same id.

    Ownership: naming a conversation widens the path allowlist to whatever
    directory that conversation was switched into, so an unchecked id let any
    authenticated profile read — and upload into, and delete through — someone
    else's custom cwd. The owner always passes. ``admin`` passes only when
    ``write`` is ``False``, because the justification for the bypass is a read:
    the group room's right-hand panel renders every member agent's file tree.
    Nothing in that panel asks to *delete* a member's file, and ``/cwd`` would
    be worse still — it repoints a running agent's working directory, so an
    admin write there re-aims another profile's live turn at a directory of the
    admin's choosing.

    ``write`` is keyword-only and has no default on purpose: a route added later
    has to state which side of that line it is on, rather than inheriting the
    laxer half by saying nothing.

    Identity: the override lives under the conversation's ``context_id``, which
    equals the row id for an ordinary chat but not for a group-chat seat. Every
    handler must widen and mutate under ``context_key`` and address the DB row
    and the event bus by ``row_id``.

    An id matching no row yields ``(None, None, None)`` — the request is served
    against the static bases exactly as an unattributed one.
    """
    if not conversation_id:
        return None, None, None
    try:
        from app.events.runner import get_conversation_storage
        conv = await get_conversation_storage().get_conversation(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"files: failed to load conversation {conversation_id}")
        conv = None
    if conv is None:
        return None, None, None
    profile = getattr(request.user, "username", "") or ""
    if conv.get("profile") != profile and (write or not is_admin(request)):
        return None, None, JSONResponse({"error": "Forbidden"}, status_code=403)
    row_id = conv.get("id") or conversation_id
    return row_id, conv.get("context_id") or row_id, None


def _safe_resolve(relative_path: str, profile: str | None = None) -> str | None:
    """Resolve a relative path inside CREMIND_SYSTEM_DIR.

    Returns the absolute path if safe, or None if traversal is detected.
    """
    base = os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR)
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        return None
    # The ``{path:path}`` route reaches this without going through
    # _is_inside_allowed, and it does not filter dotfiles either, so a request
    # for ``<profile>/coding-cli/claude/.credentials.json`` would otherwise be
    # served verbatim. Same for another profile's ``exports``.
    if _is_credential_path(target):
        return None
    if _is_other_profiles_private_path(target, profile):
        return None
    # The default workspaces root is ``<system dir>/workspaces``, so this
    # route reaches every profile's working directory by a relative path.
    if _is_foreign_workspace(target, profile):
        return None
    if _is_documents_index_path(target):
        return None
    if _is_other_profiles_authored_docs(target, profile):
        return None
    return target


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _profile_of(request: Request) -> str:
    """The authenticated caller's profile, for the private-directory rule."""
    return getattr(request.user, "username", "") or ""


async def _serve_file(request: Request):
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    relative_path = request.path_params.get("path", "")
    if not relative_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)

    logger.debug(f"File request: relative_path={relative_path}, CREMIND_SYSTEM_DIR={BaseConfig.CREMIND_SYSTEM_DIR}")
    profile = _profile_of(request)
    target = _safe_resolve(relative_path, profile)
    if target is None:
        return _access_denied(
            os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, relative_path), profile,
        )

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
    _row_id, context_key, denied = await _conversation_scope(
        request, conversation_id, write=False,
    )
    if denied is not None:
        return denied

    profile = _profile_of(request)
    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, context_key, profile):
        logger.debug(
            f"File access denied: target={target}, "
            f"allowed_bases={_allowed_bases_for_conversation(context_key, profile)}"
        )
        return _access_denied(target, profile)

    if not os.path.isfile(target):
        return JSONResponse({"error": "File not found"}, status_code=404)

    return FileResponse(target)


_DIRECTORY_LIST_CAP = 2000


def _entry_hidden(name: str, attrs: int, show_hidden: bool) -> bool:
    """Whether a ``scandir`` entry should be omitted from a listing.

    ``attrs`` is ``stat_result.st_file_attributes`` — the Windows attribute
    bitmask, or ``0`` on POSIX (where the field is absent).

    * SYSTEM-flagged entries are *always* omitted. These are protected-OS
      items such as the legacy ``My Music``/``My Pictures``/``My Videos``
      junctions in the user profile, which reparse to directories *outside*
      the working tree and only 403 when opened — Explorer keeps them hidden
      by default too.
    * Hidden entries — dotfiles (POSIX convention) or the Windows HIDDEN
      attribute — are omitted unless ``show_hidden`` is set.
    """
    if attrs & stat.FILE_ATTRIBUTE_SYSTEM:
        return True
    if not show_hidden and (
        name.startswith(".") or bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
    ):
        return True
    return False


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
    _row_id, context_key, denied = await _conversation_scope(
        request, conversation_id, write=False,
    )
    if denied is not None:
        return denied

    profile = _profile_of(request)
    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, context_key, profile):
        return _access_denied(target, profile)
    if not os.path.isdir(target):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    entries: list[dict] = []
    is_credential = _credential_matcher()
    is_protected_doc_tree = _document_trees_matcher(profile)
    is_private = _private_path_matcher(profile)
    is_foreign = _foreign_matcher(target, profile)
    try:
        with os.scandir(target) as it:
            for de in it:
                try:
                    is_dir = de.is_dir(follow_symlinks=False)
                    is_file = de.is_file(follow_symlinks=False)
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                if _entry_hidden(de.name, attrs, show_hidden):
                    continue
                # Opening one is already refused; leaving the name in the
                # listing would only advertise where the credentials live.
                # Unlike _entry_hidden this is not a show_hidden toggle. The
                # name covers the per-profile stores; the path check covers the
                # shared home, which wears an ordinary name and, on a native
                # install browsing the user's home, is an ordinary entry here.
                # Likewise another profile's working directory (listing
                # ``<system dir>/workspaces`` shows the caller its own folder
                # only) and its private ``exports``.
                entry_path = os.path.join(target, de.name)
                if (
                    de.name in _CREDENTIAL_DIR_NAMES
                    or is_credential(entry_path)
                    or is_protected_doc_tree(entry_path)
                    or is_private(entry_path)
                    or is_foreign(entry_path)
                ):
                    continue
                entries.append({
                    "name": de.name,
                    "path": entry_path,
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
    """Return the seed working directory for the file tree: the caller's own
    working directory.

    The live cwd flows in via terminal WebSocket ``status`` messages once a
    terminal is open; this endpoint just provides the initial value before
    any terminal exists.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    try:
        cwd = get_user_working_directory(_profile_of(request))
    except ValueError:
        return JSONResponse({"error": "No working directory for this caller"}, status_code=400)
    return JSONResponse({"cwd": cwd})


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
    _row_id, context_key, denied = await _conversation_scope(
        request, conversation_id, write=False,
    )
    if denied is not None:
        return denied
    profile = _profile_of(request)
    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, context_key, profile):
        return _access_denied(target, profile)
    if not os.path.isdir(target):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_WATCH_QUEUE_SIZE)
    is_credential = _credential_matcher()
    is_protected_doc_tree = _document_trees_matcher(profile)
    is_private = _private_path_matcher(profile)
    is_foreign = _foreign_matcher(target, profile)

    def _withheld(path: str) -> bool:
        return (
            is_credential(path)
            or is_protected_doc_tree(path)
            or is_private(path)
            or is_foreign(path)
        )

    def _enqueue(payload: dict) -> None:
        # The observer is recursive, so a watch on the System Directory (or on
        # a profile directory) sees every write inside a credential store --
        # and a change frame naming ``.../coding-cli/codex/auth.json`` hands
        # back exactly what the listing filter is there to withhold. Filtered
        # at the single choke point all four handlers go through, and on both
        # ends of a move. No ``realpath`` (bar a path under the workspaces
        # root, see ``_foreign_matcher``): watchdog builds these paths from the
        # already-resolved watch root, and this runs once per event. The same
        # goes for the index store, other profiles' manual pages and
        # ``exports``, and other profiles' working directories (a watch on the
        # System Directory spans ``workspaces/``), whose file names the listing
        # withholds too.
        if any(
            _withheld(os.path.normpath(payload[key]))
            for key in ("path", "dest_path")
            if payload.get(key)
        ):
            return
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
        logger.exception(f"Failed to start filesystem watcher for {target}")
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


def _resolve_safe(
    abs_path: str, context_key: str | None, profile: str | None = None,
) -> str | None:
    """Realpath-resolve and verify ``abs_path`` is within the allowlist.

    Returns the resolved absolute path on success, or ``None`` if the path is
    outside any allowed base. Used by the write endpoints (upload/delete/move/
    mkdir/cwd) so callers don't repeat the validate-and-resolve dance.
    """
    if not abs_path:
        return None
    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, context_key, profile):
        return None
    return target


def _is_protected_root(path: str, profile: str | None) -> bool:
    """``True`` iff ``path`` (already realpath'd) is a root no delete or move
    may take.

    The caller's allowed bases (CREMIND_SYSTEM_DIR, its own working
    directory), the workspaces root and its ``.deleted`` folder, and any
    profile's working directory -- the point where one owner's zone begins,
    i.e. where :func:`working_dirs.owners_of` answers differently than for the
    parent. The caller can only reach its own (or one the admin pointed it at
    together with another profile), but deleting or moving that would pull the
    folder out from under every profile using it.
    """
    norm = os.path.normcase(path)
    roots = [
        *_allowed_bases(profile),
        working_dirs.workspaces_root(),
        working_dirs.deleted_root(),
    ]
    if any(norm == os.path.normcase(os.path.realpath(root)) for root in roots):
        return True
    owners = working_dirs.owners_of(path)
    return owners is not None and owners != working_dirs.owners_of(os.path.dirname(path))


def _unique_dest(target_dir: str, basename: str) -> str:
    """Collision-free destination path; shared logic lives in ``uploads_tmp``."""
    from app.utils.uploads_tmp import unique_dest

    return unique_dest(target_dir, basename)


_UPLOAD_CHUNK = 1 << 20  # 1 MiB


async def _write_upload(value, target_dir: str, max_bytes: int | None = None) -> dict:
    """Chunk-write one uploaded file part into ``target_dir``.

    Shared by ``_upload_files`` and ``_upload_temp_files``. Strips any path
    components from the client filename, picks a collision-free destination,
    and (when ``max_bytes`` is set) aborts + removes the partial file if the
    upload exceeds the ceiling. Returns a per-file result dict.
    """
    basename = os.path.basename(getattr(value, "filename", "") or "")
    if not basename or basename in (".", ".."):
        return {"name": getattr(value, "filename", ""), "saved_as": "",
                "status": "error", "error": "Invalid filename"}
    dest = _unique_dest(target_dir, basename)
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await value.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    out.close()
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    return {"name": basename, "saved_as": "", "status": "error",
                            "error": f"File exceeds the {max_bytes}-byte upload limit"}
                out.write(chunk)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Upload failed for {basename}")
        return {"name": basename, "saved_as": "", "status": "error", "error": str(exc)}
    return {
        "name": basename,
        "saved_as": os.path.basename(dest),
        "status": "renamed" if os.path.basename(dest) != basename else "ok",
        "path": dest,
    }


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
    conversation_id = form.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        conversation_id = None
    _row_id, context_key, denied = await _conversation_scope(
        request, conversation_id, write=True,
    )
    if denied is not None:
        return denied

    profile = _profile_of(request)
    resolved = _resolve_safe(target_dir, context_key, profile)
    if resolved is None:
        return _access_denied(target_dir, profile)
    if _is_workspaces_container(resolved):
        return JSONResponse({"error": "The workspaces folder holds only profiles' own folders"},
                            status_code=403)
    if not os.path.isdir(resolved):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    results: list[dict] = []
    for field, value in form.multi_items():
        if not hasattr(value, "filename") or not getattr(value, "filename", None):
            continue
        results.append(await _write_upload(value, resolved))

    return JSONResponse({"results": results})


async def _upload_temp_files(request: Request):
    """Multipart upload into a conversation's temporary upload folder.

    Unlike ``/upload`` the client never supplies a server path: the temp dir
    is computed server-side from the authenticated profile + ``conversation_id``
    (``<CREMIND_SYSTEM_DIR>/<profile>/uploads_tmp/<conversation_id>/``), which
    already lives inside the ``system_file`` tool's allowed roots. Returns each
    file's absolute ``path`` so the client can attach it to the next message.

    Form fields:
      - ``conversation_id`` (required): the owning conversation.
      - Files: any number of file parts (any field name).
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    profile = getattr(request.user, "username", "") or ""
    if not profile:
        return JSONResponse({"error": "Profile is required"}, status_code=400)

    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"error": f"Failed to parse multipart form: {exc}"}, status_code=400
        )

    conversation_id = form.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return JSONResponse({"error": "conversation_id is required"}, status_code=400)

    # Ownership: only the conversation's owner may drop files into its slice.
    try:
        from app.events.runner import get_conversation_storage
        conv = await get_conversation_storage().get_conversation(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"upload-temp: failed to load conversation {conversation_id}"
        )
        conv = None
    if conv is None:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)
    if conv.get("profile") != profile:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    try:
        target_dir = conversation_tmp_dir(profile, conversation_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    max_bytes = max_upload_bytes()
    results: list[dict] = []
    for field, value in form.multi_items():
        if not hasattr(value, "filename") or not getattr(value, "filename", None):
            continue
        results.append(await _write_upload(value, target_dir, max_bytes=max_bytes))

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

    cid = conversation_id if isinstance(conversation_id, str) else None
    _row_id, context_key, denied = await _conversation_scope(request, cid, write=True)
    if denied is not None:
        return denied

    profile = _profile_of(request)
    resolved = _resolve_safe(path, context_key, profile)
    if resolved is None:
        return _access_denied(path, profile)
    if _is_protected_root(resolved, profile):
        return JSONResponse(
            {"error": "Refusing to delete a root folder (a working directory, the workspaces "
                      "folder or the system folder)"},
            status_code=400,
        )
    if not os.path.exists(resolved):
        return JSONResponse({"error": "Not found"}, status_code=404)
    # Unlike a credential store's parent, this parent holds OTHER profiles'
    # data: deleting it would erase every profile's manual pages or indexes.
    if _holds_protected_document_tree(resolved):
        return JSONResponse(
            {"error": "Refusing to delete a directory that holds other profiles' documents"},
            status_code=403,
        )
    # ...or their working directories.
    if _holds_foreign_workspace(resolved, profile):
        return JSONResponse(
            {"error": "Refusing to delete a directory that holds other profiles' working directories"},
            status_code=403,
        )

    try:
        if os.path.isdir(resolved) and not os.path.islink(resolved):
            shutil.rmtree(resolved)
        else:
            os.remove(resolved)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Delete failed for {resolved}")
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
    _row_id, context_key, denied = await _conversation_scope(request, cid, write=True)
    if denied is not None:
        return denied
    profile = _profile_of(request)
    src_resolved = _resolve_safe(src, context_key, profile)
    if src_resolved is None:
        return _access_denied(src, profile, " (src)")
    if _is_protected_root(src_resolved, profile):
        return JSONResponse(
            {"error": "Refusing to move a root folder (a working directory, the workspaces "
                      "folder or the system folder)"},
            status_code=400,
        )
    if not os.path.exists(src_resolved):
        return JSONResponse({"error": "src not found"}, status_code=404)
    # ``_resolve_safe`` refuses a store as the source; the subtree is the other
    # half. Carrying a store's *parent* -- a whole profile directory -- into the
    # user working directory takes the tokens out from under the name rule, and
    # every read route then serves them as ordinary files.
    if _holds_credential_store(src_resolved):
        return JSONResponse(
            {"error": "Refusing to move a directory that holds a credential store"},
            status_code=403,
        )
    # Same reasoning for the document trees: carrying their parent elsewhere
    # takes other profiles' manual pages and every index out from under the
    # rules that keep them to their owners.
    if _holds_protected_document_tree(src_resolved):
        return JSONResponse(
            {"error": "Refusing to move a directory that holds other profiles' documents"},
            status_code=403,
        )
    # And for other profiles' working directories: carried elsewhere, their
    # files would no longer sit in a folder the ownership rule knows.
    if _holds_foreign_workspace(src_resolved, profile):
        return JSONResponse(
            {"error": "Refusing to move a directory that holds other profiles' working directories"},
            status_code=403,
        )

    # Resolve the dest's *parent* against the allowlist (the dest itself
    # doesn't exist yet — realpath would resolve through the missing leaf).
    dest_parent = os.path.dirname(dest)
    if not dest_parent:
        return JSONResponse({"error": "dest must include a parent directory"}, status_code=400)
    dest_parent_resolved = _resolve_safe(dest_parent, context_key, profile)
    if dest_parent_resolved is None:
        return _access_denied(dest_parent, profile, " (dest)")
    if _is_workspaces_container(dest_parent_resolved):
        return JSONResponse({"error": "The workspaces folder holds only profiles' own folders"},
                            status_code=403)
    if not os.path.isdir(dest_parent_resolved):
        return JSONResponse({"error": "dest parent is not a directory"}, status_code=404)

    dest_resolved = os.path.join(dest_parent_resolved, os.path.basename(dest))
    # Only the parent was vetted above, so the leaf is still ours to check --
    # both so a move cannot land *inside* a store and so it cannot mint a new
    # directory wearing a store's name (which every listing would then hide).
    if _creates_credential_name(dest_resolved) or _is_credential_path(dest_resolved):
        return JSONResponse({"error": "Access denied (dest)"}, status_code=403)
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
        logger.exception(f"Move failed: {src_resolved} -> {dest_resolved}")
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
    _row_id, context_key, denied = await _conversation_scope(request, cid, write=True)
    if denied is not None:
        return denied
    parent = os.path.dirname(path)
    if not parent:
        return JSONResponse({"error": "path must include a parent"}, status_code=400)
    profile = _profile_of(request)
    parent_resolved = _resolve_safe(parent, context_key, profile)
    if parent_resolved is None:
        return _access_denied(parent, profile)
    if _is_workspaces_container(parent_resolved):
        return JSONResponse({"error": "The workspaces folder holds only profiles' own folders"},
                            status_code=403)
    if not os.path.isdir(parent_resolved):
        return JSONResponse({"error": "Parent is not a directory"}, status_code=404)

    target = os.path.join(parent_resolved, os.path.basename(path))
    # Same leaf rule as ``/move``'s dest: the parent passed the allowlist, the
    # name has still to be one the file tree will show back to its creator.
    if _creates_credential_name(target) or _is_credential_path(target):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if os.path.exists(target):
        return JSONResponse({"error": "Already exists"}, status_code=409)
    try:
        os.makedirs(target, exist_ok=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"mkdir failed for {target}")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "path": target})


async def _set_cwd(request: Request):
    """Set the conversation's working-directory override.

    Mirrors the write+publish performed by the
    ``change_working_directory`` tool ([app.tools.builtin.change_working_directory])
    so the UI can change the agent's effective cwd without a tool round-trip.

    JSON body: ``{conversation_id, path}``.

    The path must be an existing absolute directory; we deliberately do **not**
    require it to be inside ``_allowed_bases(profile)`` because the tool's
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

    row_id, context_key, denied = await _conversation_scope(
        request, conversation_id, write=True,
    )
    if denied is not None:
        return denied
    if row_id is None:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)

    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return JSONResponse({"error": "path must be absolute"}, status_code=400)
    new_path = os.path.realpath(expanded)
    if not os.path.isdir(new_path):
        return JSONResponse({"error": "path does not exist or is not a directory"},
                            status_code=400)
    # The one route that accepts a path outside the allowlist, so it is also
    # the one that cannot inherit the credential rule from ``_resolve_safe``
    # and has to state it. The override would not get the file routes to serve
    # a store -- they check the rule first, override or no -- but it aims the
    # agent's own shell, and every tool that follows the cwd, straight at it.
    if _is_credential_path(new_path):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    # ...and for the same reason the document rules: the agent's shell must
    # not be aimed at the index store or another profile's manual pages.
    profile = _profile_of(request)
    if _is_documents_index_path(new_path) or _is_other_profiles_authored_docs(
        new_path, profile
    ):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    # ...nor at another profile's exports or working directory. The file
    # routes would refuse to serve either however the override pointed, but
    # the agent's own tools and terminals follow the cwd.
    if _is_other_profiles_private_path(new_path, profile):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if _is_foreign_workspace(new_path, profile):
        return _access_denied(new_path, profile)

    # The agent reads the override back under the conversation's context_id,
    # while the durable column and the SSE channel belong to the row — one and
    # the same id for an ordinary chat, two different ones for a group seat.
    set_in_memory_override(context_key, new_path)

    # Persist alongside the in-memory write so the override survives a
    # server restart and reopening the conversation restores the same cwd
    # the user last selected (validated for existence in
    # ``hydrate_working_directory`` on next load).
    try:
        from app.events.runner import get_conversation_storage
        await persist_working_directory(
            row_id, new_path, get_conversation_storage(),
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"Failed to persist cwd override for {row_id}")

    try:
        await get_event_stream_bus().publish(
            row_id, "cwd", {"working_directory": new_path}
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"Failed to publish cwd change for {row_id}")

    return JSONResponse({"working_directory": new_path})


def get_file_routes() -> list[Route]:
    """Return routes for the file serving API.

    Every route in this module, and the credential enforcement point each one
    passes through. Written out in full because this is the easiest guard in
    the file to miss: the sandbox is a path-traversal check, so a route added
    later inherits the sandbox from ``_resolve_safe``/``_is_inside_allowed``
    *without* anyone having to think about credentials -- unless it resolves a
    path some other way, in which case it silently inherits neither.

    The same two functions carry the private-directory rule
    (``_is_other_profiles_private_path``), which needs the caller's profile and
    therefore a ``_profile_of(request)`` argument at every call site. Omitting
    it fails closed -- every private path is denied -- so a forgotten argument
    shows up as a 403 in a test rather than as another profile's export on the
    wire. They also carry the two document rules: the Documentation search
    index store (``storage/documents``, and ``storage/userdocs`` before the
    rename) is refused to everyone, and a profile's Cremind manual pages
    (``storage/cremind_documents/profiles/<uuid>``) to everyone but their
    owner (``_is_other_profiles_authored_docs``, same fail-closed default).
    ``/list`` and ``/watch`` hide both from their entries and events,
    ``/delete`` and ``/move`` refuse any directory that *contains* them, and
    ``POST /cwd`` restates both.

    And they carry the working-directory rule (``_is_foreign_workspace``):
    each profile's User Working Directory is its own, admin included, and the
    only working directory among a caller's allowed bases is its own. Checked
    before the bases, so a conversation override never widens it -- an admin
    reading a group-room seat's tree is refused the member's folder. ``/list``
    and ``/watch`` hide other profiles' folders (``_foreign_matcher``),
    ``/delete`` and ``/move`` refuse any working-directory root, the
    workspaces root and ``.deleted`` (``_is_protected_root``) and any
    directory containing another profile's folder
    (``_holds_foreign_workspace``), ``/upload``, ``/mkdir`` and ``/move`` never
    create an entry directly in the workspaces root
    (``_is_workspaces_container``), and ``POST /cwd`` restates the rule. A
    refusal under it is a 403 whose ``code`` is ``foreign_working_directory``
    (``_access_denied``), which the file panel shows as such.

    ``GET  /list``
        ``_is_inside_allowed`` on the directory, plus the per-entry filter that
        drops a store from the listing (name, and location for shared homes).
    ``GET  /cwd``
        No caller-supplied path: returns the caller's own working directory.
    ``POST /cwd``
        ``_is_credential_path`` **directly**. This route takes paths outside
        the allowlist on purpose, so it is the one place the rule is restated
        rather than inherited -- along with the document, private-directory
        and working-directory rules.
    ``GET  /watch``
        ``_is_inside_allowed`` on the directory, plus the per-event filter in
        ``_enqueue`` -- the observer is recursive, so the gate on the root is
        not enough on its own.
    ``GET  /open``
        ``_is_inside_allowed``.
    ``POST /upload``
        ``_resolve_safe`` on the destination directory. The leaf is a client
        filename written as a *file*, so it can never become a store.
    ``POST /upload-temp``
        No caller path at all: the directory is composed server-side from the
        authenticated profile and the (ownership-checked) conversation.
    ``DELETE /delete``
        ``_resolve_safe``, which refuses a store itself. Deliberately *not*
        subtree-checked: deleting a parent destroys a credential, it never
        discloses one, and deleting a profile removes these homes on purpose
        (``coding_cli_homes.remove_profile_cli_homes``).
    ``POST /move``
        Both directions. Source: ``_resolve_safe`` (never a store) plus
        ``_holds_credential_store`` (never a directory containing one).
        Destination: ``_resolve_safe`` on the parent plus
        ``_creates_credential_name`` / ``_is_credential_path`` on the leaf
        (never into a store, never creating one).
    ``POST /mkdir``
        ``_resolve_safe`` on the parent plus the same two leaf checks.
    ``GET  /{path:path}``
        ``_safe_resolve``, which applies the rule after resolving.
    """
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
        Route("/api/files/upload-temp", endpoint=_upload_temp_files, methods=["POST"]),
        Route("/api/files/delete", endpoint=_delete_entry, methods=["DELETE"]),
        Route("/api/files/move", endpoint=_move_entry, methods=["POST"]),
        Route("/api/files/mkdir", endpoint=_mkdir, methods=["POST"]),
        Route("/api/files/{path:path}", endpoint=_serve_file, methods=["GET"]),
    ]
