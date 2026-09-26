"""Per-conversation search-tool selection: the one policy every path shares.

The chat composer lets a person choose which of the four search sources the
agent may use in a conversation (or a group room). This module is the single
place that knows:

- the four sources, their labels and their fixed **priority order**
  (Documentation search → Cremind documentation search → Memory search → Web
  search);
- how a stored selection is read (``None`` = the default, every source; ``[]``
  = explicitly none; a list = those sources, in priority order);
- whether each source is *available* to a profile in a conversation of a given
  origin — desired selection and availability are kept apart, so a source that
  is temporarily unavailable never erases a preference;
- the deterministic search guidance built from the functions a run actually
  exposes, and the cache baseline / warning rules.

Selection is a conversation-level filter on top of everything else: it never
enables a tool the profile turned off, never starts indexing, never grants
document access, and never touches the global Cremind-documentation lock (the
registry still always *offers* that tool; a conversation may simply not use it).

Import discipline: stdlib only at module level. Registry, gate and feature
lookups are imported lazily inside the functions that need them, so this module
is cheap to import from the API layer, the agent and the tests.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Container, Iterable, Mapping, Sequence

# ── The catalog ────────────────────────────────────────────────────────────

DOCUMENTATION_SEARCH = "documentation_search"
CREMIND_DOCUMENTATION_SEARCH = "cremind_documentation_search"
MEMORY_SEARCH = "memory_search"
WEB_SEARCH = "web_search"

# Fixed priority order. Also the order the ids are stored and returned in.
SEARCH_TOOL_IDS: tuple[str, ...] = (
    DOCUMENTATION_SEARCH,
    CREMIND_DOCUMENTATION_SEARCH,
    MEMORY_SEARCH,
    WEB_SEARCH,
)
_PRIORITY = {tool_id: i for i, tool_id in enumerate(SEARCH_TOOL_IDS)}


@dataclass(frozen=True)
class SearchToolInfo:
    id: str
    label: str
    description: str


CATALOG: dict[str, SearchToolInfo] = {
    DOCUMENTATION_SEARCH: SearchToolInfo(
        DOCUMENTATION_SEARCH,
        "Documentation search",
        "Your own indexed files (local folder and Google Drive).",
    ),
    CREMIND_DOCUMENTATION_SEARCH: SearchToolInfo(
        CREMIND_DOCUMENTATION_SEARCH,
        "Cremind documentation search",
        "Cremind's own manuals: features, settings and the cremind CLI.",
    ),
    MEMORY_SEARCH: SearchToolInfo(
        MEMORY_SEARCH,
        "Memory search",
        "Facts and preferences remembered from past conversations.",
    ),
    WEB_SEARCH: SearchToolInfo(
        WEB_SEARCH,
        "Web search",
        "The public internet, as a last resort.",
    ),
}

# Short reasons a visible source cannot be used right now.
REASON_TURNED_OFF = "Turned off in Tools settings."
REASON_NOT_LOADED = "Not available on this server."
REASON_NO_FUNCTIONS = "All of its functions are turned off in Tools settings."
REASON_VARIES = "Availability varies by agent in this room."

CACHE_WARNING = (
    "Changing search tools may reduce prompt-cache reuse on the next response "
    "and increase input-token cost."
)
PENDING_NOTICE = (
    "Saved for the next response. The current response keeps its existing search tools."
)


class SelectionError(ValueError):
    """An update body that is not a valid search-tool selection."""


def normalize_selection(value: Any) -> list[str] | None:
    """Validate ``value`` and return its stored form.

    ``None`` stays ``None`` (the default: every source). A list must hold
    distinct, known ids; it comes back in priority order. A list naming every
    source is stored as ``None`` too — "all four" and "the default" are the same
    choice, so Reset and Select-all compare equal and a no-op stays a no-op.
    Raises :class:`SelectionError` for anything else.
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise SelectionError("enabled must be a list of search-tool ids or null")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise SelectionError("every search-tool id must be a string")
        if item not in _PRIORITY:
            raise SelectionError(
                f"unknown search tool {item!r} (expected one of: {', '.join(SEARCH_TOOL_IDS)})"
            )
        if item in seen:
            raise SelectionError(f"search tool {item!r} is listed twice")
        seen.add(item)
    ordered = sorted(seen, key=_PRIORITY.__getitem__)
    if len(ordered) == len(SEARCH_TOOL_IDS):
        return None
    return ordered


