"""Direct sends — message individual channel clients, one or many.

Where ``notification_delivery`` pushes a message *outward* to whoever
subscribed to a channel, this module addresses **specific people**: "send this
thank-you to each customer in the spreadsheet". The recipients are the humans
who already talk to Cremind through a channel (rows in ``channel_senders``)
plus, where the platform allows it, people we have never heard from.

Three things make that harder than it sounds, and they shape everything here:

**Addressing.** A spreadsheet holds phone numbers; channels speak platform ids.
The resolution ladder (:func:`resolve_recipient`) walks from the most certain
match to the least: an exact sender id, then a stored phone, then a WhatsApp
pn-JID derived from the number, and only then a cold send. Matching is exact —
never a suffix or "close enough" comparison — because the failure mode of a
loose match is delivering someone's message to a stranger, which is far worse
than failing to deliver it at all. Anything ambiguous is reported back rather
than guessed.

**Reach.** Only WhatsApp can start a conversation from a phone number, and only
after ``onWhatsApp`` confirms the number exists. Telegram bots, Messenger and
Zalo bots cannot initiate contact at all — the platforms forbid it — so those
recipients come back as errors naming what *would* work, instead of silently
doing nothing.

**Memory.** A message the client sees on their phone but that Cremind never
recorded is a hole in the agent's context: the next turn would not know the
thank-you was ever sent. So every confirmed send is written into that client's
own conversation as an ``agent`` message. "Confirmed" is load-bearing —
:meth:`BaseChannelAdapter.send_strict` raises instead of swallowing, so history
records what was really delivered.

The service is deliberately caller-agnostic: the ``send_channel_message`` tool,
``POST /api/channels/{id}/message`` and ``cremind channels message`` all pass an
explicit adapter list and share this one implementation.
"""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import asdict, dataclass, field
from typing import Any

from app.channels.attachments import file_fallback_text
from app.channels.base import PartialSendError
from app.channels.exceptions import ChannelNotImplemented
from app.utils.logger import logger


def _safe_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# A single call may fan out this far. High enough for a real campaign, low
# enough that a mistaken bulk send is a bounded accident rather than an
# unbounded one — and that platforms don't read the burst as spam.
MAX_RECIPIENTS = 100

# Consecutive transport failures that abort the run. A dead socket or a
# rate-limit ban fails every remaining recipient identically; better to stop
# and report than to march through 90 more doomed sends.
CIRCUIT_BREAKER_FAILURES = 5

# Per-platform pacing between recipients (seconds). WhatsApp is the strictest:
# bursts of unsolicited messages are exactly the pattern that gets numbers
# banned, so it gets a slow, jittered cadence. The rest are comfortable margins
# under the documented API limits (Telegram bots allow ~30 msg/s).
_SEND_DELAYS: dict[str, float] = {
    "whatsapp": 2.0,
    "telegram": 0.1,
    "telegram_userbot": 1.0,
    "zalo_userbot": 1.0,
}
_DEFAULT_SEND_DELAY = 1.0
_JITTER = 0.5

_PN_SUFFIX = "@s.whatsapp.net"

# Platforms that can only ever reply to someone who wrote first. Used to
# explain *why* a recipient is unreachable instead of returning a bare failure.
_CANNOT_INITIATE = {
    "telegram": "a Telegram bot can only message people who have sent it /start first",
    "messenger": "Messenger only allows replies to people who messaged the Page (and within 24h)",
    "zalo": "the Zalo Bot API can only reply to chats that messaged the bot first",
}


@dataclass
class RecipientOutcome:
    """What happened to one recipient. Serialized straight to the caller."""

    to: str
    status: str  # sent | would_send | failed | skipped
    channel_id: str | None = None
    channel_type: str | None = None
    sender_id: str | None = None
    display_name: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    new_contact: bool = False
    error: str | None = None
    detail: str | None = None
    alternatives: list[str] = field(default_factory=list)
    # File-attachment outcome: how many files reached this recipient, and
    # whether their transport can't carry files at all (they got the text
    # fallback notice instead).
    files_sent: int = 0
    files_unsupported: bool = False

    def as_dict(self) -> dict:
        data = asdict(self)
        if not data["alternatives"]:
            data.pop("alternatives")
        if not data["files_sent"]:
            data.pop("files_sent")
        if not data["files_unsupported"]:
            data.pop("files_unsupported")
        return {k: v for k, v in data.items() if v is not None}


