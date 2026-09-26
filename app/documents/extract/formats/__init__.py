"""Per-format extractors. Each module exposes ``extract_<format>(ctx)``, which
fills ``ctx.result`` from ``ctx.req``; :mod:`app.documents.extract.dispatch`
picks the module by kind and turns whatever it raises into a reason.

Every third-party parser is imported inside the function that uses it: the
worker must start fast and a missing optional extra must fail one file with
``awaiting_extractor``, not the whole worker at import.
"""
