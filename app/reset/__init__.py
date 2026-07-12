"""Per-profile "clean data" feature: reset a profile's data by component or preset.

Public surface:
- :func:`app.reset.components.expand_scope` — resolve a scope into component keys.
- :func:`app.reset.engine.run_clean` — execute the teardown for a component set.
- :class:`app.reset.deps.Deps` — the injected storage/registry bundle.
"""
