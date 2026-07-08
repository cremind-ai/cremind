"""Blueprint — package a single profile's *design* into a portable file.

A ``.cremind-blueprint`` captures the design of one profile (persona, tool
enable/config, LLM provider/model selection, changed settings, skills, events,
and skill listeners) so another user can import it into a **new** profile in
their own install. Unlike a full-system backup it contains **no secrets** and
no chat/runtime data — it is design + public settings only, expressed as
semantic JSON documents (not a DB row dump) so it stays portable across schema
versions and across environments.

See :mod:`app.blueprint.manifest` for the package format and the version
compatibility gate, :mod:`app.blueprint.engine` for the export engine, and
:mod:`app.blueprint.apply` for the import step appliers.
"""
