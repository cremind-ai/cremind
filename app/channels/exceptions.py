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


class DeliveryUnconfirmed(ChannelAuthError):
    """A send whose outcome is unknown: it may or may not have arrived.

    Raised when an upload was handed over but never acknowledged — a sidecar
    that did not answer in time, a platform that timed out mid-upload. Unlike a
    refused send, retrying it can deliver the same file twice, so a caller
    should report it as unconfirmed and let a person check the chat rather
    than send again. A :class:`ChannelAuthError` for the callers that already
    caught the ack timeouts as one.
    """
