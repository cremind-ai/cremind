"""`cremind tags devices …` where a tag's parent can be the gateway itself.

A newer gateway reaches tags on its own radio, so a tag connects through the
gateway or through a bridge. Every wrapper in ``app.cli.client.tags_setup`` is
replaced by a recording fake, so nothing reaches the network. What is pinned:
`add` names each device that heard a tag (gateway or bridge) and says where the
tag connects; `move` takes the tag's own gateway (`--to gateway`) or a bridge of
that gateway, never another connection's; `remove` of a bridge says where its
tags can go; `connect` prints the next step the gateway calls for; `list` shows
what a gateway reaches and where a tag connects; the setup code never shows.
"""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

CODE = "4D6KR-ART00-0G40R-40M30-E2097"
GW_ROW, BR_ROW, TAG_ROW, OTHER_BR = "gw-row", "br-row", "tag-row", "br-other"


def _connection(serves: bool = True) -> dict:
    return {
        "id": "comp-1", "name": "Office gateway", "status": "connected", "paused": False, "host_id": "h-srv",
        "gateway": {"id": GW_ROW, "kind": "gateway", "name": "Office gateway", "device_id": "ab" * 16,
                    "short_id": "9C8B7A65", "state": "ready", "paused": False, "serves_tags": serves,
                    "capacity": {"max_tags": 20, "assigned": 1} if serves else None},
        "bridges": [{"id": BR_ROW, "kind": "bridge", "name": "Hall", "device_id": "cd" * 16, "short_id": "11112222",
                     "state": "ready", "paused": False, "capacity": {"max_tags": 20, "assigned": 0}}],
        "tags": [{"id": TAG_ROW, "kind": "tag", "name": "Kitchen", "device_id": "ef" * 16, "short_id": "33334444",
                  "state": "ready", "paused": False, "bridge_id": GW_ROW, "delivery": {"pending_count": 2}}],
    }


def _elsewhere() -> dict:
    """Another gateway of the same profile, with a bridge of its own."""
    return {"id": "comp-2", "name": "Garage gateway", "status": "connected", "paused": False, "host_id": "h-srv",
            "gateway": {"id": "gw-2", "kind": "gateway", "name": "Garage gateway", "device_id": "12" * 16,
                        "short_id": "55556666", "state": "ready", "paused": False, "serves_tags": False,
                        "capacity": None},
            "bridges": [{"id": OTHER_BR, "kind": "bridge", "name": "Porch", "device_id": "34" * 16,
                         "short_id": "77778888", "state": "ready", "paused": False, "capacity": None}],
            "tags": []}


def _candidate(cid: str, kind: str, parent: str, name: str, rssi: int, eligible: bool = True) -> dict:
    return {"id": cid, "gateway_id": "comp-1", "bridge_id": parent, "bridge_kind": kind, "bridge_name": name,
            "rssi": rssi, "seen_at": "x", "capacity": None, "eligible": eligible,
            "reason": None if eligible else "bridge_full"}


@pytest.fixture
def api(monkeypatch):
    """The setup client's wrappers as recording fakes."""
    import app.cli.client.tags_setup as c

    state: dict = {"calls": [], "connections": [_connection(), _elsewhere()], "candidates": [], "recommended": None,
                   "unpair": {"operation": {"id": "op-9", "kind": "unpair"}, "device": {"affected_tag_ids": None}},
                   "ops": {"scan-1": {"id": "scan-1", "kind": "host_scan", "state": "succeeded", "ports": [],
                                      "candidates": [{"id": "hc_1", "device_id": "ab" * 16, "short_id": "9C8B7A65",
                                                      "fw": "0.3.0", "proto": 2, "board": 19, "state": "usable",
                                                      "message": "", "companion_id": None, "expires_at": "z"}]},
                           "conn-1": {"id": "conn-1", "kind": "host_connect", "state": "succeeded",
                                      "companion_id": "comp-1", "host_id": "h-srv"}}}
    hosts = [{"id": "h-srv", "kind": "server", "name": "Office PC", "platform": "linux", "online": True,
              "state": "running", "reason": None, "usb": {"available": True, "container": False, "reason": None},
              "readiness": {"state": "ready", "components": []}, "gateways": [],
              "access": {"can_use": True, "reason": None, "can_manage": True}, "connections": 1}]

    def record(name):
        async def fake(client, *args, **kwargs):
            state["calls"].append((name, args, kwargs))
            if name == "connections":
                return {"simple_setup": True, "connections": copy.deepcopy(state["connections"])}
            if name == "start_discovery":
                return {"discovery": {"id": "dis-1", "role": args[0], "state": "scanning", "candidates": []}}
            if name == "get_discovery":
                return {"discovery": {"id": "dis-1", "role": "tag", "state": "found",
                                      "candidates": copy.deepcopy(state["candidates"]),
                                      "recommended": state["recommended"]}}
            if name == "start_pairing":
                return {"pairing": {"id": "op-1", "state": "queued"}}
            if name == "get_pairing":
                return {"pairing": {"id": "op-1", "state": "succeeded", "stage": "done", "stage_detail": None}}
            if name == "move":
                return {"operation": {"id": "op-2", "kind": "move_tag", "state": "queued"}}
            if name == "unpair":
                return copy.deepcopy(state["unpair"])
            if name == "hosts":
                return {"hosts": copy.deepcopy(hosts), "active": []}
            if name in ("scan_host", "connect_gateway"):
                return {"operation": {"id": "scan-1" if name == "scan_host" else "conn-1", "state": "queued"}}
            if name == "get_operation":
                return {"operation": copy.deepcopy(state["ops"][args[0]])}
            raise AssertionError(f"unexpected call {name}")
        return fake

    for name in ("connections", "start_discovery", "get_discovery", "start_pairing", "get_pairing", "move", "unpair",
                 "hosts", "scan_host", "connect_gateway", "get_operation"):
        monkeypatch.setattr(c, name, record(name))
    import app.cli.commands.tags  # noqa: F401 - the parent group first: it registers its sub-apps
    import app.cli.commands.tags_devices as devices_cmd
    import app.cli.commands.tags_hosts as hosts_cmd

    monkeypatch.setattr(devices_cmd, "_POLL_S", 0)
    monkeypatch.setattr(hosts_cmd, "POLL_S", 0)
    return state


