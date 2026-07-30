"""The two Picker URL builders must agree.

The Picker flow is implemented twice on purpose: the gdrive skill builds its own
URL (it owns the token and must work standalone, driven by the agent), and the
backend builds one for the web UI and the CLI. Google's OAuth-parameter Picker is
a newer, sparsely documented surface, so a parameter rename would otherwise be
fixed in one place and silently left broken in the other.

The skill lives outside the app package (it is a ``uv run`` script with its own
vendored deps), so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

import app.drive.grant_flow as backend

_SKILL_SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "app" / "skills" / "builtin" / "gdrive" / "scripts"
)

# The skill's own package is also called ``app``, which would collide with the
# repo's. Mount it under a private name so its relative imports still resolve.
_ALIAS = "gdrive_skill_under_test"

# Parameters that legitimately differ: the backend routes consent through its own
# callback and tracks rounds with its own state, while the skill waits on the
# shared OAuth inbox. Everything else must match exactly.
_CONTEXT_PARAMS = {"client_id", "redirect_uri", "state", "login_hint"}
# The skill exchanges the code only as a fallback, so it always sends a PKCE
# challenge; the backend never redeems the code and sends none.
_SKILL_ONLY_PARAMS = {"code_challenge", "code_challenge_method"}


@pytest.fixture(scope="module")
def skill_grant():
    target = f"{_ALIAS}.app.grant"
    if target in sys.modules:
        return sys.modules[target]
    root = types.ModuleType(_ALIAS)
    root.__path__ = [str(_SKILL_SCRIPTS)]
    sys.modules[_ALIAS] = root
    inner = types.ModuleType(f"{_ALIAS}.app")
    inner.__path__ = [str(_SKILL_SCRIPTS / "app")]
    sys.modules[f"{_ALIAS}.app"] = inner
    return importlib.import_module(target)


def _skill_params(skill_grant, **overrides):
    kwargs = dict(
        client_id="cid",
        redirect_uri="http://localhost:9/cb",
        state="s" * 24,
        code_challenge="chal",
    )
    kwargs.update(overrides)
    return skill_grant.build_picker_params(**kwargs)


def _backend_params(**overrides):
    kwargs = dict(client_id="cid", redirect="http://localhost:9/cb", state="s" * 24)
    kwargs.update(overrides)
    return backend.build_picker_params(**kwargs)


def test_default_parameter_names_match(skill_grant):
    skill = set(_skill_params(skill_grant)) - _SKILL_ONLY_PARAMS
    assert skill == set(_backend_params())


def test_shared_parameter_values_match(skill_grant):
    skill = _skill_params(skill_grant)
    api = _backend_params()
    for key in set(api) - _CONTEXT_PARAMS:
        assert skill[key] == api[key], key


def test_optional_parameters_match(skill_grant):
    opts_skill = dict(file_ids=["a", "b"], allow_folders=False, allow_multiple=False,
                      mime_types=["text/csv"])
    skill = _skill_params(skill_grant, **opts_skill)
    api = _backend_params(**opts_skill)
    assert skill["file_ids"] == api["file_ids"] == "a,b"
    assert skill["mimetypes"] == api["mimetypes"] == "text/csv"
    for absent in ("allow_multiple", "allow_folder_selection"):
        assert absent not in skill and absent not in api


def test_both_request_drive_file_alone(skill_grant):
    """Google rejects a Picker request carrying any scope besides drive.file."""
    expected = "https://www.googleapis.com/auth/drive.file"
    assert _skill_params(skill_grant)["scope"] == expected
    assert _backend_params()["scope"] == expected


def test_both_use_the_same_authorization_endpoint(skill_grant):
    assert skill_grant._AUTH_ENDPOINT == backend.AUTH_ENDPOINT


def test_skill_parses_picked_ids(skill_grant):
    assert skill_grant.parse_picked_ids("code=x&picked_file_ids=a,b,c") == ["a", "b", "c"]
    assert skill_grant.parse_picked_ids("code=x") == []
    assert skill_grant.parse_picked_ids("picked_file_ids=%20a%20,,b") == ["a", "b"]
