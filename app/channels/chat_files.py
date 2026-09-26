"""Files the agent sends into the chat it is answering, because someone there
asked for them.

A channel reply is text. A file reaches the platform only when something sends
it on purpose (see :meth:`app.channels.base.BaseChannelAdapter._forward_reply`),
and for the chat the agent is IN — the private chat a person wrote from, or the
platform group it is answering — that something is the ``send_files_to_chat``
tool, which calls into this module. It never addresses anyone else: the
destination is read off the conversation the turn is running in, never taken
from the model, so a person in a group can get a file posted into that group
and nobody can get one posted anywhere else through it. Messaging other people
is ``send_channel_message``'s job, under its own approval rules.

Most of this module is its failure modes, because each one ends with a file on
a real person's phone or with nothing there at all:

- **Where.** No live turn; a conversation that is not a channel chat (the web
  UI, an automation's hidden run, a Cremind group seat); a channel that is not
  running; a group that was blocked or never approved; group chats switched off
  on the channel; a person the channel no longer admits; a transport that
  cannot carry files. Each is refused before a byte moves, with a reason the
  agent can pass on.
- **What.** Every file must pass
  :func:`~app.channels.attachments.validate_outbound_paths` (the profile's own
  folders, never Cremind's data, a hidden file, a credential or another
  profile's folder), be non-empty, and fit both the platform's cap and the
  server's upload cap. All of them are checked before any is sent, so a batch
  starts whole or not at all: "send the three reports" never delivers two and
  a refusal.
- **How it went.** Files go out one at a time and each outcome is reported:
  sent; failed, with the platform's reason; or unconfirmed — handed over but
  never acknowledged, so a retry could post it twice. Two failures in a row
  stop the batch (the chat is unreachable and the rest would fail the same
  way), and so does the clock, well before the tool call's own timeout would
  cut the tool off without a report.
- **Twice.** A file already delivered into this chat earlier in the same turn
  is not sent again: a model retrying a slow call must not post the same report
  twice. A later turn may send it again — that is somebody asking again.

A file posted into a group is not a room post for the group's loop brakes, and
it is not relayed to Cremind's other agents in the room
(:mod:`app.channels.groups.relay`). Both exist for conversation — runaway agent
chatter, agents that cannot hear each other — and the turn that sent the file
already speaks, and is counted and relayed, through its written answer.
Counting each file would let "send the ten reports" trip the per-minute brake
and silence the agent for the follow-up question.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from app.channels.exceptions import ChannelNotImplemented, DeliveryUnconfirmed
from app.channels.reply_target import ReplyTarget, group_target, sender_target
from app.utils.logger import logger

# Per call. Enough for "the three reports and the invoice", few enough that a
# mistaken call is a bounded accident in a room full of people.
MAX_FILES_PER_CALL = 10

# Per turn and chat, across calls: what one answer may carry. A model that
# loops on the tool would otherwise post into a group until the turn ended.
MAX_FILES_PER_TURN = 20

# Files in a row that fail before the rest of the batch is given up on. One
# failure can be the file (too big for this platform, a format it refuses); two
# in a row is the chat — the account was removed, the session dropped — and the
# remaining files would fail identically.
_STOP_AFTER_FAILURES = 2

# Seconds between two files into one chat. Telegram lets a bot post about one
# message a second into a group before flood control starts, and WhatsApp reads
# a burst from a personal account as spam; the rest take a short gap in stride.
_PACING_SECONDS = {"telegram": 1.0, "whatsapp": 2.0}
_DEFAULT_PACING_SECONDS = 0.5

# Time kept back from the tool call's timeout — this much, or a quarter of a
# short timeout. Past it no new upload starts, and the one in flight is cut off
# when it runs into it, so the call always ends with this module's report rather
# than the adapter's bare "Timeout", which would say nothing about the files
# that did go out.
_DEADLINE_MARGIN_SECONDS = 45.0
_DEADLINE_MARGIN_SHARE = 0.25

# How many turns' delivery records are kept for the duplicate guard. A record is
# only useful while its turn is running; this bounds what finished turns leave.
_TURNS_REMEMBERED = 256

# Outcomes, as reported per file.
SENT = "sent"
FAILED = "failed"
UNCONFIRMED = "unconfirmed"
NOT_SENT = "not_sent"
ALREADY_SENT = "already_sent"

KIND_GROUP = "group"
KIND_PRIVATE = "private"


class ChatFilesError(Exception):
    """A refusal that is the tool's answer: a stable ``code`` and a message
    written for the agent to act on (and, usually, to pass on)."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_result(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


