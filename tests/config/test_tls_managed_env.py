"""The HTTPS environment a container install carries in its own volume.

A container's environment is fixed at creation, so this file is the only place
an HTTPS switch can put settings that the *next boot* will read. Everything
here is about the two ways that could go wrong: not being read when it should
be, and being read when it should not.
"""
import pytest

from app.config import tls_managed_env as managed


@pytest.fixture
def volume(monkeypatch, tmp_path):
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(tmp_path))
    monkeypatch.setenv("INSTALL_MODE", "docker")
    for key in managed.MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "tls").mkdir(parents=True, exist_ok=True)
    return tmp_path


def write(volume, text: str, *, encoding: str = "utf-8") -> None:
    (volume / "tls" / "managed-env").write_text(text, encoding=encoding)


def test_the_key_allowlist_matches_the_one_the_switch_writes():
    """The duplication is deliberate — settings.py imports this module during
    its own import, so it cannot reach tls_transition. This is what stops the
    two drifting into a switch that writes keys the boot never applies."""
    from app.config.tls_transition import _NATIVE_ENV_KEYS

    assert set(managed.MANAGED_KEYS) == set(_NATIVE_ENV_KEYS)


def test_a_missing_or_unreadable_file_is_simply_nothing(volume):
    assert managed.read() == {}
    assert managed.load_into_environ() == {}


def test_values_are_applied_over_the_container_environment(volume, monkeypatch):
    """The whole point: the values being replaced are the ones baked into the
    container at creation, which is exactly what the switch is changing."""
    monkeypatch.setenv("CREMIND_SSL", "")
    monkeypatch.setenv("APP_URL", "http://localhost:1515")
    write(volume, "CREMIND_SSL=true\nAPP_URL=https://localhost:1515\n")

    applied = managed.load_into_environ()

    assert applied == {"CREMIND_SSL": "true", "APP_URL": "https://localhost:1515"}
    import os
    assert os.environ["CREMIND_SSL"] == "true"
    assert os.environ["APP_URL"] == "https://localhost:1515"


def test_only_the_keys_the_switch_owns_are_honoured(volume):
    write(volume, "APP_URL=https://a\nPATH=/evil\nDATABASE_URL=postgres://x\n")

    applied = managed.load_into_environ()

    assert applied == {"APP_URL": "https://a"}
    import os
    assert os.environ.get("PATH") != "/evil"
    assert "DATABASE_URL" not in os.environ


def test_comments_blank_lines_quotes_and_a_bom_are_all_tolerated(volume):
    write(volume, '﻿# written by Cremind\n\nAPP_URL="https://q"\n  CREMIND_SSL = true \n')

    assert managed.read() == {"APP_URL": "https://q", "CREMIND_SSL": "true"}


def test_the_file_retires_itself_once_the_deployment_agrees(volume, monkeypatch):
    """Once the container was recreated with the values baked in, the file has
    nothing left to say — and keeping it would silently outrank whatever the
    operator edits next."""
    monkeypatch.setenv("CREMIND_SSL", "auto")
    monkeypatch.setenv("APP_URL", "https://localhost:1515")
    write(volume, "CREMIND_SSL=auto\nAPP_URL=https://localhost:1515\n")

    assert managed.load_into_environ() == {}
    assert not (volume / "tls" / "managed-env").exists()


def test_a_partial_agreement_still_applies(volume, monkeypatch):
    monkeypatch.setenv("CREMIND_SSL", "auto")
    monkeypatch.setenv("APP_URL", "http://localhost:1515")  # stale
    write(volume, "CREMIND_SSL=auto\nAPP_URL=https://localhost:1515\n")

    assert managed.load_into_environ()["APP_URL"] == "https://localhost:1515"
    assert (volume / "tls" / "managed-env").exists()


@pytest.mark.parametrize("mode", ["native", "kubernetes", "", "electron"])
def test_no_other_deployment_reads_it(volume, monkeypatch, mode):
    """Kubernetes is excluded even though its PVC would hold the file: enabling
    HTTPS there moves the Service, the probes and the proxy sidecar together,
    so honouring this would let the pod believe in a switch it never made."""
    monkeypatch.setenv("INSTALL_MODE", mode)
    monkeypatch.setenv("APP_URL", "http://localhost:1515")
    write(volume, "APP_URL=https://localhost:1515\n")

    assert managed.load_into_environ() == {}
    import os
    assert os.environ["APP_URL"] == "http://localhost:1515"
    assert (volume / "tls" / "managed-env").exists()  # not retired either


def test_the_file_is_never_executed_as_shell(volume):
    """It is parsed, not sourced. A value is a value whatever it looks like."""
    write(volume, "APP_URL=https://$(whoami).example\n")

    assert managed.read()["APP_URL"] == "https://$(whoami).example"
