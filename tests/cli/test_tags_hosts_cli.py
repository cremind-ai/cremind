"""`cremind tags hosts …` and the gateway-computer flows of `cremind tags devices`.

Every wrapper in ``app.cli.client.tags_setup`` is replaced by a recording fake,
so nothing reaches the network. What is pinned: a search runs on the computer
named (or the only one there is — never silently on another), a search that
finds nothing says why in the Settings page's words, `connect` sends only an
opaque candidate id (never a port), `recover` moves the connection whose
gateway the search found, and the admin's access switch names a profile id.
"""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

GW = "ab" * 16


def _host(**kw) -> dict:
    return {
        "id": "h-srv", "kind": "server", "name": "Office PC", "platform": "linux", "version": "0.0.19",
        "online": True, "last_seen_at": None, "state": "running", "reason": None,
        "readiness": {"state": "ready", "components": [{"key": "packages", "state": "ready", "detail": ""}]},
        "usb": {"available": True, "container": False, "reason": None}, "gateways": [],
        "access": {"can_use": True, "reason": None, "can_manage": True,
                   "profiles": [{"profile": "bob", "profile_id": "pid-bob", "granted": False, "granted_at": None}]},
        "connections": 0, **kw,
    }


def _op(kind: str, state: str, **kw) -> dict:
    return {"id": "scan-1" if kind == "host_scan" else "conn-1", "kind": kind, "state": state, "stage": "done",
            "stage_detail": None, "host_id": "h-srv", "error": None, "created_at": "x", "updated_at": "y",
            "expires_at": "z", **kw}


def _candidate(state: str = "usable", **kw) -> dict:
    return {"id": "hc_1", "device_id": GW, "short_id": "9C8B7A65", "fw": "0.2.0", "proto": 2, "board": 19,
            "state": state, "message": "Ready to connect.", "companion_id": None, "expires_at": "z", **kw}


def _connection() -> dict:
    return {"id": "comp-1", "name": "Hall gateway", "status": "offline", "paused": False, "host_id": "h-old",
            "gateway": {"id": "d1", "kind": "gateway", "name": "Hall gateway", "device_id": GW, "short_id": "9C8B7A65"},
            "bridges": [], "tags": []}


@pytest.fixture
def api(monkeypatch):
    """The setup client's wrappers as recording fakes; ``state["ops"]`` answers get_operation in turn."""
    import app.cli.client.tags_setup as c

    state: dict = {"calls": [], "hosts": [_host()], "ops": {}, "connections": [_connection()]}

    def record(name):
        async def fake(client, *args, **kwargs):
            state["calls"].append((name, args, kwargs))
            if name == "hosts":
                return {"hosts": copy.deepcopy(state["hosts"]), "active": []}
            if name == "scan_host":
                return {"operation": _op("host_scan", "queued")}
            if name == "connect_gateway":
                return {"operation": _op("host_connect", "queued")}
            if name == "get_operation":
                queue = state["ops"][args[0]]
                return {"operation": copy.deepcopy(queue.pop(0) if len(queue) > 1 else queue[0])}
            if name == "set_host_access":
                return {"host_id": args[0], "profile_id": args[1], "profile": "bob", "granted": args[2]}
            if name == "connections":
                return {"simple_setup": True, "connections": copy.deepcopy(state["connections"])}
            raise AssertionError(f"unexpected call {name}")
        return fake

    for name in ("hosts", "scan_host", "connect_gateway", "get_operation", "set_host_access", "connections"):
        monkeypatch.setattr(c, name, record(name))
    import app.cli.commands.tags  # noqa: F401 - the parent group first: it registers its sub-apps
    import app.cli.commands.tags_hosts as hosts_cmd

    monkeypatch.setattr(hosts_cmd, "POLL_S", 0)
    return state


def _run(*args, input: str | None = None):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args], input=input)


def _calls(state, name):
    return [(a, k) for n, a, k in state["calls"] if n == name]


def test_list_shows_each_computer_and_why_one_cannot_search(api):
    api["hosts"] = [_host(), _host(id="h-lap", kind="desktop", name="Laptop", online=False,
                                   access={"can_use": True, "reason": None, "can_manage": False})]
    result = _run("tags", "hosts", "list")
    assert result.exit_code == 0, result.output
    assert "Office PC" in result.output and "Laptop" in result.output and "offline" in result.output
    assert "Laptop: Computer offline: Laptop is not reachable right now." in " ".join(result.output.split())


def test_a_font_update_is_a_hint_never_a_problem(api):
    outdated = {"state": "ready", "fonts_update": True, "components": [
        {"key": "fonts", "state": "outdated", "detail": "Tag screens use font pack a1a1a1a1a1a1a1a1; this Cremind "
                                                        "draws with b2b2b2b2b2b2b2b2."}]}
    api["hosts"] = [_host(readiness=outdated),
                    _host(id="h-lap", kind="desktop", name="Laptop", readiness=outdated,
                          access={"can_use": True, "reason": None, "can_manage": False})]
    result = _run("tags", "hosts", "list")
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert ('Office PC: a font update is available (tag screens keep working meanwhile): '
            'cremind -p admin tags hosts prepare "Office PC"') in text
    assert ("Laptop: a font update is available (tag screens keep working meanwhile): run cremind tags host "
            "prepare on Laptop, then restart Cremind there") in text
    result = _run("tags", "hosts", "show", "Office PC")
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "fonts: outdated — Tag screens use font pack a1a1a1a1a1a1a1a1" in text
    assert "A font update is available" in text and "cremind -p admin tags hosts prepare" in text
    # The computer still searches.
    api["ops"] = {"scan-1": [_op("host_scan", "succeeded", candidates=[_candidate()], ports=[])]}
    assert _run("tags", "hosts", "scan", "Office PC").exit_code == 0
    api["hosts"] = [_host()]
    assert "font update" not in _run("tags", "hosts", "list").output


