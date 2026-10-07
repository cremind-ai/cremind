"""A refused ``cremind config set`` says which value was refused, and why.

/api/config/user answers a bad value with ``{"error": "validation failed",
"details": {key: why}}``. The CLI kept only ``error``, so setting a color the
server could not take — easy to do from a shell, where an unquoted ``#2563eb``
starts a comment — printed "server returned 400: validation failed" and nothing
about what a valid one looks like.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.cli.client._base import APIError, Client


def _raise_for(status: int, body: object) -> APIError:
    resp = httpx.Response(status, content=json.dumps(body).encode())
    with pytest.raises(APIError) as info:
        Client._check_response(resp)
    return info.value


def test_the_details_map_names_each_refused_key_and_the_reason() -> None:
    err = _raise_for(400, {
        "error": "validation failed",
        "details": {
            "appearance.custom_accent": "'red' is not a #rrggbb hex color (e.g. '#2563eb')",
            "appearance.font_size": "Value 300 above maximum 140",
        },
    })
    assert str(err) == (
        "server returned 400: validation failed — "
        "appearance.custom_accent: 'red' is not a #rrggbb hex color (e.g. '#2563eb'); "
        "appearance.font_size: Value 300 above maximum 140"
    )


def test_a_reply_without_details_reads_as_before() -> None:
    assert str(_raise_for(404, {"error": "unknown config key"})) == "server returned 404: unknown config key"
    assert str(_raise_for(400, {"error": "x", "details": "not a map"})) == "server returned 400: x"
