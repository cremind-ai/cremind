"""Non-local sources for User Document Search (Google Drive).

- :mod:`.drive_client` — the read-only Drive REST client: classified errors,
  listings that say when they are incomplete, the change feed a page at a
  time, and in-memory downloads/exports.
- :mod:`.drive` — the Drive half of a profile's runtime, built on the client.

Nothing is imported here: the engine loads these only when a profile turns
Drive on, and the client pulls in ``httpx``.
"""
