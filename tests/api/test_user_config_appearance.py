"""The ``appearance`` and ``chat`` groups of /api/config/user — the web UI's
theme and font, and whether a reply's Thinking Process opens by itself, kept
per profile so every device follows them.

It rides the generic per-profile config (no table of its own, so no
migration), which brings three things these tests pin: a color or a font name
is checked before it is stored (it ends up inside a CSS declaration), the
schema tells the Config page to leave both groups to where they are edited
(Settings → Appearance; the Auto-open switch in the chat), and a change
wakes the profile's open apps so a theme set with ``cremind config set``
applies without a reload.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from app.api import user_config as user_config_api
from app.config.config_schema import CONFIG_SCHEMA, FONT_IDS, THEME_IDS

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESETS_TS = REPO_ROOT / "ui" / "src" / "appearance" / "presets.ts"


class _FakeConfigStorage:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    def get_all(self, table: str, profile: str = "admin") -> dict[str, str]:
        return {key: value for (p, key), value in self.rows.items() if p == profile}

    def set(self, table: str, key: str, value: str, profile: str = "admin") -> None:
        self.rows[(profile, key)] = value

    def delete(self, table: str, key: str, profile: str = "admin") -> bool:
        return self.rows.pop((profile, key), None) is not None


def _handler(storage: _FakeConfigStorage, path: str, method: str) -> Callable:
    for route in user_config_api.get_user_config_routes(storage):  # type: ignore[arg-type]
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} is not registered")


def _req(*, username: str = "li", body: dict | None = None, key: str = ""):
    async def _json():
        return body or {}

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username),
        json=_json,
        path_params={"key": key},
    )


def _call(handler: Callable, request) -> tuple[int, dict]:
    response = asyncio.run(handler(request))
    return response.status_code, json.loads(response.body)


@pytest.fixture
def pings(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(user_config_api, "publish_settings_state_changed", seen.append)
    return seen


def _put(storage: _FakeConfigStorage, values: dict, username: str = "li") -> tuple[int, dict]:
    return _call(_handler(storage, "/api/config/user", "PUT"), _req(username=username, body={"values": values}))


def test_a_whole_custom_theme_is_stored_for_the_profile_and_wakes_its_apps(pings) -> None:
    storage = _FakeConfigStorage()
    status, body = _put(storage, {
        "appearance.theme": "custom",
        "appearance.custom_accent": "#EA580C",
        "appearance.custom_background": "#1c1917",
        "appearance.custom_surface": "#292524",
        "appearance.custom_text": "#f5f5f4",
        "appearance.font": "custom",
        "appearance.custom_font": "'Iowan Old Style', Georgia, serif",
        "appearance.font_size": 115,
        "chat.thinking_process": "live",
    })
    assert status == 200, body
    assert storage.rows[("li", "appearance.custom_accent")] == "#EA580C"
    assert storage.rows[("li", "appearance.font_size")] == "115"
    assert pings == ["li"], "one wake-up for the batch, for this profile only"

    status, body = _call(_handler(storage, "/api/config/user", "GET"), _req(username="li"))
    assert status == 200
    assert body["values"]["appearance.font_size"] == 115
    assert body["values"]["appearance.theme"] == "custom"
    # Another profile still sees nothing of it.
    _, other = _call(_handler(storage, "/api/config/user", "GET"), _req(username="bob"))
    assert other["values"]["appearance.theme"] is None
    assert other["defaults"]["appearance.theme"] == "light"


@pytest.mark.parametrize(("key", "value"), [
    ("appearance.custom_accent", "red"),
    ("appearance.custom_accent", "#25e"),  # short hex: the UI's color math reads six digits
    ("appearance.custom_background", "#1c1917; } body { display: none"),
    ("appearance.custom_font", "Georgia; } * { color: red"),
    ("appearance.custom_font", "url(https://example.com/x.woff)"),
    ("appearance.custom_font", " , '' "),
    ("appearance.theme", "solarized"),
    ("appearance.font", "comic"),
    ("appearance.font_size", 300),
    ("chat.thinking_process", "expanded"),
])
def test_a_value_that_would_not_render_is_refused_and_nothing_is_stored(pings, key, value) -> None:
    storage = _FakeConfigStorage()
    status, body = _put(storage, {"appearance.theme": "dark", key: value})
    assert status == 400, body
    assert key in body["details"]
    assert storage.rows == {}, "a batch with one bad value stores none of it"
    assert pings == []


def test_reset_wakes_the_apps_only_when_there_was_something_to_reset(pings) -> None:
    storage = _FakeConfigStorage()
    storage.set("user_config", "appearance.theme", "nord", profile="li")
    reset = _handler(storage, "/api/config/user/{key}", "DELETE")

    assert _call(reset, _req(key="appearance.theme"))[1]["deleted"] is True
    assert _call(reset, _req(key="appearance.theme"))[1]["deleted"] is False
    assert pings == ["li"]


def test_the_schema_sends_both_groups_where_they_are_edited() -> None:
    status, body = _call(_handler(_FakeConfigStorage(), "/api/config/schema", "GET"), _req())
    assert status == 200
    groups = body["groups"]
    assert groups["appearance"]["page"] == "appearance"
    assert groups["appearance"]["fields"]["custom_accent"]["format"] == "color"
    assert groups["appearance"]["fields"]["custom_font"]["format"] == "font_family"
    assert groups["chat"]["page"] == "chat"
    assert groups["chat"]["fields"]["thinking_process"]["enum"] == ["collapsed", "live"]
    # Every other group is still a card on the Config page.
    assert [name for name, group in groups.items() if "page" in group] == ["appearance", "chat"]


def _ts_ids(const: str) -> list[str]:
    """The ``id``s of one exported preset array in ui/src/appearance/presets.ts."""
    text = PRESETS_TS.read_text(encoding="utf-8")
    start = text.index(f"export const {const}")
    end = text.index("\n];", start)
    return re.findall(r"^\s+id: '([a-z_]+)',", text[start:end], re.M)


def test_the_ui_ships_exactly_the_presets_the_server_accepts() -> None:
    """A preset only the UI knows is refused on save; one only the server knows
    is a value the UI cannot paint."""
    themes = _ts_ids("THEME_PRESETS")
    assert set(themes) | {"system", "custom"} == set(THEME_IDS), themes
    assert len(themes) == len(set(themes))
    fonts = _ts_ids("FONT_PRESETS")
    assert set(fonts) | {"custom"} == set(FONT_IDS), fonts
    assert len(fonts) == len(set(fonts))


def test_the_schema_defaults_are_what_the_ui_falls_back_to() -> None:
    from app.config.user_config import resolve_default

    text = PRESETS_TS.read_text(encoding="utf-8")
    start = text.index("export const DEFAULT_APPEARANCE")
    block = text[start:text.index("\n};", start)]
    ui_defaults = dict(re.findall(r"^\s+(\w+): '?([^',]+)'?,", block, re.M))
    camel = {
        "appearance.theme": "theme", "appearance.font": "font",
        "appearance.font_size": "fontSize", "appearance.custom_font": "customFont",
        "appearance.custom_accent": "customAccent",
        "appearance.custom_background": "customBackground",
        "appearance.custom_surface": "customSurface", "appearance.custom_text": "customText",
        # Read and saved with the appearance, though set from the chat.
        "chat.thinking_process": "thinkingProcess",
    }
    schema_keys = {f"appearance.{f}" for f in CONFIG_SCHEMA["appearance"].fields}
    schema_keys |= {f"chat.{f}" for f in CONFIG_SCHEMA["chat"].fields}
    assert set(camel) == schema_keys
    for key, ts_name in camel.items():
        assert ui_defaults[ts_name] == str(resolve_default(key)), key
