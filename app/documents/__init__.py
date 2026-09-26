"""Documentation search — the agent searching and reasoning over the user's own files.

A per-profile RAG index over a chosen folder (and, optionally, the Google Drive
files Cremind has been granted), kept in sync incrementally, searched with
vector + full-text retrieval, and answered with verifiable citations.

Independent of ``cremind_documentation_search`` / :mod:`app.cremind_documents`, which only
indexes Cremind's own manual. The two share no collection, table, tool or
prompt text.

Layout:

- :mod:`app.documents.settings` — the admin gate, per-profile options, root
  validation, and confirm-before-destroy change plans.
- ``app.documents.index`` — the per-profile SQLite index file (manifest, chunks,
  FTS5), the vector collections, and the sync engine that fills them.

The main-database half (settings, caption cache, vision quota) is in
:mod:`app.storage.documents_storage`.
"""
