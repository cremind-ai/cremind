"""The runtime's hardware tables agree with the firmware's, as the pinned contract carries them.

``hardware/matrix.yaml`` names every board (short id, board id) and the panel
each tag board ships with; ``hardware/targets.yaml`` the SoCs firmware is
built for. The runtime keeps its own copies (enrollment and flashing need them
offline): a board, panel or SoC the firmware adds or renumbers fails here when
the new contract is pinned (scripts/tags/pin_contract.py).
"""

from __future__ import annotations

from app.tags.runtime.cli import firmware as fw
from app.tags.runtime.enroll import hardware
from app.tags.runtime.protocol import contract
from app.tags.runtime.protocol.ids import Board, Panel


def _boards() -> list[dict]:
    matrix = contract.pinned().hardware_table("matrix")
    return [entry for group in ("gateways", "bridges", "tags") for entry in matrix.get(group) or []]


def test_every_board_of_the_matrix_is_known_by_its_short_id_and_number() -> None:
    boards = _boards()
    assert boards, "the pinned contract carries hardware/matrix.yaml"
    for entry in boards:
        assert hardware.parse_board(entry["id"]) == Board(entry["board_id"]), entry["id"]
    development = {"nrf52dk_tag"}  # a DK standing in for a tag board: a target, not a matrix board
    assert set(hardware.BOARD_ALIASES) - {e["id"] for e in boards} == development


def test_tag_boards_ship_with_the_matrix_panel() -> None:
    tags = contract.pinned().hardware_table("matrix")["tags"]
    for entry in tags:
        panel = Panel(entry["panel"]["id"])
        assert hardware.BOARD_PANEL[Board(entry["board_id"])] == panel, entry["id"]
        profile = hardware.PANEL_PROFILES.get(panel)
        if profile is not None:  # UNVERIFIED panels have none: geometry must be given
            assert [profile.width, profile.height] == list(entry["panel"]["resolution"]), entry["id"]
            assert profile.planes == entry["panel"]["planes"], entry["id"]


def test_the_soc_table_matches_the_firmware_targets() -> None:
    targets = contract.pinned().hardware_table("targets")
    socs = targets["socs"]
    assert set(fw.SOC_DEVICES) == set(socs)
    for key, (_, family, flash) in fw.SOC_DEVICES.items():
        assert flash == socs[key]["flash"] and family == socs[key]["series"].upper(), key
    assert {t["soc"] for t in targets["targets"].values()} <= set(fw.SOC_DEVICES), "every target's SoC is known"
