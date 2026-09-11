"""The Google skills' ``link`` and gdrive's ``grant``: who receives Google's redirect.

These live under ``tests/`` rather than ``<skill>/scripts/tests/`` because only
``tests/`` runs in CI (see :mod:`tests.skills.test_google_unlink`), and they
exercise gmail's copy of ``google/auth.py`` — which
:mod:`tests.skills.test_google_auth_parity` pins byte-identical across all five
skills, so this covers every one of them.

What is pinned:

* A backend-managed run (``CREMIND_SYSTEM_DIR`` set, as the backend always sets
  it) ALWAYS waits on the OAuth inbox. It advertises the redirect the backend
  injected, or ``DEFAULT_BACKEND_REDIRECT_URI`` when none was, and never opens a
  listener of its own — a random port inside a pod is unreachable from the
  user's browser. ``complete-link`` (``submit_callback``) feeds the same waiter.
* Only a standalone run uses ``run_local_server``.
* PKCE is requested explicitly, and a consent URL without a challenge is refused
  before anything is shown to the user.
* The token exchange sends the redirect exactly as advertised (Google compares
  it byte for byte), even though oauthlib is handed an https-rewritten response.

``google_auth_oauthlib`` is not installed in the repo venv — the skills pull it
in through their own ``uv`` metadata, and ``link`` imports it lazily — so each
test mounts a fake ``InstalledAppFlow`` in ``sys.modules``.
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import sys
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from app.skills.sync import BUILTIN_SKILLS_DIR

GOOGLE_DIR = BUILTIN_SKILLS_DIR / "gmail" / "scripts" / "app" / "google"
GDRIVE_SCRIPTS = BUILTIN_SKILLS_DIR / "gdrive" / "scripts"

# Private names, so neither collides with the modules other test files mount.
_PKG = "gskill_google_link_flow"
_GDRIVE_ALIAS = "gdrive_skill_link_flow"

STATE = "st4teABCDEFGHijklmnop0123456789"  # oauthlib-shaped: URL-safe, inbox-safe
EMAIL = "u@example.com"
QUERY = f"state={STATE}&code=4%2F0Ab-code&scope=email%20openid"


def _load_auth():
    """gmail's ``google/auth.py`` under a synthetic parent package (its relative
    ``account_key`` import needs one; the skill's own ``app`` would collide)."""
    package = types.ModuleType(_PKG)
    package.__path__ = [str(GOOGLE_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PKG] = package
    spec = importlib.util.spec_from_file_location(f"{_PKG}.auth", GOOGLE_DIR / "auth.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_grant():
    """gdrive's ``grant.py``, mounted the way tests/drive/test_picker_param_parity
    mounts it (the skill's package is also called ``app``)."""
    target = f"{_GDRIVE_ALIAS}.app.grant"
    if target in sys.modules:
        return sys.modules[target]
    root = types.ModuleType(_GDRIVE_ALIAS)
    root.__path__ = [str(GDRIVE_SCRIPTS)]  # type: ignore[attr-defined]
    sys.modules[_GDRIVE_ALIAS] = root
    inner = types.ModuleType(f"{_GDRIVE_ALIAS}.app")
    inner.__path__ = [str(GDRIVE_SCRIPTS / "app")]  # type: ignore[attr-defined]
    sys.modules[f"{_GDRIVE_ALIAS}.app"] = inner
    return importlib.import_module(target)


auth = _load_auth()
grant = _load_grant()


# ── a fake google_auth_oauthlib ──────────────────────────────────────────────

def _jwt(claims: Dict[str, Any]) -> str:
    def seg(obj: Dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


class _Creds:
    refresh_token = "rt-1"
    token = "at-1"
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    scopes = ["openid", "email"]
    id_token = _jwt({"email": EMAIL})


class FakeFlow:
    """Just enough of google-auth-oauthlib's ``InstalledAppFlow``.

    ``fetch_token`` records ``redirect_uri`` as it stands at call time, because
    that is the value the real exchange posts to Google.
    """

    def __init__(self, harness: "Harness", client_config, scopes, kwargs):
        self.harness = harness
        self.client_config = client_config
        self.scopes = scopes
        self.from_config_kwargs = kwargs
        self.redirect_uri: str | None = None
        self.code_verifier: str | None = None
        self.credentials = _Creds()
        self.exchanges: List[Dict[str, Any]] = []
        self.local_server_calls: List[Dict[str, Any]] = []

    def authorization_url(self, **kwargs):
        params = {
            "response_type": "code",
            "client_id": self.client_config["installed"]["client_id"],
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": STATE,
            **kwargs,
        }
        if self.harness.pkce:
            self.code_verifier = "v" * 64
            params.update(code_challenge="c" * 43, code_challenge_method="S256")
        url = f"{auth.GOOGLE_AUTH_URI}?{urlencode(params)}"
        self.harness.consent_urls.append(url)
        self.harness.consent_shown.set()
        return url, STATE

    def fetch_token(self, **kwargs):
        self.exchanges.append({"kwargs": kwargs, "redirect_uri": self.redirect_uri})
        return {"access_token": "at-1"}

    def run_local_server(self, **kwargs):
        """Mirror the real one's order: bind, point redirect_uri at the bound port,
        build the consent URL through ``self.authorization_url`` with the kwargs it
        does not consume (the seam ``_guard_pkce`` shadows), then exchange."""
        self.local_server_calls.append(dict(kwargs))
        self.redirect_uri = f"http://{kwargs['host']}:54321/"
        self.authorization_url(
            **{k: v for k, v in kwargs.items() if k in ("access_type", "prompt")}
        )
        self.fetch_token(authorization_response=f"https://{kwargs['host']}:54321/?{QUERY}")
        return self.credentials


class Harness:
    def __init__(self) -> None:
        self.pkce = True
        self.flows: List[FakeFlow] = []
        self.consent_urls: List[str] = []
        self.consent_shown = threading.Event()

    @property
    def flow(self) -> FakeFlow:
        assert self.flows, "link never built a flow"
        return self.flows[-1]


@pytest.fixture
def oauthlib(monkeypatch) -> Harness:
    harness = Harness()

    class InstalledAppFlow:
        @classmethod
        def from_client_config(cls, client_config, scopes, **kwargs):
            flow = FakeFlow(harness, client_config, scopes, kwargs)
            harness.flows.append(flow)
            return flow

    package = types.ModuleType("google_auth_oauthlib")
    package.__path__ = []  # type: ignore[attr-defined]
    flow_module = types.ModuleType("google_auth_oauthlib.flow")
    flow_module.InstalledAppFlow = InstalledAppFlow  # type: ignore[attr-defined]
    package.flow = flow_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", package)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_module)
    return harness


# ── environments ─────────────────────────────────────────────────────────────

def _bound_waiter(module, monkeypatch) -> None:
    """Poll fast, and never let a broken test hang for the real 600s."""
    monkeypatch.setattr(module, "_INBOX_POLL_S", 0.01)
    real = module._await_oauth_callback

    def bounded(state, *, timeout=600.0):
        return real(state, timeout=min(timeout, 5.0))

    monkeypatch.setattr(module, "_await_oauth_callback", bounded)


@pytest.fixture
def backend(tmp_path, monkeypatch) -> Path:
    """A backend-managed run. ``app.config.settings`` exports CREMIND_SYSTEM_DIR
    into this very process, so it is always set explicitly here."""
    system = tmp_path / "system"
    system.mkdir()
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(system))
    _bound_waiter(auth, monkeypatch)
    _bound_waiter(grant.auth, monkeypatch)
    return system / "oauth_inbox"


@pytest.fixture
def standalone(monkeypatch) -> None:
    monkeypatch.delenv("CREMIND_SYSTEM_DIR", raising=False)

    def no_inbox(*_args, **_kwargs):
        raise AssertionError("a standalone run must never wait on the backend inbox")

    monkeypatch.setattr(auth, "_await_oauth_callback", no_inbox)
    monkeypatch.setattr(grant.auth, "_await_oauth_callback", no_inbox)


def _drop_in_inbox(inbox: Path, query: str, state: str = STATE) -> None:
    """What the backend's ``GET /api/oauth/callback`` handler writes."""
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{state}.txt").write_text(query, encoding="utf-8")


def _token_path(tmp_path: Path) -> Path:
    scripts = tmp_path / "skills" / "gmail" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    return scripts / ".google_token.json"


def _link(tmp_path: Path, **overrides):
    kwargs: Dict[str, Any] = dict(
        token_path=_token_path(tmp_path),
        client_id="cid",
        client_secret="csecret",
        scopes=["openid", "email"],
        redirect_uri=None,
    )
    kwargs.update(overrides)
    return auth.link(**kwargs)


def _in_background(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Run ``fn`` on a thread; the returned ``join`` re-raises its failure."""
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by join()
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()

    def join() -> Any:
        worker.join(timeout=10)
        assert not worker.is_alive(), "background step never finished"
        if "error" in box:
            raise box["error"]
        return box.get("result")

    return join


def _consent_redirect(url: str) -> str:
    return parse_qs(urlparse(url).query)["redirect_uri"][0]


# ── link, backend-managed ────────────────────────────────────────────────────

def test_without_an_injected_redirect_the_default_is_advertised_and_complete_link_finishes_it(
    oauthlib, backend, tmp_path, capsys
):
    """The remote/Ingress case: the backend could not derive a loopback callback,
    so nothing was injected. ``link`` must still wait on the inbox — never a
    random-port listener — and the URL the user pastes into ``complete-link``
    must reach that waiter."""

    def complete_link() -> Dict[str, Any]:
        assert oauthlib.consent_shown.wait(5), "link never showed a consent URL"
        return auth.submit_callback(f"{auth.DEFAULT_BACKEND_REDIRECT_URI}?{QUERY}")

    join = _in_background(complete_link)
    data = _link(tmp_path, open_browser=True)
    assert join() == {"submitted": True, "state": STATE}

    flow = oauthlib.flow
    assert auth.DEFAULT_BACKEND_REDIRECT_URI == "http://localhost:1515/api/oauth/callback"
    assert _consent_redirect(oauthlib.consent_urls[-1]) == auth.DEFAULT_BACKEND_REDIRECT_URI
    assert flow.local_server_calls == [], "a backend-managed run opened its own listener"

    [exchange] = flow.exchanges
    # Google compares the exchange's redirect_uri with the advertised one exactly.
    assert exchange["redirect_uri"] == auth.DEFAULT_BACKEND_REDIRECT_URI
    # oauthlib only parses https responses; the query must survive unchanged.
    response = urlparse(exchange["kwargs"]["authorization_response"])
    assert response.scheme == "https"
    assert response.query == QUERY

    saved = json.loads(_token_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["email"] == data["email"] == EMAIL
    assert saved["refresh_token"] == "rt-1"
    assert not (backend / f"{STATE}.txt").exists(), "the inbox file was not consumed"

    out = capsys.readouterr().out
    assert "Please visit this URL to authorize this application:" in out
    assert 'complete-link --response "<that URL>"' in out


@pytest.mark.parametrize(
    "redirect",
    [
        "http://127.0.0.1:1515/api/oauth/callback",
        "http://localhost:8443/api/oauth/callback",
        "http://[::1]:1515/api/oauth/callback",
    ],
)
def test_an_injected_loopback_redirect_is_advertised_as_is(oauthlib, backend, tmp_path, redirect):
    _drop_in_inbox(backend, QUERY)  # the backend's callback route got there first

    _link(tmp_path, redirect_uri=redirect)

    flow = oauthlib.flow
    assert _consent_redirect(oauthlib.consent_urls[-1]) == redirect
    [exchange] = flow.exchanges
    assert exchange["redirect_uri"] == redirect
    response = urlparse(exchange["kwargs"]["authorization_response"])
    assert (response.scheme, response.query) == ("https", QUERY)
    assert flow.local_server_calls == []


def test_pkce_is_requested_explicitly(oauthlib, backend, tmp_path):
    _drop_in_inbox(backend, QUERY)

    _link(tmp_path)

    assert oauthlib.flow.from_config_kwargs == {"autogenerate_code_verifier": True}
    assert "code_challenge=" in oauthlib.consent_urls[-1]


def test_a_consent_url_without_pkce_is_refused_before_it_is_shown(
    oauthlib, backend, tmp_path, capsys
):
    oauthlib.pkce = False
    _drop_in_inbox(backend, QUERY)

    with pytest.raises(auth.AuthError, match="PKCE"):
        _link(tmp_path)

    assert "Please visit" not in capsys.readouterr().out
    assert oauthlib.flow.exchanges == []
    assert not _token_path(tmp_path).exists()
    assert (backend / f"{STATE}.txt").exists(), "refused before waiting, so nothing consumed"


def test_a_denial_in_the_inbox_fails_the_link(oauthlib, backend, tmp_path):
    """The callback handler writes the inbox FIRST, even for ``error=...``."""
    _drop_in_inbox(backend, f"error=access_denied&state={STATE}")

    with pytest.raises(auth.AuthError, match="denied"):
        _link(tmp_path)

    assert oauthlib.flow.exchanges == []
    assert not _token_path(tmp_path).exists()
    assert not (backend / f"{STATE}.txt").exists()


def test_a_denial_pasted_into_complete_link_stops_the_waiting_link_at_once(
    oauthlib, backend, tmp_path
):
    """``complete-link`` with the ``error=...`` URL still reports the denial, and
    also hands it to the waiter — otherwise ``link`` sits out its full timeout
    after the user has already been told consent failed."""

    def complete_link() -> None:
        assert oauthlib.consent_shown.wait(5), "link never showed a consent URL"
        with pytest.raises(auth.AuthError, match="denied"):
            auth.submit_callback(
                f"{auth.DEFAULT_BACKEND_REDIRECT_URI}?error=access_denied&state={STATE}"
            )

    join = _in_background(complete_link)
    started = time.monotonic()
    with pytest.raises(auth.AuthError, match="denied"):
        _link(tmp_path)
    join()

    assert time.monotonic() - started < 4, "the waiter only gave up at its timeout"
    assert oauthlib.flow.exchanges == []
    assert not (backend / f"{STATE}.txt").exists()


def test_a_pasted_state_with_a_trailing_newline_is_refused(backend):
    """``$`` accepts a trailing newline; the inbox file name must not."""
    with pytest.raises(auth.AuthError, match="state"):
        auth.submit_callback(f"code=abc&state={STATE}%0A")
    assert not backend.exists() or not any(backend.iterdir())


def test_an_inbox_response_is_consumed_exactly_once(backend):
    """A replayed state finds nothing: the first waiter deletes the file."""
    _drop_in_inbox(backend, QUERY)

    assert auth._await_oauth_callback(STATE, timeout=2) == QUERY
    with pytest.raises(auth.AuthError, match="Timed out"):
        auth._await_oauth_callback(STATE, timeout=0.05)


# ── link, standalone ─────────────────────────────────────────────────────────

def test_a_standalone_run_uses_its_own_loopback_listener(oauthlib, standalone, tmp_path):
    """Even a leftover redirect in the environment cannot route a standalone run
    to an inbox nobody writes."""
    _link(
        tmp_path,
        redirect_uri="http://localhost:1515/api/oauth/callback",
        open_browser=False,
    )

    flow = oauthlib.flow
    assert flow.from_config_kwargs == {"autogenerate_code_verifier": True}
    assert flow.local_server_calls == [
        {
            "host": "localhost",
            "port": 0,
            "access_type": "offline",
            "prompt": "consent",
            "open_browser": False,
        }
    ]
    assert _consent_redirect(oauthlib.consent_urls[-1]) == "http://localhost:54321/"
    assert _token_path(tmp_path).exists()


def test_a_standalone_run_refuses_a_consent_url_without_pkce(oauthlib, standalone, tmp_path):
    oauthlib.pkce = False

    with pytest.raises(auth.AuthError, match="PKCE"):
        _link(tmp_path, open_browser=False)

    # run_local_server builds the URL and opens the browser in one call; the
    # guard sits between the two, so nothing past it ran.
    assert oauthlib.flow.exchanges == []
    assert not _token_path(tmp_path).exists()


# ── gdrive grant ─────────────────────────────────────────────────────────────

class GrantRun:
    """Drives ``grant.run_grant`` with the Drive API and credentials faked out."""

    def __init__(self, tmp_path: Path, monkeypatch):
        scripts = tmp_path / "skills" / "gdrive" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        self.token_path = scripts / ".google_token.json"
        self.token_path.write_text(json.dumps({"email": EMAIL}), encoding="utf-8")
        self.grants_path = scripts / ".drive_grants.json"
        self.params: Dict[str, str] = {}
        self.url_built = threading.Event()

        real_build = grant.build_authorize_url

        def spy(**kwargs):
            url, params = real_build(**kwargs)
            self.params.update(params)
            self.url_built.set()
            return url, params

        monkeypatch.setattr(grant, "build_authorize_url", spy)
        monkeypatch.setattr(grant.auth, "get_credentials", lambda _path: (object(), {}))

    def __call__(self, **overrides):
        kwargs: Dict[str, Any] = dict(
            token_path=self.token_path,
            grants_path=self.grants_path,
            client_id="cid",
            client_secret="csecret",
            redirect_uri=None,
            build_service=lambda _creds: "svc",
            get_file=lambda _svc, file_id: {"id": file_id, "name": "F", "mimeType": "text/plain"},
            list_files=lambda *_a, **_k: {"files": []},
            timeout=5,
        )
        kwargs.update(overrides)
        return grant.run_grant(**kwargs)


def test_grant_under_the_backend_waits_on_the_inbox_with_the_default_redirect(
    backend, tmp_path, monkeypatch, capsys
):
    run = GrantRun(tmp_path, monkeypatch)

    def never_listen(*_args, **_kwargs):
        raise AssertionError("a backend-managed grant must not open its own listener")

    monkeypatch.setattr(grant, "_capture_via_local_server", never_listen)

    def complete_link() -> Dict[str, Any]:
        assert run.url_built.wait(5), "grant never built a Picker URL"
        redirect, state = run.params["redirect_uri"], run.params["state"]
        return grant.auth.submit_callback(f"{redirect}?state={state}&code=c&picked_file_ids=f1")

    join = _in_background(complete_link)
    result = run()
    join()

    assert run.params["redirect_uri"] == grant.auth.DEFAULT_BACKEND_REDIRECT_URI
    assert run.params["code_challenge_method"] == "S256"  # grant keeps its own PKCE
    assert result["granted"] == 1
    assert result["picked_file_ids"] == ["f1"]
    assert json.loads(run.grants_path.read_text(encoding="utf-8"))[0]["id"] == "f1"
    assert 'complete-link --response "<that URL>"' in capsys.readouterr().out


def test_grant_under_the_backend_advertises_an_injected_redirect(backend, tmp_path, monkeypatch):
    run = GrantRun(tmp_path, monkeypatch)
    injected = "http://127.0.0.1:1515/api/oauth/callback"

    def backend_callback() -> None:
        assert run.url_built.wait(5)
        state = run.params["state"]
        _drop_in_inbox(backend, f"state={state}&code=c&picked_file_ids=f1", state=state)

    join = _in_background(backend_callback)
    result = run(redirect_uri=injected)
    join()

    assert run.params["redirect_uri"] == injected
    assert result["granted"] == 1


def test_grant_refuses_a_response_that_does_not_carry_its_state(backend, tmp_path, monkeypatch):
    """The inbox file is keyed by state, so a genuine response always carries it;
    one that does not is not this grant's."""
    run = GrantRun(tmp_path, monkeypatch)

    def stateless_response() -> None:
        assert run.url_built.wait(5)
        _drop_in_inbox(backend, "code=c&picked_file_ids=f1", state=run.params["state"])

    join = _in_background(stateless_response)
    with pytest.raises(grant.auth.AuthError, match="state mismatch"):
        run()
    join()
    assert not run.grants_path.exists()


@pytest.mark.parametrize("returned", ["own", "someone-elses-state-0123"])
def test_a_standalone_grant_uses_its_own_listener_and_requires_the_exact_state(
    standalone, tmp_path, monkeypatch, returned
):
    run = GrantRun(tmp_path, monkeypatch)

    def fake_capture(port_box, *, timeout):
        port_box["port"] = 4242
        port_box["ready"].set()
        assert run.url_built.wait(5)
        state = run.params["state"] if returned == "own" else returned
        return f"state={state}&code=c&picked_file_ids=f1"

    monkeypatch.setattr(grant, "_capture_via_local_server", fake_capture)

    if returned == "own":
        # A leftover redirect is ignored: with no inbox, only the listener can answer.
        result = run(redirect_uri="http://localhost:1515/api/oauth/callback")
        assert run.params["redirect_uri"] == "http://localhost:4242/"
        assert result["granted"] == 1
    else:
        with pytest.raises(grant.auth.AuthError, match="state mismatch"):
            run()
        assert not run.grants_path.exists()
