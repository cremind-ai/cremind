"""The port pre-flight: fail fast, and say which port and what to do.

uvicorn binds its listening socket only AFTER the ASGI lifespan has run, so a
port already in use used to start the whole installation — watchers, every
channel adapter, the Node sidecars — before failing, then tear it all down
again, with the actual cause buried under the teardown's own warnings and a
``SystemExit`` raised inside a gathered task. These pin the check that moved
that decision to the top of ``main()``.

Real sockets rather than mocks: the question being asked is whether the OS will
give us the address, and that is exactly what the probe has to get right on
both Windows and Linux.
"""

from __future__ import annotations

import os
import socket

import pytest

from app import server


def _free_port() -> int:
    """A port nothing is listening on. Bound and released, so it is only
    free until something takes it — fine for a single assertion."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_free_port_reads_as_free() -> None:
    assert server._port_taken("127.0.0.1", _free_port()) is False


def test_a_listening_socket_reads_as_taken() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert server._port_taken("127.0.0.1", port) is True


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows SO_REUSEADDR means 'share a live listener', so the probe "
    "there stays strict and a closing socket does read as taken.",
)
def test_a_closing_socket_reads_as_free_because_the_real_bind_would_get_it() -> None:
    """The Kubernetes regression: a pod's network namespace outlives its
    container, so the ``CREMIND_SSL=after-setup`` restart always boots into the
    sockets the previous server left behind. A strict probe called those "in
    use" and refused to start — four times over, kubelet backing off further
    each time, until the remnants finally aged out. What the probe has to
    answer is whether the *server's* bind would succeed, so this pins both
    halves: the probe says free, and a bind shaped exactly like uvicorn's and
    hypercorn's then gets the port.

    The dying listener sets ``SO_REUSEADDR`` here because the real one does,
    and that is load-bearing rather than incidental: Linux excuses a closing
    socket only when *both* it and the newcomer carry the flag. Drop it from
    the setup below and the scenario stops being the one that happens in
    production — the remnant becomes genuinely unbindable and the test fails
    for a reason that has nothing to do with the probe.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", port))
            conn, _ = listener.accept()
            # The serving side closes first, which is what leaves *its* address
            # behind in TIME_WAIT rather than the client's.
            conn.close()

    assert server._port_taken("127.0.0.1", port) is False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as real:
        real.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        real.bind(("127.0.0.1", port))  # what uvicorn and hypercorn do, verbatim


def test_the_probe_leaves_the_port_free_for_the_real_bind() -> None:
    """The check must not become the thing that holds the port."""
    port = _free_port()
    assert server._port_taken("127.0.0.1", port) is False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as after:
        after.bind(("127.0.0.1", port))  # would raise if the probe lingered


def test_free_ports_start_the_server() -> None:
    """Both free — no exception, nothing to report."""
    server._require_free_ports("127.0.0.1", _free_port(), _free_port())


def _refusal(monkeypatch, *, public_port: int, loopback_port: int) -> str:
    """Run the check expecting it to stop, and return what it said.

    The message is captured off the logger rather than through ``caplog``:
    this project logs through loguru, which pytest's handler never sees.
    """
    said: list[str] = []
    monkeypatch.setattr(server.logger, "error", lambda msg: said.append(str(msg)))
    with pytest.raises(SystemExit) as exc:
        server._require_free_ports("127.0.0.1", public_port, loopback_port)
    assert exc.value.code == 1
    assert len(said) == 1, "one line, or the cause is buried again"
    return said[0]


def test_a_taken_public_port_names_the_dev_server_remedy(monkeypatch) -> None:
    """The common case by far: Vite already has :1515. The message has to say
    so, because the fix (CREMIND_UI_PORT=0) is not guessable from "address
    already in use"."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        message = _refusal(monkeypatch, public_port=taken, loopback_port=_free_port())
    assert f"127.0.0.1:{taken}" in message
    assert "CREMIND_UI_PORT=0" in message
    assert "Vite" in message


def test_a_taken_loopback_port_is_refused_too(monkeypatch) -> None:
    """The internal API port is only ever taken by a second Cremind, so it
    gets its own remedy — telling someone to set CREMIND_UI_PORT=0 here would
    send them after the wrong thing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        message = _refusal(monkeypatch, public_port=_free_port(), loopback_port=taken)
    assert f"127.0.0.1:{taken}" in message
    assert "PORT" in message
    assert "CREMIND_UI_PORT=0" not in message


def test_no_public_bind_checks_only_the_loopback_port() -> None:
    """``CREMIND_UI_PORT=0`` opens no public bind — that is how the dev loop
    frees :1515 for Vite — so a busy :1515 must not stop it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        # The public port is passed as 0; the busy one stands in for :1515.
        server._require_free_ports("127.0.0.1", 0, _free_port())


def test_the_public_port_reads_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("CREMIND_UI_PORT", "8080")
    assert server._resolve_public_port() == 8080
    # 0 is a value, not a failure: it means "no public bind at all".
    monkeypatch.setenv("CREMIND_UI_PORT", "0")
    assert server._resolve_public_port() == 0
    monkeypatch.delenv("CREMIND_UI_PORT")
    assert server._resolve_public_port() == 1515


def test_an_unparseable_port_falls_back_rather_than_crashing(monkeypatch) -> None:
    monkeypatch.setenv("CREMIND_UI_PORT", "not-a-port")
    assert server._resolve_public_port() == 1515
