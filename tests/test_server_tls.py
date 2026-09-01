"""Resolving TLS config: enable it, refuse it clearly, or stay on plain HTTP.

The default — nothing set — has to keep returning ``None``, because that is
what leaves the dual-uvicorn plain-HTTP path exactly as it was.

Messages are captured off the loguru logger rather than through ``caplog``:
this project logs through loguru, which pytest's handler never sees (same
approach as tests/test_server_ports.py).
"""

from __future__ import annotations

import os

import pytest

from app import server
from app.config.settings import BaseConfig


@pytest.fixture
def tls_env(monkeypatch):
    """A clean TLS-related environment, whatever the developer's .env holds."""
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE_PASSWORD", "", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_AUTO_HOSTS", [], raising=False)
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example.com", raising=False)
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    return monkeypatch


@pytest.fixture
def pair(tmp_path):
    """Two files standing in for a cert/key pair. Contents never get parsed —
    ``_resolve_tls`` only decides whether to hand them to hypercorn."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert")
    key.write_text("key")
    return str(cert), str(key)


def _errors(monkeypatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(server.logger, "error", lambda msg: said.append(str(msg)))
    return said


def _warnings(monkeypatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(server.logger, "warning", lambda msg: said.append(str(msg)))
    return said


def test_nothing_configured_means_plain_http(tls_env) -> None:
    """The default. Returning None is what keeps the existing path untouched."""
    assert server._resolve_tls(None, None, 1515) is None


def test_an_explicit_pair_enables_tls(tls_env, pair) -> None:
    cert, key = pair
    assert server._resolve_tls(cert, key, 1515) == (cert, key)


def test_config_supplies_the_pair_when_the_arguments_do_not(tls_env, pair) -> None:
    """The .env is the canonical place to set this, because an in-app restart
    re-execs without the CLI flags."""
    cert, key = pair
    tls_env.setattr(BaseConfig, "SSL_CERTFILE", cert, raising=False)
    tls_env.setattr(BaseConfig, "SSL_KEYFILE", key, raising=False)
    assert server._resolve_tls(None, None, 1515) == (cert, key)


def test_a_cert_without_a_key_refuses_to_start(tls_env, pair) -> None:
    """Half-configured TLS is a mistake, not a request for plain HTTP — silently
    serving http:// here would be the worst possible reading of it."""
    cert, _ = pair
    said = _errors(tls_env)
    with pytest.raises(SystemExit) as exc:
        server._resolve_tls(cert, None, 1515)
    assert exc.value.code == 1
    assert "CREMIND_SSL_KEYFILE" in said[0]


def test_a_key_without_a_cert_refuses_to_start(tls_env, pair) -> None:
    _, key = pair
    said = _errors(tls_env)
    with pytest.raises(SystemExit):
        server._resolve_tls(None, key, 1515)
    assert "CREMIND_SSL_CERTFILE" in said[0]


def test_a_missing_file_names_the_path(tls_env, pair, tmp_path) -> None:
    """Boot-time failure, before anything has started, with the path in it."""
    _, key = pair
    absent = str(tmp_path / "nope.pem")
    said = _errors(tls_env)
    with pytest.raises(SystemExit) as exc:
        server._resolve_tls(absent, key, 1515)
    assert exc.value.code == 1
    assert absent in said[0]


def test_no_public_bind_falls_back_to_plain_http(tls_env, pair) -> None:
    """CREMIND_UI_PORT=0 means an external proxy owns the origin, so it holds
    the certificate. Warn rather than fail — the env may be set globally."""
    cert, key = pair
    said = _warnings(tls_env)
    assert server._resolve_tls(cert, key, 0) is None
    assert "CREMIND_UI_PORT=0" in said[0]


def test_electron_ignores_tls(tls_env, pair) -> None:
    """The desktop shell loads the UI over http://127.0.0.1:1515; honouring TLS
    here would break its health check for no gain."""
    cert, key = pair
    tls_env.setenv("CREMIND_ELECTRON_PARENT", "1234")
    said = _warnings(tls_env)
    assert server._resolve_tls(cert, key, 1515) is None
    assert "Electron" in said[0]


def test_an_http_app_url_is_called_out(tls_env, pair) -> None:
    """APP_URL feeds the agent card and OAuth redirects, so leaving it http://
    sends browsers to a port that now speaks TLS."""
    cert, key = pair
    tls_env.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    said = _warnings(tls_env)
    assert server._resolve_tls(cert, key, 1515) == (cert, key)
    assert any("APP_URL" in m for m in said)


