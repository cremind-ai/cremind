"""Resolve `$VAR` and `@profile` tokens in a user message into plain text.

`$NAME` is replaced with the resolved value of the matching entry in
:data:`app.config.system_vars.SYSTEM_VARS` (resolved against the active
profile). `@name` is replaced with the bare profile name when that profile
exists. Tokens that don't match a known variable / profile pass through
unchanged, so unrelated occurrences like ``$5`` or an email address survive.

Substitution happens once on the raw input — no recursion, so a resolved
value that itself contains ``$FOO`` is left as-is.

:func:`resolve_system_var_tokens` is a synchronous, ``$VAR``-only variant used
when rendering ``.md`` documents. It reuses the same system-variable syntax but
skips ``@profile`` resolution (so it needs no storage) and adds a ``$$NAME``
escape that renders as a literal ``$NAME``.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from app.config.system_vars import SYSTEM_VARS, SystemVarSpec
from app.storage.conversation_storage import ConversationStorage

_SYS_VAR_RE = re.compile(r"\$([A-Z][A-Z0-9_]*)")
_PROFILE_RE = re.compile(r"@([a-z0-9_-]+)")

# Documents reuse the system-variable syntax but add a ``$$NAME`` escape that
# renders as a literal ``$NAME``. The escaped alternative is listed first so
# ``$$FOO`` matches it whole and never falls through to the bare-``$FOO`` branch.
_DOC_VAR_RE = re.compile(r"\$\$([A-Z][A-Z0-9_]*)|\$([A-Z][A-Z0-9_]*)")


def _system_vars_index(specs: Iterable[SystemVarSpec]) -> dict[str, SystemVarSpec]:
    return {spec.name: spec for spec in specs}


async def resolve_message_tokens(
    text: str,
    *,
    profile: str,
    conversation_storage: ConversationStorage,
) -> str:
    if not text or ("$" not in text and "@" not in text):
        return text

    sys_index = _system_vars_index(SYSTEM_VARS)

    def replace_sys_var(match: re.Match[str]) -> str:
        name = match.group(1)
        spec = sys_index.get(name)
        if spec is None:
            return match.group(0)
        value = spec.resolve(profile)
        if value is None:
            return match.group(0)
        return str(value)

    text = _SYS_VAR_RE.sub(replace_sys_var, text)

    profile_matches = list(_PROFILE_RE.finditer(text))
    if not profile_matches:
        return text

    candidate_names = {m.group(1) for m in profile_matches}
    resolved: dict[str, bool] = {}
    for name in candidate_names:
        try:
            resolved[name] = await conversation_storage.profile_exists(name)
        except Exception:
            resolved[name] = False

    def replace_profile(match: re.Match[str]) -> str:
        name = match.group(1)
        return name if resolved.get(name) else match.group(0)

    return _PROFILE_RE.sub(replace_profile, text)


def resolve_system_var_tokens(text: str, profile: Optional[str]) -> str:
    """Substitute ``$VAR`` system-variable tokens in ``text`` for ``profile``.

    Synchronous, ``$VAR``-only counterpart to :func:`resolve_message_tokens`,
    intended for rendering ``.md`` document bodies at read time. It shares the
    system-variable index so the syntax matches chat exactly, but does not
    resolve ``@profile`` tokens (hence no storage dependency).

    Resolution rules, applied in a single non-recursive pass:

    - ``$$NAME`` → literal ``$NAME`` (escape; never resolved).
    - ``$NAME`` matching a registered variable → its resolved value (including
      secrets such as ``CREMIND_TOKEN`` — same as chat).
    - ``$NAME`` not in the registry (``$PATH``, ``$HOME``) or whose resolver
      returns ``None`` (e.g. unset for the current profile) → left unchanged.
    """
    if not text or "$" not in text:
        return text

    sys_index = _system_vars_index(SYSTEM_VARS)

    def replace(match: re.Match[str]) -> str:
        escaped = match.group(1)
        if escaped is not None:
            return "$" + escaped
        name = match.group(2)
        spec = sys_index.get(name)
        if spec is None:
            return match.group(0)
        value = spec.resolve(profile)
        if value is None:
            return match.group(0)
        return str(value)

    return _DOC_VAR_RE.sub(replace, text)
