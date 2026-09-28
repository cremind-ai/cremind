"""Cremind Tag hardware hosting: running gateway workers where the gateway is plugged in.

- :mod:`.local_connector` — how a worker the backend runs itself reaches Cremind:
  the connector API's own rules, called in process.

The runtime code itself (the worker, its queue, the protocol clients) is
:mod:`app.tags.runtime`; this package decides where it runs and wires it to
Cremind. Nothing here is imported at boot unless hardware hosting is on, and
the runtime's hardware dependencies (the ``tags`` extra) are imported lazily.
"""