@dataclass(frozen=True)
class ChatRef:
    """The chat a turn is answering, resolved to a live adapter."""

    adapter: Any
    target: ReplyTarget
    kind: str       # KIND_GROUP | KIND_PRIVATE
    name: str       # the group's title, or the person's display name
    platform: str   # "Telegram", "Zalo", ...

    @property
    def key(self) -> str:
        """What the duplicate guard files deliveries under. The channel is part
        of it: two channels can each have a sender with the same platform id."""
        return f"{self.adapter.channel_id}\x00{self.target.key}"

    def describe(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "platform": self.platform}


# ── where ──────────────────────────────────────────────────────────────────


async def resolve_chat(
    profile: str,
    conversation_id: Optional[str],
    *,
    registry: Any,
    context_id: Optional[str] = None,
) -> ChatRef:
    """The chat ``conversation_id`` is with, on a channel that can take files.

    ``conversation_id`` comes from the live run binding. ``context_id`` — the
    one the tool adapter injects — is the fallback when there is none: a
    platform group's conversation is keyed ``channel_group:<id>``, and a private
    chat's context is its own conversation id. Neither ever comes from the
    model, which is what keeps a file going only where the turn is.

    Raises :class:`ChatFilesError` for every "not here" case.
    """
    group = await _group_for(conversation_id, context_id)
    if group is not None:
        return _group_chat(profile, group, registry)
    sender = await _sender_for(conversation_id, context_id, registry)
    if sender is not None:
        return _private_chat(profile, sender, registry)
    raise ChatFilesError(
        "NotInAChat",
        "This conversation is not a chat on a messaging channel, so there is "
        "nowhere to send files. send_files_to_chat only works while answering "
        "someone on Telegram, Zalo, WhatsApp and the like — in their private "
        "chat or in a group. In the web UI the files are already visible to "
        "the user.",
    )


async def _group_for(
    conversation_id: Optional[str], context_id: Optional[str],
) -> Optional[dict]:
    from app.channels.groups.origin import group_id_from_context
    from app.storage import get_channel_group_storage

    storage = get_channel_group_storage()
    if conversation_id:
        group = await storage.get_group_by_conversation(conversation_id)
        if group is not None:
            return group
    group_id = group_id_from_context(context_id)
    if group_id:
        return await storage.get_group(group_id)
    return None


async def _sender_for(
    conversation_id: Optional[str], context_id: Optional[str], registry: Any,
) -> Optional[dict]:
    storage = getattr(registry, "storage", None)
    if storage is None:
        return None
    for candidate in (conversation_id, context_id):
        if candidate:
            sender = await storage.get_sender_by_conversation(candidate)
            if sender is not None:
                return sender
    return None


def _group_chat(profile: str, group: dict, registry: Any) -> ChatRef:
    from app.channels.groups.constants import STATUS_APPROVED

    title = str(group.get("title") or group.get("platform_chat_id") or "this group")
    if group.get("profile") != profile:
        raise _not_yours()
    status = str(group.get("status") or "")
    if status != STATUS_APPROVED:
        raise ChatFilesError(
            "GroupNotApproved",
            f"The group {title!r} is {status or 'not approved'}, so nothing can "
            "be posted there. The operator approves groups on the Channels page "
            "or with `cremind channels groups approve`.",
            group_status=status or None,
        )
    adapter = _live_adapter(registry, group.get("channel_id"))
    if adapter.profile != profile:
        raise _not_yours()
    if not adapter.groups_enabled():
        raise ChatFilesError(
            "GroupChatsOff",
            "Group chats are switched off on this channel, so nothing can be "
            "posted into its groups.",
        )
    target = group_target(group)
    if not target.address:
        raise ChatFilesError(
            "GroupUnreachable",
            f"The group {title!r} has no platform chat id on record, so it "
            "cannot be addressed.",
        )
    return _with_file_support(ChatRef(
        adapter=adapter, target=target, kind=KIND_GROUP, name=title,
        platform=_platform_name(adapter.channel_type),
    ))


