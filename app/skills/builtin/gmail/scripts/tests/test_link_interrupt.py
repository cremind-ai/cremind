"""Regression test: `link` must stay interruptible with Ctrl+C.

flow.run_local_server() blocks in wsgiref's handle_request(); on Windows that
WinSock wait swallows SIGINT until a request arrives, so Ctrl+C was a no-op and
`link` looked frozen. auth._run_local_server_interruptible runs the blocking call
on a daemon thread and parks the main thread in an interruptible join loop, so the
signal lands within ~0.5s and surfaces as a clean AuthError.

We simulate Ctrl+C with _thread.interrupt_main() (raises KeyboardInterrupt in the
main thread, exactly as SIGINT does).

Run standalone (no pytest needed):  python scripts/tests/test_link_interrupt.py
Or via pytest:                      pytest scripts/tests/test_link_interrupt.py
"""
import _thread
import sys
import threading
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app.google import auth


def test_success_path_returns_creds():
    class Flow:
        def run_local_server(self, **kwargs):
            return "CREDS"

    assert auth._run_local_server_interruptible(Flow()) == "CREDS"


def test_flow_error_is_reraised():
    class Flow:
        def run_local_server(self, **kwargs):
            raise ValueError("kaboom")

    raised = None
    try:
        auth._run_local_server_interruptible(Flow())
    except ValueError as e:
        raised = str(e)
    assert raised == "kaboom"


def test_keyboard_interrupt_becomes_autherror():
    """A Ctrl+C while the server is still waiting for the callback must abort with
    a clean AuthError instead of hanging or dumping a traceback."""
    started = threading.Event()
    release = threading.Event()  # set only during cleanup so the daemon worker exits

    class BlockingFlow:
        def run_local_server(self, **kwargs):
            started.set()
            release.wait()  # mimic handle_request() blocking until the callback
            return "CREDS"

    def interrupter():
        # Fire only once the worker is actually blocking, so the interrupt lands
        # inside the wrapper's join loop (where it is caught), not before it.
        started.wait(timeout=5)
        _thread.interrupt_main()

    threading.Thread(target=interrupter, daemon=True).start()
    outcome = None
    try:
        auth._run_local_server_interruptible(BlockingFlow())
    except auth.AuthError as e:
        outcome = ("AuthError", str(e))
    except KeyboardInterrupt:
        outcome = ("KeyboardInterrupt", "leaked uncaught")
    finally:
        release.set()

    assert outcome is not None, "interrupt was never delivered (server stayed blocked)"
    assert outcome[0] == "AuthError", f"expected clean AuthError, got {outcome}"
    assert "Ctrl+C" in outcome[1]


if __name__ == "__main__":
    test_success_path_returns_creds()
    test_flow_error_is_reraised()
    test_keyboard_interrupt_becomes_autherror()
    print("OK: link stays interruptible — Ctrl+C aborts cleanly with AuthError.")
