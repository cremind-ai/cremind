"""Resolving TLS config: enable it, refuse it clearly, or stay on plain HTTP.

The default — nothing set — has to keep returning ``None``, because that is
what leaves the dual-uvicorn plain-HTTP path exactly as it was.

Messages are captured off the loguru logger rather than through ``caplog``:
this project logs through loguru, which pytest's handler never sees (same
approach as tests/test_server_ports.py).
"""

from __future__ import annotations

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


def test_an_explicit_pair_wins_over_auto(tls_env, pair, tmp_path) -> None:
    """Someone who supplies a real certificate should get it, even with
    CREMIND_SSL=auto left set in the environment."""
    cert, key = pair
    tls_env.setattr(BaseConfig, "SSL_MODE", "auto", raising=False)
    tls_env.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    assert server._resolve_tls(cert, key, 1515) == (cert, key)
    assert not (tmp_path / "tls" / "ca.pem").exists(), "nothing should be generated"


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
