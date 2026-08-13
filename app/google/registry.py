"""What a Google Suite link *is on disk* — one declaration, five entries.

Every surface that talks about a Google link (the REST inventory, the
``cremind google`` CLI, the Settings page, the confirm dialogs) reads this
module. In particular ``consequence`` is phrased **here** rather than separately
by each client, for the same reason :func:`app.drive.skill_token.access_model`
is returned from the backend: three clients phrasing the same fact three ways is
how they end up describing the same account differently.

The skills themselves are standalone ``uv`` projects whose ``scripts/app/config.py``
declares these paths independently, so this is a deliberate second copy.
``tests/skills/test_google_unlink_file_parity.py`` pins the two together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

_SCRIPTS = Path("scripts")

#: The credential itself. Identical across all five skills.
TOKEN_REL = _SCRIPTS / ".google_token.json"

#: ``TokenStore.save`` writes this then ``os.replace``s it into place, so a crash
#: between the two leaves a *full* credential set behind — which is why every
#: skill's ``scripts/.gitignore`` lists it. Wiping the link has to take it too.
TOKEN_TMP_REL = _SCRIPTS / ".google_token.json.tmp"

#: Never delete these. ``.env`` is user config (and is re-materialized from
#: ``tool_configs`` on every boot by ``app.skills.sync``), and ``.listener.lock``
#: is a live OS lock whose removal breaks the single-instance guard.
PRESERVED_REL = (_SCRIPTS / ".env", _SCRIPTS / ".listener.lock")

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

#: Where a user removes Cremind's access by hand when we cannot do it for them.
GOOGLE_CONNECTIONS_URL = "https://myaccount.google.com/connections"

#: Revoking a refresh token ends the whole grant for the (client, account) pair.
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class GoogleSkill:
    """One Google Suite skill, from the perspective of its Google link."""

    dir_name: str
    """Directory under ``<profile>/skills/`` — also the API path segment. The
    five names form an allow-list, which is what makes path traversal
    impossible by construction."""

    label: str
    """Human name, used in every message and confirm dialog."""

    consequence: str
    """The one sentence describing what the user actually loses. Served to the
    CLI and UI so they cannot phrase it differently."""

    watch_base: Optional[str] = None
    """API base for ``channels.stop``, or None when the skill registers no
    Google push channel."""

    has_listener: bool = False
    """Whether the skill declares a ``long_running_app`` listener, i.e. whether
    unlinking will stop a process and drop an autostart registration."""

    event_types: Tuple[str, ...] = ()
    """Event folder names under ``events/``. Their ``*.md`` payloads hold content
    from the linked account, so they go with the link."""

    extra_rel: Tuple[Path, ...] = field(default_factory=tuple)
    """Skill-specific files to delete *beyond* the token and its temp file."""

    @property
    def token_rel(self) -> Path:
        return TOKEN_REL

    @property
    def delete_rel(self) -> Tuple[Path, ...]:
        """Every file a wipe removes, token first.

        Token first on purpose: it is what defines "linked", so a later failure
        on an extra file is cosmetic while a failure on this one is not.
        """
        return (TOKEN_REL, TOKEN_TMP_REL, *self.extra_rel)

    @property
    def event_dirs_rel(self) -> Tuple[Path, ...]:
        return tuple(Path("events") / name for name in self.event_types)


_LISTENER_STATE_REL = (
    _SCRIPTS / ".listener_state.json",
    _SCRIPTS / ".listener_state.json.tmp",
)

GOOGLE_SKILLS: Tuple[GoogleSkill, ...] = (
    GoogleSkill(
        dir_name="gcalendar",
        label="Google Calendar",
        consequence=(
            "Cremind stops reading and writing this Google Calendar. The Calendar & "
            "Schedule page falls back to the Google account connected on that page, or "
            "to the built-in system calendar if there is none — your scheduled events "
            "keep firing either way, and events already mirrored into Google stay in "
            "Google."
        ),
        watch_base=CALENDAR_API_BASE,
        has_listener=True,
        event_types=("event_changed",),
        extra_rel=_LISTENER_STATE_REL,
    ),
    GoogleSkill(
        dir_name="gdrive",
        label="Google Drive",
        consequence=(
            "Cremind loses access to every Drive file you granted it. Re-linking does "
            "not bring the grants back — you have to pick the files again."
        ),
        watch_base=DRIVE_API_BASE,
        has_listener=True,
        event_types=("file_changed",),
        extra_rel=(*_LISTENER_STATE_REL, _SCRIPTS / ".drive_grants.json"),
    ),
    GoogleSkill(
        dir_name="gmail",
        label="Gmail",
        consequence="Cremind can no longer send mail as this account.",
    ),
    GoogleSkill(
        dir_name="gsheets",
        label="Google Sheets",
        consequence=(
            "Cremind can no longer read or write any spreadsheet, including ones you "
            "pasted a URL for."
        ),
    ),
    GoogleSkill(
        dir_name="gdocs",
        label="Google Docs",
        consequence=(
            "Cremind can no longer read or write any document, including ones you "
            "pasted a URL for."
        ),
    ),
)


def names() -> Tuple[str, ...]:
    return tuple(spec.dir_name for spec in GOOGLE_SKILLS)


def by_name(name: str) -> Optional[GoogleSkill]:
    """The spec for ``name``, or None when it is not a Google Suite skill."""
    wanted = (name or "").strip().lower()
    for spec in GOOGLE_SKILLS:
        if spec.dir_name == wanted:
            return spec
    return None