def test_show_tells_what_moved_in_from_cremind_connect(api):
    api["hosts"] = [_host(migration={"moved": 2, "failed": 1, "rolled_back": 0})]
    result = _run("tags", "hosts", "show", "Office PC")
    assert result.exit_code == 0, result.output
    assert "Moved in: 2 gateway(s) from Cremind Connect, 1 to retry at the next start" in " ".join(result.output.split())
    api["hosts"] = [_host(migration={})]
    assert "Cremind Connect" not in _run("tags", "hosts", "show", "Office PC").output


def test_connect_searches_the_only_computer_and_connects_what_it_found(api):
    api["ops"] = {
        "scan-1": [_op("host_scan", "succeeded", candidates=[_candidate()], ports=[])],
        "conn-1": [_op("host_connect", "running", stage_detail="Checking the gateway"),
                   _op("host_connect", "running", stage_detail="Connecting to the gateway…", companion_id="comp-1"),
                   _op("host_connect", "succeeded", companion_id="comp-1")],
    }
    result = _run("tags", "devices", "connect", "--yes", "--name", "Hall gateway")
    assert result.exit_code == 0, result.output
    assert "Searching Office PC's USB ports…" in result.output
    assert "Checking the gateway…" in result.output and "Connecting to the gateway…" in result.output
    assert "Gateway connected through Office PC." in result.output
    assert _calls(api, "scan_host") == [(("h-srv",), {})]
    assert _calls(api, "connect_gateway") == [(("h-srv", "hc_1"), {"name": "Hall gateway"})]


def test_a_search_that_finds_nothing_says_why_and_connects_nothing(api):
    api["ops"] = {"scan-1": [_op("host_scan", "succeeded", candidates=[],
                                 ports=[{"reason": "no_access", "detail": "permission denied"}])]}
    result = _run("tags", "devices", "connect", "--yes")
    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "USB access denied" in text and "dialout" in text
    assert _calls(api, "connect_gateway") == []
    api["ops"] = {"scan-1": [_op("host_scan", "succeeded", candidates=[], ports=[])]}
    result = _run("tags", "hosts", "scan")
    assert result.exit_code == 1 and "No gateway detected" in result.output


def test_with_several_computers_nothing_is_searched_until_one_is_named(api):
    api["hosts"] = [_host(), _host(id="h-lap", kind="desktop", name="Laptop")]
    result = _run("tags", "devices", "connect", "--yes")
    assert result.exit_code == 1 and "--host" in result.output and "Laptop" in result.output
    assert _calls(api, "scan_host") == []
    api["ops"] = {"scan-1": [_op("host_scan", "succeeded", candidates=[_candidate()], ports=[])],
                  "conn-1": [_op("host_connect", "succeeded", companion_id="comp-1")]}
    result = _run("tags", "devices", "connect", "--host", "Laptop", "--yes")
    assert result.exit_code == 0, result.output
    assert _calls(api, "scan_host") == [(("h-lap",), {})]


def test_several_free_gateways_need_a_candidate(api):
    api["ops"] = {"scan-1": [_op("host_scan", "succeeded", ports=[], candidates=[
        _candidate(), _candidate(id="hc_2", device_id="cd" * 16, short_id="11112222")])]}
    result = _run("tags", "devices", "connect", "--yes")
    assert result.exit_code == 1 and "--candidate" in result.output and "hc_2" in result.output
    assert _calls(api, "connect_gateway") == []


def test_a_gateway_owned_elsewhere_is_explained(api):
    api["ops"] = {"scan-1": [_op("host_scan", "succeeded", ports=[], candidates=[_candidate("owned_elsewhere")])]}
    result = _run("tags", "devices", "connect", "--yes")
    assert result.exit_code == 1 and "Gateway owned elsewhere" in result.output


def test_recover_moves_the_connection_whose_gateway_the_search_found(api):
    api["ops"] = {
        "scan-1": [_op("host_scan", "succeeded", ports=[], candidates=[
            _candidate("recovery_required", companion_id="comp-1")])],
        "conn-1": [_op("host_connect", "succeeded", companion_id="comp-1", recover=True, recovery_id="rec-9")],
    }
    result = _run("tags", "devices", "recover", "Hall gateway", "--yes")
    assert result.exit_code == 0, result.output
    assert _calls(api, "connect_gateway") == [(("h-srv", "hc_1"), {"recover": True})]
    assert "now works through Office PC" in result.output and "rec-9" in result.output


def test_recover_says_when_the_gateway_is_not_plugged_in_there(api):
    api["ops"] = {"scan-1": [_op("host_scan", "succeeded", ports=[], candidates=[
        _candidate(device_id="ef" * 16)])]}
    result = _run("tags", "devices", "recover", "Hall gateway", "--yes")
    assert result.exit_code == 1 and "is not plugged into Office PC" in result.output
    assert _calls(api, "connect_gateway") == []


def test_access_names_the_profile_by_id(api):
    result = _run("tags", "hosts", "access", "Office PC", "bob", "--allow")
    assert result.exit_code == 0, result.output
    assert _calls(api, "set_host_access") == [(("h-srv", "pid-bob", True), {})]
    result = _run("tags", "hosts", "access", "Office PC", "bob")
    assert result.exit_code == 1 and "--allow or --deny" in result.output


def test_a_computer_that_cannot_search_is_not_asked_to(api):
    api["hosts"] = [_host(state="unavailable", reason="Gateway components are missing.")]
    result = _run("tags", "hosts", "scan")
    assert result.exit_code == 1 and "Required components unavailable" in result.output
    assert _calls(api, "scan_host") == []