def read_stored(value: Any) -> list[str] | None:
    """A selection read back from storage, tolerating anything a hand-edited
    or older row may hold: unknown ids are dropped, never raised."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
        if value is None:
            return None
    if not isinstance(value, (list, tuple)):
        return None
    known = {v for v in value if isinstance(v, str) and v in _PRIORITY}
    ordered = sorted(known, key=_PRIORITY.__getitem__)
    return None if len(ordered) == len(SEARCH_TOOL_IDS) else ordered


def desired_ids(selection: Sequence[str] | None) -> list[str]:
    """The sources a stored selection asks for, in priority order."""
    if selection is None:
        return list(SEARCH_TOOL_IDS)
    return [t for t in SEARCH_TOOL_IDS if t in set(selection)]


def is_search_tool(tool_id: str | None) -> bool:
    return bool(tool_id) and tool_id in _PRIORITY


# ── Availability ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Availability:
    """Whether one source can be used by one profile in one kind of conversation.

    ``hidden`` is only ever set for Documentation search: it is absent from the
    picker when its feature is off, blocked by the administrator, or not allowed
    for the conversation's origin. The other sources stay visible, disabled,
    with a short ``reason``.
    """

    id: str
    available: bool
    reason: str | None = None
    hidden: bool = False


def origin_key(message_origin: Any = None, *, conversation_kind: str | None = None,
               channel_kind: str | None = None) -> str:
    """The ``allow_in`` key (``web_cli`` / ``channels`` / ``rooms``) of a
    conversation, from a run's ``message_origin`` or, outside a run, from the
    conversation row itself."""
    from app.documents.gate import ORIGIN_CHANNELS, ORIGIN_ROOMS, ORIGIN_WEB_CLI, origin_class

    if message_origin is not None:
        return origin_class(message_origin) if isinstance(message_origin, dict) else str(message_origin)
    if conversation_kind == "group_chat":
        return ORIGIN_ROOMS
    if channel_kind == "group":
        return ORIGIN_ROOMS
    if channel_kind == "channel":
        return ORIGIN_CHANNELS
    return ORIGIN_WEB_CLI


def _documentation_search_offered(profile: str, origin: str) -> bool:
    """Documentation search is offered only when the administrator allowed it,
    the profile turned a source on, and this origin is allowed — exactly the
    rule the agent's own gate applies (:func:`documents_tool_available`), so
    the picker never hides a source a run would still expose, or offers one it
    would not. Keyword-only indexes count: embeddings being off never hides it,
    and neither do missing optional extras (plain-text files still index)."""
    try:
        from app.documents.gate import documents_tool_available

        return bool(documents_tool_available(profile, origin))
    except Exception:  # noqa: BLE001
        return False


def availability(profile: str, origin: str, *, registry: Any = None) -> dict[str, Availability]:
    """Availability of every source for ``profile`` in a conversation of
    ``origin``, keyed by id. Never raises: a failure reads as unavailable."""
    if registry is None:
        try:
            from app.tools.registry import get_tool_registry

            registry = get_tool_registry()
        except Exception:  # noqa: BLE001
            registry = None
    enabled: set[str] = set()
    registered: set[str] = set()
    if registry is not None:
        try:
            enabled = {t.tool_id for t in registry.tools_for_profile(profile)}
        except Exception:  # noqa: BLE001
            enabled = set()
        for tool_id in SEARCH_TOOL_IDS:
            try:
                if registry.get(tool_id) is not None:
                    registered.add(tool_id)
            except Exception:  # noqa: BLE001
                pass

    out: dict[str, Availability] = {}
    for tool_id in SEARCH_TOOL_IDS:
        if tool_id == DOCUMENTATION_SEARCH and not _documentation_search_offered(profile, origin):
            out[tool_id] = Availability(tool_id, False, None, hidden=True)
            continue
        if tool_id not in registered:
            out[tool_id] = Availability(tool_id, False, REASON_NOT_LOADED,
                                        hidden=tool_id == DOCUMENTATION_SEARCH)
            continue
        if tool_id not in enabled:
            out[tool_id] = Availability(tool_id, False, REASON_TURNED_OFF)
            continue
        if not _has_enabled_leaf(registry, profile, tool_id):
            out[tool_id] = Availability(tool_id, False, REASON_NO_FUNCTIONS)
            continue
        out[tool_id] = Availability(tool_id, True)
    return out


def _has_enabled_leaf(registry: Any, profile: str, tool_id: str) -> bool:
    """False only when the profile turned off every function of the group
    (``leaves_for_profile`` → ``{"leaves": [{"enabled": …}, …]}``)."""
    try:
        payload = registry.leaves_for_profile(profile, tool_id)
    except Exception:  # noqa: BLE001 — no leaf state: the group decides alone
        return True
    leaves = payload.get("leaves") if isinstance(payload, dict) else None
    if not leaves:
        return True
    return any(bool(leaf.get("enabled", True)) for leaf in leaves if isinstance(leaf, dict))


def effective_ids(selection: Sequence[str] | None, avail: Mapping[str, Availability]) -> list[str]:
    """What a run will actually expose: the selection intersected with what is
    available, in priority order."""
    return [t for t in desired_ids(selection) if avail.get(t) is not None and avail[t].available]


# ── A run's frozen snapshot ────────────────────────────────────────────────


@dataclass(frozen=True)
class Snapshot:
    """The selection a run adopted when it started. Immutable: saving new
    choices mid-run never changes an agent that already holds one.

    ``source`` is ``conversation``, ``room`` or ``default`` (no stored row).
    """

    selection: tuple[str, ...] | None = None
    version: int = 0
    source: str = "default"

    @classmethod
    def of(cls, selection: Any, version: int | None = 0, source: str = "conversation") -> "Snapshot":
        """A snapshot of a stored selection, in whatever shape the row holds
        (``None``, a list, or the JSON text of either) — see :func:`read_stored`."""
        sel = read_stored(selection)
        try:
            ver = int(version or 0)
        except (TypeError, ValueError):
            ver = 0
        return cls(tuple(sel) if sel is not None else None, ver, source)

    def desired(self) -> list[str]:
        return desired_ids(self.selection)

    def allows(self, tool_id: str | None) -> bool:
        """Whether a tool group may be exposed. Anything that is not one of the
        four search sources is outside this policy and always allowed."""
        if not is_search_tool(tool_id):
            return True
        return tool_id in self.desired()


DEFAULT_SNAPSHOT = Snapshot()


def filter_tools(tools: Iterable[Any], snapshot: Snapshot | None) -> list[Any]:
    """``tools`` without the search groups ``snapshot`` does not allow. A
    removed group takes all its functions with it (reading, research…)."""
    snap = snapshot or DEFAULT_SNAPSHOT
    return [t for t in tools if snap.allows(getattr(t, "tool_id", None))]


async def snapshot_for_conversation(
    conversation_id: str | None = None,
    *,
    conv: Mapping[str, Any] | None = None,
    conversation_storage: Any = None,
) -> Snapshot:
    """The selection a run on this conversation adopts, read once at its start.

    Shared by every path that starts a run (the stream runner, the A2A
    executor, the compaction fold) so they can never disagree about which
    selection applies:

    - a **group seat** (``context_id`` ``group:<gid>:<profile>``) reads its
      ROOM's shared selection — the seat row's own columns are never consulted,
      only its per-seat cache baseline lives there;
    - every other conversation reads its own row
      (``ConversationStorage.get_search_tools_row``);
    - no row, no stored choice, a room that is gone, or anything unreadable
      reads as the default (every source) — a missing selection must never
      fail a run or silently narrow it.

    ``conv`` is the conversation row when the caller already holds it (saves a
    read for ``kind``/``context_id``); ``conversation_storage`` defaults to the
    process singleton. Never raises.
    """
    from app.utils.logger import logger

    cid = conversation_id or (conv or {}).get("id")
    storage = conversation_storage
    if storage is None:
        try:
            from app.storage import get_conversation_storage

            storage = get_conversation_storage()
        except Exception:  # noqa: BLE001 — storage not up (tests, early boot): defaults
            storage = None

    row: Mapping[str, Any] | None = None
    getter = getattr(storage, "get_search_tools_row", None) if storage is not None else None
    if cid and getter is not None:
        try:
            row = await getter(cid)
        except Exception:  # noqa: BLE001
            logger.debug(f"[search_tools] selection read failed for conv={cid}", exc_info=True)
            row = None

    context_id = (row or {}).get("context_id") or (conv or {}).get("context_id")
    try:
        from app.groups.shadow import group_id_from_context

        group_id = group_id_from_context(context_id)
    except Exception:  # noqa: BLE001
        group_id = None
    if group_id:
        try:
            from app.storage import get_group_chat_storage

            groups = get_group_chat_storage()
            # The narrow read when the storage has it (no member rows), else
            # the full group — both carry the two columns.
            reader = getattr(groups, "get_group_search_tools", None) or groups.get_group
            group = await reader(group_id)
        except Exception:  # noqa: BLE001
            logger.debug(f"[search_tools] room selection read failed for group={group_id}", exc_info=True)
            group = None
        if not group:
            return DEFAULT_SNAPSHOT
        return Snapshot.of(group.get("search_tools"), group.get("search_tools_version"), "room")

    # A caller-held row that already carries the columns is as good as a read.
    if row is None and conv is not None and "search_tools" in conv:
        row = conv
    if not row:
        return DEFAULT_SNAPSHOT
    return Snapshot.of(row.get("search_tools"), row.get("search_tools_version"), "conversation")


# ── Historical function names ──────────────────────────────────────────────

# Function names older conversations hold in their replayed history. A model
# that copies one is dispatched to today's function — dispatch only (no
# schema is ever sent under these names), and only when today's tool group is
# exposed in this run, so an alias can never bypass the selection or a gate.
HISTORICAL_FUNCTION_ALIASES: dict[str, tuple[str, str]] = {
    # Cremind's manual search was ``documentation_search`` before the rename.
    "documentation_search__search_documentation": (CREMIND_DOCUMENTATION_SEARCH, "search_documentation"),
    "documentation_search__read_documentation_section": (CREMIND_DOCUMENTATION_SEARCH, "read_documentation_section"),
    # The personal-document search was ``user_documents``.
    "user_documents__find_files": (DOCUMENTATION_SEARCH, "find_files"),
    "user_documents__search": (DOCUMENTATION_SEARCH, "search"),
    "user_documents__read": (DOCUMENTATION_SEARCH, "read"),
    "user_documents__research": (DOCUMENTATION_SEARCH, "research"),
}


def resolve_historical_aliases(present: Container[str]) -> dict[str, str]:
    """``{historical name: today's function name}`` for the aliases whose
    target is ``present`` — the functions this step can actually dispatch.

    ``present`` is the step's dispatch map (or any container of function
    names), built AFTER every gate, the selection and the profile's per-leaf
    switches, so an alias exists exactly when its target does: a source the
    conversation turned off, a gated-off group or a disabled function takes its
    historical names with it. A historical name that is itself a live function
    (never the case today) is left alone — the live function wins.
    """
    out: dict[str, str] = {}
    for alias, (tool_id, leaf) in HISTORICAL_FUNCTION_ALIASES.items():
        if alias in present:
            continue
        target = _leaf_name(tool_id, leaf)
        if target in present:
            out[alias] = target
    return out


def _leaf_name(tool_id: str, leaf: str) -> str:
    """``app.tools.base.make_leaf_name`` without importing the tools package
    (this module stays stdlib-only at import). Kept identical by a test."""
    return leaf if leaf == tool_id else f"{tool_id}__{leaf}"


# ── Guidance ───────────────────────────────────────────────────────────────

_GUIDANCE_ROLE = {
    DOCUMENTATION_SEARCH: "the user's own indexed files — for questions about their documents, notes, reports and records",
    CREMIND_DOCUMENTATION_SEARCH: "Cremind's own manuals — for questions about Cremind itself: its features, settings and the `cremind` CLI",
    MEMORY_SEARCH: "long-term memory — for background about the user that is not already in this conversation",
}
# Web search's role depends on whether anything ranks above it: "the fallback
# when the sources above cannot answer" would point at nothing in a web-only
# conversation.
_WEB_ROLE_FALLBACK = (
    "the public internet — the fallback when the sources above cannot answer, or when the user "
    "asks for fresh or external information"
)
_WEB_ROLE_ALONE = (
    "the public internet — for fresh or external information you do not already have"
)


def build_priority_guidance(exposed: Mapping[str, Sequence[str]]) -> str:
    """Deterministic search guidance for the sources a run exposes.

    ``exposed`` maps a source id to the function names this run actually sends
    for it (only non-empty entries count). Sources are listed in priority order
    and each names only its real functions, so the text can never point at a
    function the model does not have — a source the conversation turned off, a
    gated-off group or a function the profile disabled is simply not in it.
    Identical inputs give identical text, which keeps the prompt prefix
    byte-stable for caching. Returns "" when nothing is exposed (a conversation
    with every search source off is a normal state, not an error). Wrapped
    ``'\\n...\\n'`` like the agent's other system-prompt blocks.
    """
    rows = [(t, list(exposed.get(t) or [])) for t in SEARCH_TOOL_IDS if exposed.get(t)]
    if not rows:
        return ""
    lines = [
        "SEARCH SOURCES — IN PRIORITY ORDER: these are the only search functions available in "
        "this conversation" + (", most preferred first." if len(rows) > 1 else "."),
    ]
    for n, (tool_id, fns) in enumerate(rows, start=1):
        names = ", ".join(f"`{fn}`" for fn in fns)
        role = _GUIDANCE_ROLE.get(tool_id) or (_WEB_ROLE_FALLBACK if n > 1 else _WEB_ROLE_ALONE)
        lines.append(f"{n}. {CATALOG[tool_id].label} ({names}): {role}.")
    if len(rows) > 1:
        lines.append(
            "Pick the source that fits the request — you do not have to search every source for "
            "every message, and casual conversation needs no search. Skip a source that is "
            "irrelevant to the question and go to the next one in this order; fall back to a later "
            "source only when the relevant earlier ones come back empty. Do not claim something "
            "cannot be found or done until you have tried the relevant sources above."
        )
    else:
        lines.append(
            "Use it when the request needs information you do not already have; casual "
            "conversation needs no search. Do not claim something cannot be found or done until "
            "you have tried it."
        )
    return "\n" + "\n".join(lines) + "\n"


# ── Cache baseline and warning ─────────────────────────────────────────────

BASELINE_VERSION = 1


def fingerprint(function_specs: Iterable[tuple[str, Any]], guidance: str) -> str:
    """A digest of the selection-sensitive part of the prompt: the search
    functions' names and schemas and the search guidance text."""
    payload = {
        "functions": sorted(
            ([name, schema] for name, schema in function_specs),
            key=lambda pair: pair[0],
        ),
        "guidance": guidance or "",
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_baseline(*, version: int, effective: Sequence[str], fingerprint_hex: str,
                  now_ms: int | None = None) -> dict[str, Any]:
    """The ``search_cache_baseline`` row value recorded at a main-model request."""
    return {
        "v": BASELINE_VERSION,
        "version": int(version),
        "effective": [t for t in SEARCH_TOOL_IDS if t in set(effective)],
        "fingerprint": fingerprint_hex,
        "at": int(now_ms if now_ms is not None else time.time() * 1000),
    }


def read_baseline(value: Any) -> dict[str, Any] | None:
    """A stored baseline, or ``None`` when absent or unreadable."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict) or not isinstance(value.get("effective"), list):
        return None
    return value


def baseline_effective(baseline: Mapping[str, Any] | None) -> list[str] | None:
    if not baseline:
        return None
    eff = baseline.get("effective")
    if not isinstance(eff, list):
        return None
    return [t for t in SEARCH_TOOL_IDS if t in set(eff)]


def edit_may_miss_cache(
    *,
    before_effective: Sequence[str],
    after_effective: Sequence[str],
    baseline: Mapping[str, Any] | None,
    prior_activity: bool,
) -> bool:
    """Whether an edit should carry the cache warning.

    - Not when the edit leaves what the next response exposes unchanged (a
      no-op, or a change to an unavailable source only).
    - Not when the result restores what the last main-model request sent.
    - For a conversation with a baseline: when the result differs from it.
    - Without a baseline (a pre-upgrade conversation): conservatively, when
      the conversation already had main-model activity.
    - Never for a new conversation (no baseline, no activity).
    """
    after = list(after_effective)
    if list(before_effective) == after:
        return False
    base = baseline_effective(baseline)
    if base is not None:
        return base != after
    return bool(prior_activity)


def pending_next_response(
    version: int,
    baseline: Mapping[str, Any] | None,
    running_versions: Iterable[int] = (),
) -> bool:
    """A saved selection no response has adopted yet.

    Either the last main-model request ran on an older version (the baseline
    records it), or a response is running right now on an older snapshot —
    which covers a conversation's first response, saved-over before it reached
    the model and so before it recorded any baseline. Cleared once a new
    response starts on the saved version."""
    try:
        target = int(version)
    except (TypeError, ValueError):
        return False
    if any(int(v) < target for v in running_versions):
        return True
    if not baseline:
        return False
    try:
        return int(baseline.get("version", 0)) < target
    except (TypeError, ValueError):
        return False


# ── Responses running right now ────────────────────────────────────────────
#
# The snapshot version each in-flight run adopted, per conversation. Process-
# local on purpose: it only refines the "saved for the next response" flag
# while a response is running on this server; the durable answer is the
# baseline. Registered and released by the stream runner around a run.

_running_lock = threading.Lock()
_running: dict[str, dict[str, int]] = {}


def begin_run(conversation_id: str, run_id: str, version: int) -> None:
    with _running_lock:
        _running.setdefault(conversation_id, {})[run_id] = int(version)


def end_run(conversation_id: str, run_id: str) -> None:
    with _running_lock:
        runs = _running.get(conversation_id)
        if runs is None:
            return
        runs.pop(run_id, None)
        if not runs:
            _running.pop(conversation_id, None)


def running_versions(conversation_id: str) -> list[int]:
    """Snapshot versions of the responses running in this conversation now."""
    with _running_lock:
        return list((_running.get(conversation_id) or {}).values())


# ── API state ──────────────────────────────────────────────────────────────


@dataclass
class ToolRow:
    id: str
    label: str
    description: str
    available: bool
    unavailable_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass
class State:
    """The body every search-tools endpoint returns (``SearchToolsState``)."""

    version: int
    enabled: list[str]
    effective: list[str]
    tools: list[ToolRow] = field(default_factory=list)
    pending_next_response: bool = False
    cache_warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": list(self.enabled),
            "effective": list(self.effective),
            "tools": [row.as_dict() for row in self.tools],
            "pending_next_response": self.pending_next_response,
            "cache_warning": self.cache_warning,
        }


def tool_rows(avail: Mapping[str, Availability]) -> list[ToolRow]:
    """Picker rows in priority order. Documentation search is left out while
    it is hidden (see :class:`Availability`)."""
    rows: list[ToolRow] = []
    for tool_id in SEARCH_TOOL_IDS:
        a = avail.get(tool_id)
        if a is None or a.hidden:
            continue
        info = CATALOG[tool_id]
        rows.append(ToolRow(tool_id, info.label, info.description, a.available,
                            None if a.available else (a.reason or REASON_NOT_LOADED)))
    return rows


def build_state(
    *,
    selection: Sequence[str] | None,
    version: int,
    avail: Mapping[str, Availability],
    pending: bool = False,
    cache_warning: str | None = None,
) -> State:
    """The ordinary-conversation state. ``enabled`` echoes the stored desire
    (unavailable sources included — a temporary outage never rewrites it);
    ``effective`` is what the next response would expose."""
    return State(
        version=int(version),
        enabled=desired_ids(selection),
        effective=effective_ids(selection, avail),
        tools=tool_rows(avail),
        pending_next_response=bool(pending),
        cache_warning=cache_warning,
    )


def merge_room_availability(per_member: Sequence[Mapping[str, Availability]]) -> dict[str, Availability]:
    """Aggregate availability for a group room, from each member agent's own.

    A source is available when at least one member agent can use it (its
    reason then notes that availability varies when not all can). Documentation
    search is shown when at least one member can use it. Nothing about any one
    profile — which folders it indexed, its models, its tool settings — is
    carried, only the aggregate.
    """
    out: dict[str, Availability] = {}
    for tool_id in SEARCH_TOOL_IDS:
        rows = [m.get(tool_id) for m in per_member if m.get(tool_id) is not None]
        usable = [r for r in rows if r.available]
        if usable:
            varies = len(usable) < len(rows)
            out[tool_id] = Availability(tool_id, True, REASON_VARIES if varies else None)
        elif tool_id == DOCUMENTATION_SEARCH:
            out[tool_id] = Availability(tool_id, False, None, hidden=True)
        else:
            out[tool_id] = Availability(tool_id, False, REASON_TURNED_OFF if rows else REASON_NOT_LOADED)
    return out


def room_tool_rows(avail: Mapping[str, Availability]) -> list[ToolRow]:
    """Like :func:`tool_rows`, but an available source whose availability
    varies by agent keeps that note as its (informational) reason."""
    rows: list[ToolRow] = []
    for tool_id in SEARCH_TOOL_IDS:
        a = avail.get(tool_id)
        if a is None or a.hidden:
            continue
        info = CATALOG[tool_id]
        rows.append(ToolRow(tool_id, info.label, info.description, a.available,
                            a.reason if (a.reason or not a.available) else None))
    return rows


__all__ = [
    "CACHE_WARNING",
    "CATALOG",
    "CREMIND_DOCUMENTATION_SEARCH",
    "DEFAULT_SNAPSHOT",
    "DOCUMENTATION_SEARCH",
    "HISTORICAL_FUNCTION_ALIASES",
    "MEMORY_SEARCH",
    "PENDING_NOTICE",
    "SEARCH_TOOL_IDS",
    "WEB_SEARCH",
    "Availability",
    "SearchToolInfo",
    "SelectionError",
    "Snapshot",
    "State",
    "ToolRow",
    "availability",
    "baseline_effective",
    "build_priority_guidance",
    "build_state",
    "desired_ids",
    "edit_may_miss_cache",
    "effective_ids",
    "filter_tools",
    "fingerprint",
    "is_search_tool",
    "make_baseline",
    "merge_room_availability",
    "normalize_selection",
    "origin_key",
    "begin_run",
    "end_run",
    "pending_next_response",
    "running_versions",
    "read_baseline",
    "read_stored",
    "resolve_historical_aliases",
    "room_tool_rows",
    "snapshot_for_conversation",
    "tool_rows",
]