class RecipientError(Exception):
    """A recipient could not be resolved. Carries the structured explanation."""

    def __init__(
        self, code: str, detail: str, alternatives: list[str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.alternatives = alternatives or []


# ── phone normalization ────────────────────────────────────────────────────


def normalize_phone(raw: str, default_country_code: str | None = None) -> str | None:
    """Return ``raw`` as canonical digits (E.164 without ``+``), or ``None``.

    Deliberately strict. ``+84 90 123 4567`` and ``84901234567`` are the same
    number and normalize identically, but a national-format ``0901234567`` is
    genuinely ambiguous without knowing the country — the same digits are a
    valid subscriber number in dozens of them. Rather than guess a country and
    risk messaging a stranger, that form resolves only when the caller supplies
    ``default_country_code``; otherwise it is rejected so the caller can ask for
    the international form.
    """
    if not raw:
        return None
    text = str(raw).strip()
    has_plus = text.startswith("+")
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None

    if has_plus:
        return digits if len(digits) >= 7 else None
    if digits.startswith("00"):
        digits = digits[2:]
        return digits if len(digits) >= 7 else None
    if digits.startswith("0"):
        cc = "".join(ch for ch in str(default_country_code or "") if ch.isdigit())
        if not cc:
            return None
        digits = cc + digits.lstrip("0")
        return digits if len(digits) >= 7 else None
    return digits if len(digits) >= 7 else None


def looks_like_phone(raw: str) -> bool:
    """True when ``raw`` is plausibly a phone number rather than a platform id.

    Shape only: digits with the punctuation people type into spreadsheets, and
    long enough to be a real number. A bare digit string is *also* a valid
    Telegram/Discord id, which is why the resolution ladder always tries an
    exact sender-id match before trusting this.
    """
    text = str(raw or "").strip()
    if not text:
        return False
    if "@" in text:
        return False
    body = text.lstrip("+")
    if not body:
        return False
    stripped = "".join(ch for ch in body if ch not in " -().")
    return stripped.isdigit() and len(stripped) >= 7


def phone_to_wa_jid(digits: str) -> str:
    """The WhatsApp pn-JID for a canonical phone number."""
    return f"{digits}{_PN_SUFFIX}"


# ── resolution ─────────────────────────────────────────────────────────────


def channel_matches(adapter: Any, wanted: str) -> bool:
    """Does ``adapter`` answer to the caller-supplied channel selector?

    Accepts a channel type (``"whatsapp"`` — how the model and the operator
    naturally refer to channels, and unambiguous because a profile holds at
    most one channel per platform) or a raw channel id.
    """
    want = str(wanted or "").strip().lower()
    if not want:
        return True
    return want in (adapter.channel_type.lower(), adapter.channel_id.lower())


def _describe(adapter: Any, sender: dict | None) -> str:
    who = (sender or {}).get("display_name") or (sender or {}).get("sender_id") or "?"
    return f"{adapter.channel_type}:{who}"


def _reach_alternatives(adapters: list[Any]) -> list[str]:
    """Channel types on this profile that *could* reach a phone number."""
    out: list[str] = []
    for a in adapters:
        if a.channel_type == "whatsapp":
            out.append("whatsapp (can message any number on WhatsApp)")
        elif a.channel_type == "telegram_userbot":
            out.append("telegram_userbot (only numbers already in the account's contacts)")
    return out


async def resolve_recipient(
    to: str,
    adapters: list[Any],
    senders_by_channel: dict[str, list[dict]],
    *,
    channel: str | None = None,
    default_country_code: str | None = None,
) -> tuple[Any, str, dict | None, dict]:
    """Resolve ``to`` to ``(adapter, platform_sender_id, sender_row, info)``.

    ``sender_row`` is ``None`` for someone we have never messaged; ``info``
    carries what the send path needs afterwards (``phone``, ``wa_lid``,
    ``cold``). Raises :class:`RecipientError` when the recipient cannot be
    addressed, including when it resolves two different ways — an ambiguous
    recipient is reported, never guessed.
    """
    target = str(to or "").strip()
    if not target:
        raise RecipientError("invalid_recipient", "Empty recipient.")

    candidates = [a for a in adapters if channel_matches(a, channel or "")]
    if not candidates:
        available = sorted({a.channel_type for a in adapters})
        raise RecipientError(
            "unknown_channel",
            f"No connected channel matches {channel!r}. "
            f"Available: {', '.join(available) or '(none)'}.",
        )

    # 1. Exact platform sender id. First because it is the only certain match,
    #    and because a bare number is a valid Telegram id as well as a phone.
    id_hits = [
        (a, s)
        for a in candidates
        for s in senders_by_channel.get(a.channel_id, [])
        if s["sender_id"] == target
    ]
    if len(id_hits) == 1:
        adapter, sender = id_hits[0]
        return adapter, sender["sender_id"], sender, {"cold": False}
    if len(id_hits) > 1:
        raise RecipientError(
            "ambiguous_recipient",
            f"{target} matches contacts on several channels "
            f"({', '.join(_describe(a, s) for a, s in id_hits)}). "
            "Re-send with 'channel' set to the one you mean.",
        )

    phone = normalize_phone(target, default_country_code) if looks_like_phone(target) else None

    if looks_like_phone(target) and not phone:
        raise RecipientError(
            "ambiguous_phone",
            f"{target} looks like a national-format number, which is ambiguous "
            "without a country. Supply it in international form (e.g. "
            "+84901234567) or pass 'default_country_code'.",
        )

    if phone:
        # 2. A number we have already associated with a contact.
        phone_hits = [
            (a, s)
            for a in candidates
            for s in senders_by_channel.get(a.channel_id, [])
            if s.get("phone") and s["phone"] == phone
        ]
        if len(phone_hits) > 1:
            raise RecipientError(
                "ambiguous_recipient",
                f"{target} matches contacts on several channels "
                f"({', '.join(_describe(a, s) for a, s in phone_hits)}). "
                "Re-send with 'channel' set to the one you mean.",
            )
        if len(phone_hits) == 1:
            adapter, sender = phone_hits[0]
            return adapter, sender["sender_id"], sender, {"cold": False, "phone": phone}

        # 3. WhatsApp encodes the number in the sender id, so a known contact
        #    may match even without a stored phone.
        wa = next((a for a in candidates if a.channel_type == "whatsapp"), None)
        if wa is not None:
            jid = phone_to_wa_jid(phone)
            existing = next(
                (s for s in senders_by_channel.get(wa.channel_id, []) if s["sender_id"] == jid),
                None,
            )
            if existing is not None:
                return wa, jid, existing, {"cold": False, "phone": phone}
            # 4. Cold send — verify the number is actually on WhatsApp first,
            #    so we don't register a contact for a message that can't land.
            check = await wa.resolve_phone(phone)
            if not check.get("exists"):
                raise RecipientError(
                    "not_on_whatsapp",
                    f"{target} does not have a WhatsApp account, so it cannot be "
                    "messaged there.",
                )
            return wa, check.get("jid") or jid, None, {
                "cold": True, "phone": phone, "wa_lid": check.get("lid"),
            }

        blocked = sorted({
            _CANNOT_INITIATE[a.channel_type]
            for a in candidates if a.channel_type in _CANNOT_INITIATE
        })
        raise RecipientError(
            "platform_cannot_initiate",
            f"No contact on the selected channel(s) has the number {target}, and "
            + (f"{'; '.join(blocked)}. " if blocked else "none of them can address people by phone. ")
            + "Ask them to message the channel first.",
            alternatives=_reach_alternatives(adapters),
        )

    # 5. A non-phone identifier we have never seen: send it as a raw platform
    #    id when exactly one channel is in play (covers Slack ids and
    #    Telegram-userbot contacts). With several channels it is undecidable.
    if len(candidates) == 1:
        adapter = candidates[0]
        if adapter.channel_type in _CANNOT_INITIATE:
            raise RecipientError(
                "platform_cannot_initiate",
                f"{target} has never messaged this channel and "
                f"{_CANNOT_INITIATE[adapter.channel_type]}.",
                alternatives=_reach_alternatives(adapters),
            )
        return adapter, target, None, {"cold": True}

    raise RecipientError(
        "ambiguous_identifier",
        f"{target} is not a known contact and is not a phone number, so it is "
        f"unclear which channel it belongs to "
        f"({', '.join(sorted({a.channel_type for a in candidates}))}). "
        "Re-send with 'channel' set.",
    )


# ── the send pipeline ──────────────────────────────────────────────────────


def normalize_recipients(raw: Any) -> list[dict]:
    """Coerce caller input into ``[{"to", "message"?, "name"?, "channel"?}]``.

    Lenient on purpose: a model reading a spreadsheet may produce a list of
    bare strings just as naturally as a list of objects, and both mean the same
    thing. Raises ``ValueError`` on shapes that don't.
    """
    if raw is None:
        raise ValueError("'recipients' is required.")
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("'recipients' must be a list.")

    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            entry = {"to": item.strip()}
        elif isinstance(item, dict):
            entry = {
                "to": str(item.get("to") or item.get("sender_id") or item.get("phone") or "").strip(),
                "message": (str(item["message"]).strip() if item.get("message") else None),
                "name": (str(item["name"]).strip() if item.get("name") else None),
                "channel": (str(item["channel"]).strip() if item.get("channel") else None),
            }
        else:
            raise ValueError(
                "Each recipient must be a string or an object with a 'to' field.",
            )
        if not entry["to"]:
            raise ValueError("Every recipient needs a non-empty 'to'.")
        out.append(entry)
    if not out:
        raise ValueError("'recipients' cannot be empty.")
    return out


def _delay_for(adapter: Any) -> float:
    """Seconds to wait before the next recipient on this platform.

    Jittered so a campaign doesn't arrive on a machine-perfect cadence, which
    is itself a spam signal. Fast platforms skip the jitter — it would swamp
    the interval.
    """
    base = _SEND_DELAYS.get(adapter.channel_type, _DEFAULT_SEND_DELAY)
    if base <= _JITTER:
        return base
    return max(0.0, base + random.uniform(-_JITTER, _JITTER))


async def send_direct_messages(
    *,
    adapters: list[Any],
    storage: Any,
    recipients: list[dict],
    message: str | None = None,
    channel: str | None = None,
    default_country_code: str | None = None,
    dry_run: bool = True,
    initiated_by: str = "tool",
    confirm: bool = False,
    confirm_policy: Any = None,
    attachments: list[dict] | None = None,
) -> dict:
    """Resolve, send, register and record.

    With ``dry_run`` nothing is sent and nothing is written: each recipient is
    resolved and reported, so the caller can show who would be messaged —
    including which entries are cold contacts and which don't resolve — before
    committing. That preview is the main guard against a bulk send going to the
    wrong list.

    ``confirm_policy`` opts into the *configurable* version of that guard, used
    by the agent-facing tool. Given ``(sender_row, cold) -> bool``, it is asked
    about every resolved recipient once resolution is complete; if it says any of
    them still needs the operator's approval, the whole call degrades to a
    preview and **nothing** is sent. All-or-nothing on purpose: a call that
    delivered half a list before asking about the rest would be far harder to
    reason about than one that delivered none. ``confirm=True`` means the
    operator has already approved this exact list, so the policy is not
    consulted. Callers that leave ``confirm_policy`` as ``None`` — the REST
    endpoint and the CLI, where the operator is right there and `--send` is
    itself the approval — behave exactly as before.

    A live run sends sequentially with per-platform pacing. One recipient's
    failure never aborts the others (a bad row in a spreadsheet shouldn't cost
    the other 99 their message), but a run of consecutive transport failures
    does, since that means the channel itself is broken.

    ``attachments`` are already-validated ``{"path", "name"?, "mime"?}``
    entries (see :func:`app.channels.attachments.validate_outbound_paths` —
    the caller owns that validation) sent to EVERY recipient after their text.
    A send with attachments needs no text.
    """
    if len(recipients) > MAX_RECIPIENTS:
        raise ValueError(
            f"{len(recipients)} recipients exceeds the {MAX_RECIPIENTS}-per-call "
            "limit. Split the list into smaller batches.",
        )

    shared = (message or "").strip()
    if not attachments:
        missing_text = [r["to"] for r in recipients if not (r.get("message") or shared)]
        if missing_text:
            raise ValueError(
                "No message text for: " + ", ".join(missing_text[:5])
                + ". Provide 'message' for the whole send, or a 'message' on each recipient.",
            )

    senders_by_channel: dict[str, list[dict]] = {}
    for adapter in adapters:
        try:
            senders_by_channel[adapter.channel_id] = await storage.list_senders(
                adapter.channel_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"[direct_send] could not list senders for {adapter.channel_id}",
            )
            senders_by_channel[adapter.channel_id] = []

    # ── pass 1: resolve everything before anything is sent ──
    #
    # Resolution has no side effects, and doing it up front is what lets the
    # confirmation policy below be all-or-nothing: the decision needs to see
    # every recipient, including the per-client overrides that only exist once a
    # recipient has been matched to a client row.
    plan: list[dict[str, Any]] = []
    for entry in recipients:
        target = entry["to"]
        text = (entry.get("message") or shared).strip()
        try:
            adapter, sender_id, sender, info = await resolve_recipient(
                target, adapters, senders_by_channel,
                channel=entry.get("channel") or channel,
                default_country_code=default_country_code,
            )
        except RecipientError as exc:
            plan.append({"outcome": RecipientOutcome(
                to=target, status="failed", error=exc.code, detail=exc.detail,
                alternatives=exc.alternatives,
            )})
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[direct_send] resolution failed for {target}")
            plan.append({"outcome": RecipientOutcome(
                to=target, status="failed", error="resolution_failed", detail=str(exc),
            )})
            continue

        plan.append({
            "outcome": RecipientOutcome(
                to=target,
                status="would_send",
                channel_id=adapter.channel_id,
                channel_type=adapter.channel_type,
                sender_id=sender_id,
                display_name=entry.get("name") or (sender or {}).get("display_name"),
                conversation_id=(sender or {}).get("conversation_id"),
                new_contact=bool(info.get("cold")),
                # Flagged at resolution (off the CLASS, like the dry-run
                # preview needs) so a preview already tells the caller which
                # recipients would get the fallback notice instead of a file.
                files_unsupported=bool(attachments)
                and not type(adapter).supports_file_send,
            ),
            "entry": entry,
            "text": text,
            "adapter": adapter,
            "sender_id": sender_id,
            "sender": sender,
            "info": info,
        })

    sendable = [slot for slot in plan if "adapter" in slot]

    # ── the configurable confirmation gate ──
    pending: list[dict[str, Any]] = []
    if confirm_policy is not None and not confirm:
        for slot in sendable:
            try:
                needs = bool(confirm_policy(slot["sender"], bool(slot["info"].get("cold"))))
            except Exception:  # noqa: BLE001
                logger.exception("[direct_send] confirmation policy failed")
                needs = True  # a policy we cannot evaluate means "ask"
            if needs:
                slot["needs_confirmation"] = True
                pending.append(slot)

    if dry_run or pending:
        return _preview_summary(
            plan, pending, dry_run=dry_run, attachments=attachments,
        )

    results: list[RecipientOutcome] = []
    consecutive_failures = 0
    aborted = False

    # ── pass 2: deliver ──
    for index, slot in enumerate(plan):
        base = slot["outcome"]
        if "adapter" not in slot:
            results.append(base)          # resolution already failed
            continue
        if aborted:
            base.status = "skipped"
            base.error = "aborted"
            base.detail = "Skipped after repeated delivery failures on this channel."
            results.append(base)
            continue

        adapter = slot["adapter"]
        sender_id = slot["sender_id"]
        text = slot["text"]
        entry = slot["entry"]
        info = slot["info"]

        try:
            outcome = await _send_one(
                adapter=adapter, storage=storage, sender_id=sender_id,
                text=text, info=info, display_name=entry.get("name"),
                initiated_by=initiated_by, base=base,
                senders_by_channel=senders_by_channel,
                attachments=attachments,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[direct_send] send failed for {base.to}")
            base.status = "failed"
            base.error = "send_failed"
            base.detail = str(exc)
            outcome = base

        results.append(outcome)
        if outcome.status == "sent":
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= CIRCUIT_BREAKER_FAILURES:
                logger.warning(
                    f"[direct_send] aborting after {consecutive_failures} "
                    f"consecutive failures on {adapter.channel_type}",
                )
                aborted = True

        if index < len(plan) - 1 and not aborted:
            await asyncio.sleep(_delay_for(adapter))

    sent = sum(1 for r in results if r.status == "sent")
    failed = sum(1 for r in results if r.status in ("failed", "skipped"))
    summary: dict[str, Any] = {
        "dry_run": False,
        "sent": sent,
        "failed": failed,
        "results": [r.as_dict() for r in results],
    }
    if aborted:
        summary["aborted"] = True
    return summary


def _preview_summary(
    plan: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    *,
    dry_run: bool,
    attachments: list[dict] | None = None,
) -> dict:
    """Summarize a call that resolved recipients but sent nothing.

    Two reasons land here and the message has to distinguish them, because the
    operator's next move differs: an explicitly requested preview is finished
    business, while a confirmation hold is waiting on an answer.
    """
    from app.channels import send_policy

    results = [slot["outcome"] for slot in plan]
    resolved = sum(1 for r in results if r.status == "would_send")
    cold = sum(1 for r in results if r.status == "would_send" and r.new_contact)

    summary: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "sent": 0,
        "failed": sum(1 for r in results if r.status == "failed"),
        "results": [r.as_dict() for r in results],
        "resolved": resolved,
        "new_contacts": cold,
    }
    if attachments:
        summary["attachments"] = len(attachments)
        unsupported = sum(
            1 for r in results if r.status == "would_send" and r.files_unsupported
        )
        if unsupported:
            summary["files_unsupported_recipients"] = unsupported

    if pending:
        summary["needs_confirmation"] = [
            {
                "to": slot["outcome"].to,
                "sender_id": slot["sender_id"],
                "display_name": slot["outcome"].display_name,
                "reason": send_policy.describe(
                    slot["sender"], cold=bool(slot["info"].get("cold")),
                ),
            }
            for slot in pending
        ]
        summary["message"] = (
            f"Nothing was sent — {len(pending)} of {resolved} resolved recipient(s) "
            "need the user's approval first (see 'needs_confirmation'). Show the "
            "user who would be messaged, and once they approve call again with "
            "confirm=true to deliver to everyone in this list."
        )
        return summary

    summary["message"] = (
        f"Preview only — nothing was sent. {resolved} of {len(results)} "
        f"recipient(s) resolved"
        + (f", {cold} of them never messaged this channel before" if cold else "")
        + ". Call again without dry_run to deliver."
    )
    return summary


async def _send_one(
    *, adapter: Any, storage: Any, sender_id: str, text: str, info: dict,
    display_name: str | None, initiated_by: str, base: RecipientOutcome,
    senders_by_channel: dict[str, list[dict]],
    attachments: list[dict] | None = None,
) -> RecipientOutcome:
    """Send to one resolved recipient, then record it in their conversation.

    Registration happens before the send so the contact and their conversation
    exist to write into; the history entry happens strictly after, because a
    message we failed to deliver must not appear in the transcript as though
    the client had seen it.

    The sender row is re-read here rather than taken from the caller's
    pre-loop snapshot. A snapshot row can be stale in the one way that matters:
    if its ``conversation_id`` is NULL (the normal state for notification-mode
    subscribers, who never go through the inbound conversation path) then a
    recipient listed twice in the same call would create a *second*
    conversation and strand the first message in one no sender points at.
    """
    lock = adapter._inbound_lock(sender_id)  # noqa: SLF001
    async with lock:
        # Creates the row for a contact we are reaching first, and backfills
        # phone/lid for one we already knew. ``authenticated`` stays False
        # either way: sending to someone is not a decision to let them command
        # the agent, so on gated channels their reply still has to meet the
        # operator's access rules.
        row = await storage.get_or_create_sender(
            adapter.channel_id, sender_id,
            display_name=display_name,
            phone=info.get("phone"),
            wa_lid=info.get("wa_lid"),
        )

        conversation_id = await storage.ensure_sender_conversation(
            row, profile=adapter.profile, channel_id=adapter.channel_id,
            display_name=display_name,
        )
        # Keep the caller's snapshot consistent with what we just wrote, so a
        # later recipient resolving to this same person sees the real row
        # (and a cold contact registered mid-run resolves without a second
        # existence check).
        row["conversation_id"] = conversation_id
        _remember(senders_by_channel, adapter.channel_id, row)
        base.conversation_id = conversation_id
        base.display_name = display_name or row.get("display_name")

        try:
            await adapter.send_strict(sender_id, text)
        except PartialSendError as exc:
            # The client saw the opening chunks, so the transcript has to show
            # them — otherwise the agent's picture of the conversation is wrong
            # in the one direction that matters (it would re-send).
            base.status = "failed"
            base.error = "partial_delivery"
            base.detail = str(exc)
            msg = await storage.add_message(
                conversation_id=conversation_id, role="agent", content=text,
                metadata=_metadata(
                    adapter, sender_id, base.display_name, initiated_by,
                    cold=base.new_contact, partial=True,
                ),
            )
            base.message_id = msg["id"]
            return base
        except Exception as exc:  # noqa: BLE001
            base.status = "failed"
            base.error = "delivery_failed"
            base.detail = str(exc)
            return base

        # ── attachments, after the text landed ──
        #
        # A transport with no file support downgrades each file to the
        # fallback notice (name only, never the server path); any other
        # failure records what DID reach the client and reports the rest,
        # mirroring the partial-delivery contract above.
        delivered_files: list[dict] = []
        file_error: str | None = None
        for att in attachments or []:
            path = att.get("path") or ""
            att_name = att.get("name") or os.path.basename(path)
            try:
                await adapter.send_file_strict(
                    sender_id, path, name=att_name, mime=att.get("mime"),
                )
                delivered_files.append({"name": att_name, "path": path})
            except ChannelNotImplemented:
                base.files_unsupported = True
                try:
                    await adapter.send_strict(
                        sender_id, file_fallback_text(att_name, _safe_size(path)),
                    )
                except Exception as exc:  # noqa: BLE001
                    file_error = str(exc)
                    break
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"[direct_send] file send failed for {base.to} ({att_name})",
                )
                file_error = str(exc)
                break
        base.files_sent = len(delivered_files)

        from app.agent.stream_runner import attachment_file_parts

        msg = await storage.add_message(
            conversation_id=conversation_id, role="agent", content=text,
            parts=attachment_file_parts(delivered_files) or None,
            metadata=_metadata(
                adapter, sender_id, base.display_name, initiated_by,
                cold=base.new_contact, partial=file_error is not None,
            ),
        )
        base.message_id = msg["id"]
        if file_error is not None:
            base.status = "failed"
            base.error = "file_delivery_failed"
            base.detail = file_error
        else:
            base.status = "sent"
        return base


def _remember(
    senders_by_channel: dict[str, list[dict]], channel_id: str, row: dict,
) -> None:
    """Upsert ``row`` into the per-run sender snapshot."""
    rows = senders_by_channel.setdefault(channel_id, [])
    for i, existing in enumerate(rows):
        if existing.get("sender_id") == row.get("sender_id"):
            rows[i] = row
            return
    rows.append(row)


def _metadata(
    adapter: Any, sender_id: str, display_name: str | None, initiated_by: str,
    *, cold: bool = False, partial: bool = False,
) -> dict:
    """Provenance stamp mirroring the inbound one in ``BaseChannelAdapter``.

    ``source: agent_outbound`` is what distinguishes "the agent messaged this
    person on its own" from a reply inside their turn, both in the UI and to
    anything later reading the transcript.
    """
    data: dict[str, Any] = {
        "source": "agent_outbound",
        "channel_id": adapter.channel_id,
        "channel_type": adapter.channel_type,
        "sender_id": sender_id,
        "display_name": display_name,
        "initiated_by": initiated_by,
    }
    if cold:
        data["cold_contact"] = True
    if partial:
        data["partial_failure"] = True
    return data
