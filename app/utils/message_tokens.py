"""Resolve `$VAR` and `@profile` tokens in a user message into plain text.

`$NAME` is replaced with the resolved value of the matching entry in
:data:`app.config.system_vars.SYSTEM_VARS` (resolved against the active
profile). `@name` is replaced with the bare profile name when that profile
exists. Tokens that don't match a known variable / profile pass through
unchanged, so unrelated occurrences like ``$5`` or an email address survive.

Substitution happens once on the raw input — no recursion, so a resolved
value that itself contains ``$FOO`` is left as-is.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.config.system_vars import SYSTEM_VARS, SystemVarSpec
from app.storage.conversation_storage import ConversationStorage

_SYS_VAR_RE = re.compile(r"\$([A-Z][A-Z0-9_]*)")
_PROFILE_RE = re.compile(r"@([a-z0-9_-]+)")


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