def test_auto_mode_generates_a_pair(tls_env, tmp_path) -> None:
    tls_env.setattr(BaseConfig, "SSL_MODE", "auto", raising=False)
    tls_env.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    resolved = server._resolve_tls(None, None, 1515)
    assert resolved is not None
    cert, key = resolved
    assert (tmp_path / "tls" / "ca.pem").is_file(), "the CA is what gets trusted"
    assert cert.endswith("cert.pem") and key.endswith("key.pem")


def test_auto_mode_says_how_to_trust_the_ca(tls_env, tmp_path) -> None:
    """This log line is the only in-product pointer for a native install, so it
    has to name something that ships — a command and a URL, not a repo file."""
    said: list[str] = []
    tls_env.setattr(server.logger, "info", lambda msg: said.append(str(msg)))
    tls_env.setattr(BaseConfig, "SSL_MODE", "auto", raising=False)
    tls_env.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)

    server._resolve_tls(None, None, 1515)

    message = next(m for m in said if "CREMIND_SSL=auto" in m)
    assert "cremind tls trust" in message
    assert "/ca.pem" in message
    assert "ca.pem" in message and str(tmp_path) in message
    assert "CONTRIBUTING" not in message, "released installs do not ship it"


def test_an_explicit_pair_wins_over_auto(tls_env, pair, tmp_path) -> None:
    """Someone who supplies a real certificate should get it, even with
    CREMIND_SSL=auto left set in the environment."""
    cert, key = pair
    tls_env.setattr(BaseConfig, "SSL_MODE", "auto", raising=False)
    tls_env.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    assert server._resolve_tls(cert, key, 1515) == (cert, key)
    assert not (tmp_path / "tls" / "ca.pem").exists(), "nothing should be generated"


# ── CREMIND_SSL=after-setup ──────────────────────────────────────────────
#
# The wizard phase serves plain HTTP on purpose: a certificate nobody trusts
# yet turns the very first page a user opens into a security warning, which is
# precisely what this mode exists to avoid. The CA is still generated, because
# the wizard has to hand it over during that phase.


def _after_setup(tls_env, tmp_path, *, bootstrap: bool):
    tls_env.setattr(BaseConfig, "SSL_MODE", "after-setup", raising=False)
    tls_env.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    tls_env.setattr(server, "bootstrap_exists", lambda: bootstrap)


def test_after_setup_serves_plain_http_until_setup_completes(tls_env, tmp_path) -> None:
    _after_setup(tls_env, tmp_path, bootstrap=False)
    assert server._resolve_tls(None, None, 1515) is None


def test_after_setup_still_generates_the_ca_during_the_wizard(tls_env, tmp_path) -> None:
    """The wizard hands this file to the user before any HTTPS page loads."""
    _after_setup(tls_env, tmp_path, bootstrap=False)
    server._resolve_tls(None, None, 1515)
    assert (tmp_path / "tls" / "ca.pem").is_file()


def test_after_setup_explains_the_phase(tls_env, tmp_path) -> None:
    said: list[str] = []
    tls_env.setattr(server.logger, "info", lambda msg: said.append(str(msg)))
    _after_setup(tls_env, tmp_path, bootstrap=False)

    server._resolve_tls(None, None, 1515)

    message = next(m for m in said if "after-setup" in m)
    assert "plain HTTP" in message and "wizard" in message
    # It names where it is going, which doubles as the "APP_URL says https but
    # we are serving http" signal for this phase.
    assert "https://cremind.example.com" in message


def test_after_setup_serves_tls_once_setup_is_done(tls_env, tmp_path) -> None:
    _after_setup(tls_env, tmp_path, bootstrap=True)

    resolved = server._resolve_tls(None, None, 1515)

    assert resolved is not None
    cert, key = resolved
    assert cert.endswith("cert.pem") and key.endswith("key.pem")
    assert (tmp_path / "tls" / "ca.pem").is_file()


def test_after_setup_names_its_own_mode_in_the_trust_log(tls_env, tmp_path) -> None:
    """The post-restart boot must not claim to be CREMIND_SSL=auto."""
    said: list[str] = []
    tls_env.setattr(server.logger, "info", lambda msg: said.append(str(msg)))
    _after_setup(tls_env, tmp_path, bootstrap=True)

    server._resolve_tls(None, None, 1515)

    assert any("CREMIND_SSL=after-setup" in m and "cremind tls trust" in m for m in said)


