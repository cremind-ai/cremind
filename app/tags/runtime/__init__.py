"""Cremind Tag hardware runtime: the gateway worker, its durable delivery
queue, the protocol v1/v2 clients, multilingual text layout, font packs,
previews, simulators and the host-side hardware tools.

It came from the ``cremind-tag`` repository's ``companion/`` package, which
now holds firmware only. Nothing here imports Cremind's server (storage, API,
agents): the same code runs inside the backend, in a desktop hardware host,
and in the developer tools. Hardware dependencies (pyserial, ICU, HarfBuzz,
FreeType, cbor2) come from the ``tags`` extra and are imported lazily by the
modules that need them, so a Cremind without the extra still starts.
"""

from app.__version__ import __version__

__all__ = ["__version__"]
