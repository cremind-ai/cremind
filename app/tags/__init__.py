"""Cremind Tag: e-paper tags that show a profile's updates.

A PC companion (the ``cremind-tag`` repo) connects OUT to Cremind and drives
the radio hardware. Cremind's side is four layers:

- :mod:`app.tags.journal` — an ordered, per-profile log of allowlisted events,
  written in the SAME transaction as the state it describes;
- :mod:`app.tags.projection` — a background worker that turns journal entries
  into delivery jobs (one card for one tag), expires and prunes them;
- :mod:`app.tags.storage` / :mod:`app.tags.service` — companions, credentials,
  devices, deliveries, commands, previews, settings;
- the REST surfaces: ``/api/tags`` (profile), ``/api/tags/hardware`` (admin)
  and ``/api/tag-connector/v1`` (companion credentials only).

Nothing here imports :mod:`app.storage` at module level beyond the ORM models,
so the storage modules can call into the journal lazily without a cycle.
"""