def test_an_explicit_pair_wins_over_after_setup(tls_env, pair, tmp_path) -> None:
    cert, key = pair
    _after_setup(tls_env, tmp_path, bootstrap=False)
    assert server._resolve_tls(cert, key, 1515) == (cert, key)
    assert not (tmp_path / "tls" / "ca.pem").exists(), "nothing should be generated"


def test_after_setup_generates_nothing_when_tls_can_never_happen(
    tls_env, tmp_path
) -> None:
    """No public bind means no TLS ever — so no CA, and nothing pending."""
    _warnings(tls_env)
    _after_setup(tls_env, tmp_path, bootstrap=False)

    assert server._resolve_tls(None, None, 0) is None
    assert not (tmp_path / "tls").exists()


def test_after_setup_generates_nothing_under_electron(tls_env, tmp_path) -> None:
    _warnings(tls_env)
    _after_setup(tls_env, tmp_path, bootstrap=False)
    tls_env.setenv("CREMIND_ELECTRON_PARENT", "1")

    assert server._resolve_tls(None, None, 1515) is None
    assert not (tmp_path / "tls").exists()


def test_an_unknown_mode_warns_instead_of_silently_serving_http(tls_env) -> None:
    """A typo in something the operator deliberately set should say so."""
    said = _warnings(tls_env)
    tls_env.setattr(BaseConfig, "SSL_MODE", "aftersetup", raising=False)

    assert server._resolve_tls(None, None, 1515) is None

    message = next(m for m in said if "aftersetup" in m)
    assert "after-setup" in message and "auto" in message


def test_the_mode_is_read_case_insensitively(tls_env, tmp_path) -> None:
    tls_env.setattr(BaseConfig, "SSL_MODE", "After-Setup", raising=False)
    tls_env.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    tls_env.setattr(server, "bootstrap_exists", lambda: True)

    assert server._resolve_tls(None, None, 1515) is not None


def test_supervised_environments(monkeypatch) -> None:
    """Kubernetes counts: the wizard now asks for a restart deliberately, and a
    wedged shutdown there stops the kubelet bringing the pod back at all."""
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.delenv("CREMIND_SUPERVISED", raising=False)
    for mode, expected in (
        ("docker", True),
        ("kubernetes", True),
        ("native", False),
        ("", False),
    ):
        monkeypatch.setenv("INSTALL_MODE", mode)
        assert server._supervised_env() is expected, mode


def test_a_boot_service_counts_as_a_supervisor(monkeypatch) -> None:
    """A native install with `cremind boot enable` is supervised too.

    The unit sets CREMIND_SUPERVISED, so a hung lifespan shutdown would stop
    it bringing the server back — exactly the case the bounded shutdown
    exists for.
    """
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.setenv("INSTALL_MODE", "native")
    monkeypatch.setenv("CREMIND_SUPERVISED", "1")

    assert server._supervised_env() is True


def test_the_server_pid_file_is_only_written_when_supervised(
    monkeypatch, tmp_path
) -> None:
    """A hand-run `cremind serve` must leave no pid file behind.

    `cremind boot disable` and the uninstallers stop whatever PID sits in
    server.pid; a developer's terminal server must never be that PID.
    """
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.delenv("CREMIND_SUPERVISED", raising=False)

    server._write_server_pid_if_supervised()
    assert not (tmp_path / "server.pid").exists()

    monkeypatch.setenv("CREMIND_SUPERVISED", "1")
    server._write_server_pid_if_supervised()
    assert (tmp_path / "server.pid").read_text().strip() == str(os.getpid())


def test_the_hypercorn_config_asks_for_http2(pair) -> None:
    """ALPN advertising h2 is the entire reason the TLS path uses hypercorn:
    it is how a browser escapes the ~6-connection-per-origin cap. http/1.1
    stays listed so non-h2 clients and the WebSocket upgrade still work."""
    cert, key = pair
    cfg = server._mk_hypercorn_config("0.0.0.0", 1515, cert, key)
    assert cfg.alpn_protocols == ["h2", "http/1.1"]
    assert cfg.bind == ["0.0.0.0:1515"]
    assert cfg.certfile == cert
    assert cfg.keyfile == key
    # Parity with the uvicorn binds, and inside _BoundedShutdownServer's 12s
    # hard-exit deadline so the clean path normally wins.
    assert cfg.graceful_timeout == 10.0


def test_a_key_passphrase_reaches_hypercorn(tls_env, pair) -> None:
    cert, key = pair
    tls_env.setattr(BaseConfig, "SSL_KEYFILE_PASSWORD", "s3cret", raising=False)
    assert server._mk_hypercorn_config("0.0.0.0", 1515, cert, key).keyfile_password == "s3cret"
