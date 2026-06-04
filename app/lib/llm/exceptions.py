"""LLM-layer exceptions.

These are raised by the LLM resolution path (model groups, provider factory)
and caught by the agent stream runner so the user gets an actionable error
instead of a generic "see server logs". Subclasses ValueError so existing
``except ValueError`` handlers still catch it.
"""

from __future__ import annotations


class SetupRequiredError(ValueError):
    """Raised when a piece of setup data is missing and the user must fill it in.

    Carries enough context for the front-end / CLI to point the user at the
    right settings page rather than just printing the message verbatim:

      - ``code``           : short machine-readable identifier
      - ``settings_path``  : front-end route the user should open to fix it
      - ``settings_label`` : human label for the settings link
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        settings_path: str = "",
        settings_label: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.settings_path = settings_path
        self.settings_label = settings_label

    def to_event_payload(self) -> dict:
        """Serializable shape published to the conversation bus."""
        payload = {
            "message": str(self),
            "code": self.code,
            "setup_required": True,
        }
        if self.settings_path:
            payload["settings_path"] = self.settings_path
        if self.settings_label:
            payload["settings_label"] = self.settings_label
        return payload