def _run(*args, input: str | None = None):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args], input=input)


def _calls(state, name):
    return [(a, k) for n, a, k in state["calls"] if n == name]


def _add_tag(*extra: str):
    return _run("tags", "devices", "add", "tag", "--code-file", "-", *extra, input=CODE + "\n")


def test_add_tag_names_each_device_that_hears_it_and_where_it_connects(api):
    api["candidates"] = [_candidate("c1", "gateway", GW_ROW, "Office gateway", -55),
                         _candidate("c2", "bridge", BR_ROW, "Hall", -70)]
    api["recommended"] = "c1"
    result = _add_tag()
    assert result.exit_code == 1
    assert "several devices hear it (recommended: c1); choose with --candidate:" in result.output
    assert "c1  gateway 'Office gateway'  rssi -55" in result.output and "c2  bridge 'Hall'  rssi -70" in result.output
    assert _calls(api, "start_pairing") == []

    result = _add_tag("--candidate", "c1", "--name", "Desk")
    assert result.exit_code == 0, result.output
    assert "Tag ready. It connects through gateway 'Office gateway'." in result.output
    assert _calls(api, "start_pairing") == [(("dis-1", "c1", "Desk"), {})]
    assert CODE not in result.output  # a setup code is never printed back

    # The only device with room is taken at once; none with room says why.
    api["candidates"] = [_candidate("c1", "gateway", GW_ROW, "Office gateway", -45, eligible=False),
                         _candidate("c2", "bridge", BR_ROW, "Hall", -70)]
    result = _add_tag()
    assert result.exit_code == 0 and "It connects through bridge 'Hall'." in result.output
    api["candidates"] = [_candidate("c1", "gateway", GW_ROW, "Office gateway", -45, eligible=False)]
    result = _add_tag()
    assert result.exit_code == 1
    assert "found it, but nothing that hears it can take it (bridge_full)." in result.output
    assert "--to <gateway or bridge>" in result.output


def test_move_takes_the_tags_gateway_or_a_bridge_of_that_gateway(api):
    assert _run("tags", "devices", "move", "Kitchen", "--to", "gateway").exit_code == 0
    assert _run("tags", "devices", "move", "Kitchen", "--bridge", "Hall").exit_code == 0
    assert _run("tags", "devices", "move", "33334444", "--to", "Office gateway").exit_code == 0
    assert _calls(api, "move") == [((TAG_ROW, GW_ROW), {}), ((TAG_ROW, BR_ROW), {}), ((TAG_ROW, GW_ROW), {})]
    # Never another gateway's bridge.
    result = _run("tags", "devices", "move", "Kitchen", "--to", "Porch")
    assert result.exit_code == 1
    assert "no gateway or bridge of the tag's connection matches 'Porch'" in result.output
    assert len(_calls(api, "move")) == 3


def test_removing_a_bridge_says_where_its_tags_can_go(api):
    api["unpair"] = {"operation": {"id": "op-9", "kind": "unpair"},
                     "device": {"kind": "bridge", "affected_tag_ids": [TAG_ROW]}}
    result = _run("tags", "devices", "remove", "Hall", "--yes")
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "1 tag(s) used this bridge; move them to your gateway (if it reaches tags itself) or another bridge" in text
    assert "cremind tags devices move <tag> --to <gateway or bridge>" in text
    assert _calls(api, "unpair") == [((BR_ROW,), {"force": False})]


def test_list_shows_what_a_gateway_reaches_and_where_a_tag_connects(api):
    result = _run("tags", "devices", "list")
    assert result.exit_code == 0, result.output
    rows = {line.split("\t")[0]: line.split("\t") for line in result.output.splitlines()}
    assert rows[GW_ROW][-1] == "1/20 tags directly"
    assert rows[BR_ROW][-1] == "0/20 tags"
    assert rows[TAG_ROW][-1] == "2 pending, via Office gateway"
    assert rows["gw-2"][-1] == "tags need a bridge"


def test_connect_prints_the_next_step_the_gateway_calls_for(api):
    result = _run("tags", "devices", "connect", "--yes")
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "Gateway connected through Office PC. It reaches the tags near it itself." in text
    assert "Next: cremind tags devices add tag --code-file <label.txt> (add a bridge only to reach tags farther away)" \
        in text
    api["connections"] = [_connection(serves=False)]
    result = _run("tags", "devices", "connect", "--yes")
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "It does not report that it reaches tags itself, so tags need a bridge." in text
    assert "Next: cremind tags devices add bridge --code-file <label.txt>" in text
