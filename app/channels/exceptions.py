"""Typed errors for the channels subsystem."""


class ChannelNotImplemented(Exception):
    """Raised by an adapter when its platform integration isn't built yet.

    Surfaces as ``HTTP 501`` from the channels API; the registry catches it,
    flips ``enabled`` to false on the channel row, and stores the message in
    ``state.last_error`` so the UI can surface it.
    """


class ChannelAuthError(Exception):
    """Raised when a channel can't authenticate with its platform.

    Surfaces as ``HTTP 400`` so the user can correct the credentials.
    """