def _private_chat(profile: str, sender: dict, registry: Any) -> ChatRef:
    adapter = _live_adapter(registry, sender.get("channel_id"))
    if adapter.profile != profile:
        raise _not_yours()
    # The turn could only have started for an admitted sender. Checked again
    # because access can be revoked while it runs, and a file is not a thing
    # to hand someone the operator has just shut out.
    try:
        auth = adapter._subscribe_auth()  # noqa: SLF001
    except Exception:  # noqa: BLE001 — an unreadable gate is not an open one
        auth = ""
    if auth != "open" and not sender.get("authenticated"):
        raise ChatFilesError(
            "NotAllowed",
            "This person no longer has access to the channel, so nothing is "
            "sent to them.",
        )
    sender_id = str(sender.get("sender_id") or "")
    if not sender_id:
        raise ChatFilesError(
            "NotInAChat", "This chat has no platform address on record.",
        )
    return _with_file_support(ChatRef(
        adapter=adapter, target=sender_target(sender_id), kind=KIND_PRIVATE,
        name=str(sender.get("display_name") or sender_id),
        platform=_platform_name(adapter.channel_type),
    ))


def _live_adapter(registry: Any, channel_id: Any) -> Any:
    adapter = registry.get_adapter(str(channel_id or "")) if channel_id else None
    if adapter is None:
        raise ChatFilesError(
            "ChannelNotRunning",
            "The channel this chat is on is not running right now (stopped, "
            "disabled or reconnecting), so nothing could be sent. Try again "
            "once it is back.",
        )
    return adapter


def _with_file_support(chat: ChatRef) -> ChatRef:
    if not type(chat.adapter).supports_file_send:
        raise ChatFilesError(
            "FilesUnsupported",
            f"This {chat.platform} channel cannot send files, so nothing was "
            "sent. Tell them where else they can get the file.",
            chat=chat.describe(),
        )
    return chat


def _not_yours() -> ChatFilesError:
    return ChatFilesError(
        "NotYourChat",
        "This chat belongs to another profile's channel, so nothing is sent.",
    )


def _platform_name(channel_type: str) -> str:
    """The platform's own name ("Telegram"), or the raw type as a fallback."""
    try:
        from app.config import load_channel_catalog

        catalog = load_channel_catalog(channel_type) or {}
        return (catalog.get("channel") or {}).get("display_name") or channel_type
    except Exception:  # noqa: BLE001
        return channel_type or "this platform"


# ── what ───────────────────────────────────────────────────────────────────


def normalize_paths(raw: Any) -> list[str]:
    """The ``files`` argument as a list of non-empty strings.

    Lenient about shape — a single path is as clear as a list of one, and
    models write both; a path wrapped in quotes is still that path — and strict
    about the count. Raises ``ValueError``.
    """
    if raw is None:
        raise ValueError("'files' is required: the absolute paths to send.")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("'files' must be a list of absolute paths.")
    paths = [_unquote(str(p)) for p in raw if p is not None]
    paths = [p for p in paths if p]
    if not paths:
        raise ValueError("'files' has no paths in it.")
    if len(paths) > MAX_FILES_PER_CALL:
        raise ValueError(
            f"{len(paths)} files is more than the {MAX_FILES_PER_CALL} a call "
            "may send. Send the ones that were asked for, in more than one "
            "call if you really need to.",
        )
    return paths


def _unquote(text: str) -> str:
    text = text.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    return text


def _local_path(raw: str) -> Optional[str]:
    """``raw`` as a local path — a ``file://`` URI becomes its path (the web
    UI's file chips hand those around) — or ``None`` for a web address."""
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://", "ftp://")):
        return None
    if lowered.startswith("file://"):
        from urllib.parse import unquote, urlparse
        from urllib.request import url2pathname

        parsed = urlparse(raw)
        path = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = f"\\\\{parsed.netloc}{path}" if os.name == "nt" else f"//{parsed.netloc}{path}"
        return path
    return raw


def _is_absolute(path: str) -> bool:
    expanded = os.path.expanduser(path)
    return os.path.isabs(expanded) or (len(expanded) >= 2 and expanded[1] == ":")


def check_files(
    profile: str,
    paths: Sequence[str],
    *,
    chat: ChatRef,
    working_directory: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """Split ``paths`` into files that can go into ``chat`` and refusals.

    ``validate_outbound_paths`` decides whether a file may leave at all; the
    rest is whether this chat can take it — empty files are refused by every
    platform, and a file over the platform's cap or the server's upload cap
    would fail after the others went. A path given twice is sent once.

    A relative path is taken as the turn's working directory would read it —
    "report.pdf" is what a listing of it shows — and then judged like any
    other; a web address is refused with what to do instead.
    """
    from app.channels.attachments import validate_outbound_paths
    from app.utils.uploads_tmp import max_upload_bytes

    candidates: list[str] = []
    rejected: list[dict] = []
    for raw in paths:
        path = _local_path(raw)
        if path is None:
            rejected.append({
                "path": raw,
                "reason": (
                    "is a web address, not a file — download it into your "
                    "folder first, then send the downloaded file"
                ),
            })
            continue
        if working_directory and not _is_absolute(path):
            path = os.path.join(working_directory, path)
        candidates.append(path)

    ok, refused = validate_outbound_paths(
        profile, candidates, extra_roots=[working_directory] if working_directory else None,
    )
    for entry in refused:
        if entry.get("reason") == "not an existing file" and os.path.isdir(
            os.path.expanduser(entry["path"]),
        ):
            entry = {
                **entry,
                "reason": (
                    "is a folder — put it in a .zip first (the shell can) and "
                    "send the archive"
                ),
            }
        rejected.append(entry)
    platform_cap = type(chat.adapter).max_file_send_bytes
    try:
        server_cap = int(max_upload_bytes())
    except Exception:  # noqa: BLE001
        server_cap = 0

    sendable: list[dict] = []
    seen: set[str] = set()
    for entry in ok:
        key = os.path.normcase(entry["path"])
        if key in seen:
            continue
        seen.add(key)
        size = entry.get("size")
        name = entry["name"]
        if size is None:
            reason = "could not be read"
        elif size <= 0:
            reason = "is empty, and no platform accepts an empty file"
        elif platform_cap and size > platform_cap:
            reason = (
                f"is {_human_size(size)}, over the {_human_size(platform_cap)} "
                f"{chat.platform} accepts from this channel"
            )
        elif server_cap and size > server_cap:
            reason = (
                f"is {_human_size(size)}, over this server's "
                f"{_human_size(server_cap)} limit for files in a chat "
                "(the uploads.tmp_max_bytes setting)"
            )
        else:
            sendable.append(entry)
            continue
        rejected.append({"path": entry["path"], "name": name, "reason": reason})
    return sendable, rejected


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


# ── how it went ────────────────────────────────────────────────────────────

# (turn, chat) -> ({file identity: outcome}, lock). The lock keeps two calls in
# one turn — a model may emit them in parallel — from both deciding a file is
# new. Bounded by ``_TURNS_REMEMBERED``, oldest first.
_turns: "OrderedDict[tuple[str, str], tuple[dict[tuple, str], asyncio.Lock]]" = OrderedDict()


def _remembered(run_id: str, chat: ChatRef) -> tuple[dict[tuple, str], asyncio.Lock]:
    """This turn's delivery record for ``chat``, and the lock that guards it.

    A call with no turn to belong to gets a record of its own: pooling every
    such call under one empty run id would make a file sent by one of them
    "already sent" to all the others, for as long as the entry lived.
    """
    if not run_id:
        return {}, asyncio.Lock()
    key = (run_id, chat.key)
    entry = _turns.get(key)
    if entry is None:
        entry = _turns[key] = ({}, asyncio.Lock())
    else:
        _turns.move_to_end(key)
    excess = len(_turns) - _TURNS_REMEMBERED
    if excess > 0:
        for old in list(_turns)[:excess]:
            # A held lock stays: dropping it would hand the next caller a
            # second lock and let two sends past the duplicate check.
            if old != key and not _turns[old][1].locked():
                del _turns[old]
    return entry


def reset_delivery_records() -> None:
    """Forget every turn's deliveries (tests)."""
    _turns.clear()


def _file_identity(entry: dict) -> tuple:
    """A file as it is now: the same path edited since is a different file."""
    path = entry["path"]
    try:
        stat = os.stat(path)
        return (os.path.normcase(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (os.path.normcase(path), entry.get("size"), None)


def _budget_seconds() -> Optional[float]:
    """How long this call may keep sending, or ``None`` for no limit."""
    try:
        from app.config.settings import BaseConfig

        timeout = BaseConfig.MCP_TOOL_CALL_TIMEOUT
    except Exception:  # noqa: BLE001
        timeout = None
    if not timeout or float(timeout) <= 0:
        return None
    timeout = float(timeout)
    return timeout - min(_DEADLINE_MARGIN_SECONDS, timeout * _DEADLINE_MARGIN_SHARE)


async def deliver(
    chat: ChatRef,
    files: Sequence[dict],
    *,
    run_id: str,
    started: Optional[float] = None,
) -> dict[str, Any]:
    """Send ``files`` into ``chat`` one at a time and report each outcome.

    ``files`` are :func:`check_files`' sendable entries. Never raises for a
    send that goes wrong — that is the report's job.

    ``started`` is when the tool call began (``time.monotonic()``), which is
    what its timeout counts from: the budget has to include the lookups before
    this, and any wait for a parallel call in the same turn to release the
    chat, or the call could still be cut off before it reports.
    """
    started = time.monotonic() if started is None else started
    record, lock = _remembered(run_id, chat)
    budget = _budget_seconds()
    async with lock:
        results: list[dict[str, Any]] = []
        failures_in_a_row = 0
        stop_reason: Optional[str] = None
        attempted = 0
        for entry in files:
            name = entry["name"]
            identity = _file_identity(entry)
            earlier = record.get(identity)
            if earlier in (SENT, UNCONFIRMED):
                results.append({
                    "file": name, "path": entry["path"],
                    "status": ALREADY_SENT if earlier == SENT else UNCONFIRMED,
                    "detail": (
                        "Already sent into this chat earlier in this turn — not "
                        "sent again."
                        if earlier == SENT else
                        "Already tried earlier in this turn without confirmation "
                        "— not sent again, so it cannot arrive twice."
                    ),
                })
                continue
            if stop_reason is None and budget is not None:
                if time.monotonic() - started >= budget:
                    stop_reason = (
                        "Not sent: the call ran out of time. Send it in a new "
                        "call if they still want it."
                    )
            if stop_reason is None and _delivered_count(record) >= MAX_FILES_PER_TURN:
                stop_reason = (
                    f"Not sent: {MAX_FILES_PER_TURN} files have already gone into "
                    "this chat in this turn, the most one answer may carry."
                )
            if stop_reason is not None:
                results.append({
                    "file": name, "path": entry["path"], "status": NOT_SENT,
                    "detail": stop_reason,
                })
                continue

            if attempted:
                await asyncio.sleep(_pacing(chat))
            attempted += 1
            remaining = None if budget is None else max(
                1.0, budget - (time.monotonic() - started),
            )
            outcome, detail, stop = await _send_one(chat, entry, timeout=remaining)
            result: dict[str, Any] = {"file": name, "path": entry["path"], "status": outcome}
            if detail:
                result["detail"] = detail
            results.append(result)
            if outcome in (SENT, UNCONFIRMED):
                # Unconfirmed counts as sent for the duplicate guard: it may be
                # in the chat already, and a second copy is the worse mistake.
                record[identity] = outcome
            if outcome == SENT:
                failures_in_a_row = 0
                continue
            failures_in_a_row += 1
            if stop:
                stop_reason = stop
            elif failures_in_a_row >= _STOP_AFTER_FAILURES:
                stop_reason = (
                    "Not sent: the files before it failed too, so the chat "
                    "looks unreachable right now."
                )

    summary = _summarize(chat, results)
    logger.info(
        f"[chat_files] {chat.kind} {chat.target.key} on {chat.adapter.channel_type}: "
        f"sent={summary['sent']} failed={summary['failed']} "
        f"unconfirmed={summary.get('unconfirmed', 0)} of {len(results)}"
    )
    return summary


def _delivered_count(record: dict[tuple, str]) -> int:
    return sum(1 for outcome in record.values() if outcome in (SENT, UNCONFIRMED))


def _pacing(chat: ChatRef) -> float:
    return _PACING_SECONDS.get(chat.adapter.channel_type, _DEFAULT_PACING_SECONDS)


async def _send_one(
    chat: ChatRef, entry: dict, *, timeout: Optional[float],
) -> tuple[str, str, Optional[str]]:
    """Send one file. Returns ``(outcome, detail, stop)``: ``stop`` is set when
    no later file in the batch could fare any better, and says why for them."""
    adapter = chat.adapter
    path, name = entry["path"], entry["name"]
    mime = entry.get("mime")
    if chat.target.is_group:
        send = adapter.send_file_to_chat_strict(chat.target.address, path, name=name, mime=mime)
    else:
        send = adapter.send_file_strict(chat.target.address, path, name=name, mime=mime)
    try:
        if timeout is None:
            await send
        else:
            await asyncio.wait_for(send, timeout=timeout)
    except asyncio.TimeoutError:
        # Cut off mid-upload by the call's clock: the platform may have the
        # file already. And the clock is spent, so nothing after it starts.
        return (
            UNCONFIRMED,
            "The upload was still running when the call ran out of time, so "
            "it may or may not have arrived. Ask them to check before sending "
            "it again.",
            "Not sent: the call ran out of time. Send it in a new call if they "
            "still want it.",
        )
    except DeliveryUnconfirmed as exc:
        return (
            UNCONFIRMED,
            f"{exc}. Ask them to check before sending it again — a retry could "
            "post it twice.",
            None,
        )
    except ChannelNotImplemented:
        cannot = f"{chat.platform} cannot send files into this chat."
        return FAILED, cannot, f"Not sent: {cannot}"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[chat_files] sending {name!r} into {chat.target.key} on "
            f"{adapter.channel_type} failed: {type(exc).__name__}: {exc}"
        )
        return FAILED, _explain(exc, chat), None
    return SENT, "", None


# Platform errors as the agent should relay them. Matched on the exception's
# type name and text together, lowercased, because each SDK spells the same
# refusal its own way and none of them is imported here.
_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("missing_scope", "files:write"),
     "the Slack app is missing the files:write permission — reinstall it with "
     "that scope"),
    (("not enough rights", "have no rights", "chat_send_media_forbidden",
      "chat_send_docs_forbidden", "chatwriteforbidden", "chat_write_forbidden",
      "missing permissions", "not_allowed_token_type", "restricted"),
     "this account is not allowed to post files in this chat"),
    (("bot was kicked", "bot was blocked", "kicked from", "forbidden",
      "user is deactivated", "not_in_channel", "channel_not_found",
      "chat not found", "peer_id_invalid", "channel_private", "is_archived"),
     "this account can no longer post in this chat — it may have been removed "
     "or blocked"),
    (("too large", "too big", "file_too_big", "file_parts_invalid"),
     "the platform refused the file as too large"),
    (("not connected", "not ready", "sidecar"),
     "the channel's connection to the platform is down right now"),
)


def _explain(exc: BaseException, chat: ChatRef) -> str:
    """The platform's refusal in a sentence, with the SDK's own words after it."""
    raw = f"{type(exc).__name__}: {exc}".strip()
    haystack = raw.lower()
    detail = str(exc).strip() or type(exc).__name__
    if len(detail) > 300:
        detail = detail[:299].rstrip() + "…"
    for needles, hint in _HINTS:
        if any(needle in haystack for needle in needles):
            return f"{chat.platform} refused it: {hint} ({detail})"
    return f"{chat.platform} refused it: {detail}"


def _summarize(chat: ChatRef, results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in (SENT, FAILED, UNCONFIRMED, NOT_SENT, ALREADY_SENT)}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary: dict[str, Any] = {
        "chat": chat.describe(),
        "sent": counts[SENT],
        "failed": counts[FAILED] + counts[NOT_SENT],
        "results": results,
    }
    if counts[UNCONFIRMED]:
        summary["unconfirmed"] = counts[UNCONFIRMED]
    if counts[ALREADY_SENT]:
        summary["already_sent"] = counts[ALREADY_SENT]

    notes: list[str] = []
    if counts[SENT]:
        where = "the group" if chat.kind == KIND_GROUP else "their chat"
        notes.append(
            f"The sent files are in {where} now, ahead of your written answer. "
            "Do not send them again and do not paste their contents — say in a "
            "line what you sent."
        )
        if chat.kind == KIND_GROUP:
            notes.append("Everyone in the group can see them.")
    if counts[ALREADY_SENT]:
        notes.append(
            "Some files were already sent earlier in this turn and were not "
            "sent again."
        )
    if counts[FAILED] or counts[NOT_SENT]:
        notes.append(
            "Tell them which files did not arrive and why. Do not retry a file "
            "that was refused for its size or for permissions — it will be "
            "refused again."
        )
    if counts[UNCONFIRMED]:
        notes.append(
            "An unconfirmed file may or may not have arrived: ask them to check "
            "rather than sending it again."
        )
    summary["note"] = " ".join(notes)
    return summary


__all__ = [
    "ALREADY_SENT",
    "ChatFilesError",
    "ChatRef",
    "FAILED",
    "KIND_GROUP",
    "KIND_PRIVATE",
    "MAX_FILES_PER_CALL",
    "NOT_SENT",
    "SENT",
    "UNCONFIRMED",
    "check_files",
    "deliver",
    "normalize_paths",
    "reset_delivery_records",
    "resolve_chat",
]
