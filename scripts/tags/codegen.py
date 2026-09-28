#!/usr/bin/env python3
"""Generate Cremind's Python protocol bindings from the pinned protocol contract.

The wire protocol is defined in the cremind-tag firmware repository
(``protocol/spec.yaml``). Cremind pins an immutable contract artifact built
from it (``app/tags/runtime/protocol/pinned/``: ``contract.json``, the spec,
the golden fixtures, the normative docs) and generates its Python bindings
from that snapshot only:

- app/tags/runtime/protocol/ids.py   identifiers and constants
- app/tags/runtime/protocol/msgs.py  mesh message structs with little-endian pack/unpack

(The firmware repository generates the C headers from the same spec with its
own tools/codegen.py; the two generators share this spec loader and checks.)

Usage::

    .venv/Scripts/python.exe scripts/tags/codegen.py          # regenerate
    .venv/Scripts/python.exe scripts/tags/codegen.py --check  # CI: exit 1 if stale

Only PyYAML is required. Before generating, the spec is checked for internal
consistency (unique ids, id ranges, derived limits); a violation aborts.
Re-pin a new contract with scripts/tags/pin_contract.py, which runs this.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "app" / "tags" / "runtime" / "protocol"
SPEC_PATH = PROTOCOL / "pinned" / "spec.yaml"
OUT_IDS_PY = PROTOCOL / "ids.py"
OUT_MSGS_PY = PROTOCOL / "msgs.py"


class SpecError(Exception):
    """The spec is malformed or internally inconsistent."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class HexInt(int):
    """An int written in hex in the spec; emitted in hex again with as many digits."""

    digits: int


def _construct_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    value = loader.construct_yaml_int(node)
    literal = node.value.lower().lstrip("+-")
    if not literal.startswith("0x"):
        return value
    hex_value = HexInt(value)
    hex_value.digits = len(literal) - 2
    return hex_value


class _SpecLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    # YAML keeps the LAST of two equal keys silently; a duplicated name in the
    # spec (a CBOR key, a message) would renumber the first one. Refuse it.
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise SpecError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    loader.flatten_mapping(node)
    return dict(loader.construct_pairs(node, deep=True))


_SpecLoader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_SpecLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_spec(text: str) -> dict[str, Any]:
    spec = yaml.load(text, Loader=_SpecLoader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(spec, dict):
        raise SpecError("spec root must be a mapping")
    return spec


def spec_text() -> str:
    return SPEC_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

INT_TYPES: dict[str, tuple[int, bool]] = {
    "u8": (1, False), "u16": (2, False), "u32": (4, False), "u64": (8, False),
    "i8": (1, True), "i16": (2, True), "i32": (4, True),
}
_FIXED_BYTES = re.compile(r"bytes\[(\d+)\]$")
_VARIABLE = re.compile(r"(glyph|str)\[(\w+)\]$")
_STRUCT_CODES = {"u8": "B", "u16": "H", "u32": "I", "u64": "Q", "i8": "b", "i16": "h", "i32": "i"}
_C_TYPES = {"u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
            "i8": "int8_t", "i16": "int16_t", "i32": "int32_t"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    doc: str
    max: str | None
    default: int | None = None

    @property
    def kind(self) -> str:
        if self.type in INT_TYPES:
            return "int"
        if _FIXED_BYTES.match(self.type):
            return "bytes_n"
        if self.type == "bytes":
            return "tail"
        if _VARIABLE.match(self.type):
            return "variable"
        raise SpecError(f"field {self.name}: unknown type {self.type!r}")

    @property
    def size(self) -> int:
        if self.kind == "int":
            return INT_TYPES[self.type][0]
        if self.kind == "bytes_n":
            match = _FIXED_BYTES.match(self.type)
            assert match
            return int(match.group(1))
        return 0


@dataclass(frozen=True)
class Message:
    title: str
    c_name: str
    py_name: str
    type_ref: tuple[str, str] | None
    fields: tuple[Field, ...]
    variable: Field | None

    @property
    def fixed(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.kind != "tail")

    @property
    def tail(self) -> Field | None:
        return next((f for f in self.fields if f.kind == "tail"), None)

    @property
    def fixed_len(self) -> int:
        return sum(f.size for f in self.fixed)

    @property
    def len_macro(self) -> str:
        return f"CTAG_{self.c_name.upper()}_LEN"


def _fields(raw: list[dict[str, Any]], where: str) -> tuple[tuple[Field, ...], Field | None]:
    fields = [Field(f["name"], str(f["type"]), str(f.get("doc", "")), f.get("max"), f.get("default"))
              for f in raw]
    for i, field in enumerate(fields):
        if field.kind in ("tail", "variable") and i != len(fields) - 1:
            raise SpecError(f"{where}.{field.name}: a variable field must be last")
        if field.kind == "tail" and not field.max:
            raise SpecError(f"{where}.{field.name}: a trailing bytes field needs max: <constant>")
        if field.default is not None:
            if field.kind != "int" or not isinstance(field.default, int):
                raise SpecError(f"{where}.{field.name}: only integer fields take an integer default")
            if any(f.default is None for f in fields[i + 1 :]):
                raise SpecError(f"{where}.{field.name}: fields after a default need one too")
    if fields and fields[-1].kind == "variable":
        return tuple(fields[:-1]), fields[-1]
    return tuple(fields), None


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.lower().split("_"))


def collect_messages(spec: dict[str, Any]) -> list[Message]:
    out: list[Message] = []

    def add(title: str, c_name: str, py_name: str, ref: tuple[str, str] | None,
            raw: list[dict[str, Any]]) -> None:
        fields, variable = _fields(raw or [], c_name)
        out.append(Message(title, c_name, py_name, ref, fields, variable))

    add("Serial frame header", "serial_header", "SerialHeader", None, spec["serial"]["header"])
    for name, op in spec["mesh"]["opcodes"].items():
        add(f"Mesh {name} parameters (opcode 0x{op['value']:02X}, handled by {op['model']})",
            f"mesh_{name.lower()}", f"Mesh{_camel(name)}", ("MeshOp", name), op["fields"])
    add("Layout header", "layout_header", "LayoutHeader", None, spec["layout"]["header"])
    for name, cmd in spec["layout"]["commands"].items():
        add(f"Layout command {name} (op 0x{cmd['value']:02X}), after the op byte",
            f"layout_cmd_{name.lower()}", f"LayoutCmd{_camel(name)}", ("LayoutCmd", name),
            cmd["fields"])
    add("One GLYPHS entry", "layout_glyph", "LayoutGlyph", None, spec["layout"]["glyph"])
    add("Tag CAPS characteristic value", "tag_caps", "TagCaps", None, spec["gatt"]["tag_caps"])
    for name, msg in spec["gatt"]["ctrl_messages"].items():
        add(f"CTRL {name} (type 0x{msg['value']:02X}, {msg['dir']}), after the type byte",
            f"ctrl_{name.lower()}", f"Ctrl{_camel(name)}", ("CtrlMsg", name), msg["fields"])
    for name, msg in spec["gatt"]["record_types"].items():
        add(f"Record {name} plaintext (type 0x{msg['value']:02X}, {msg['dir']})",
            f"rec_{name.lower()}", f"Rec{_camel(name)}", ("RecordType", name), msg["fields"])
    for name, msg in spec["gatt"]["plain_messages"].items():
        add(f"STATUS plain {name} (type 0x{msg['value']:02X}, {msg['dir']}), after the type byte",
            f"plain_{name.lower()}", f"Plain{_camel(name)}", ("PlainMsg", name), msg["fields"])
    add("Enrollment blob (UICR.CUSTOMER)", "enrollment", "Enrollment", None,
        spec["enrollment"]["fields"])
    v2 = spec["v2"]
    add("v2 IDENT characteristic value / tunnel OPEN data", "ident2", "Ident2", None, v2["ident2"])
    add("v2 enrollment blob (UICR.CUSTOMER[0..5])", "enrollment2", "Enrollment2", None, v2["enrollment2"])
    add("v2 identity copy (UICR.CUSTOMER at uicr_identity_offset)", "uicr_identity", "UicrIdentity", None,
        v2["uicr_identity"])
    return out


# ---------------------------------------------------------------------------
# Consistency checks
# ---------------------------------------------------------------------------


def _values(group: dict[str, Any], key: str = "value") -> dict[str, int]:
    return {name: (v[key] if isinstance(v, dict) else v) for name, v in group.items()}


def check_spec(spec: dict[str, Any], messages: list[Message]) -> None:
    errors: list[str] = []
    const = _values(spec["constants"])
    by_name = {m.c_name: m for m in messages}

    def unique(label: str, values: dict[str, int]) -> None:
        seen: dict[int, str] = {}
        for name, value in values.items():
            if value in seen:
                errors.append(f"{label}: {name} and {seen[value]} share value {value}")
            seen[value] = name

    for label, group, key in [
        ("status_codes", spec["status_codes"], "value"),
        ("delivery_stages", spec["delivery_stages"], "value"),
        ("delivery_outcomes", spec["delivery_outcomes"], "value"),
        ("serial.message_types", spec["serial"]["message_types"], "value"),
        ("serial.cbor_keys", spec["serial"]["cbor_keys"], "value"),
        ("tag_commands", spec["tag_commands"], "value"),
        ("node_roles", spec["node_roles"], "value"),
        ("mesh.models", spec["mesh"]["models"], "id"),
        ("mesh.opcodes", spec["mesh"]["opcodes"], "value"),
        ("layout.commands", spec["layout"]["commands"], "value"),
        ("boards", spec["boards"], "value"),
        ("panels", spec["panels"], "value"),
        ("gatt.characteristics", spec["gatt"]["characteristics"], "short"),
        ("v2.grant_ops", spec["v2"]["grant_ops"], "value"),
        ("v2.owner_states", spec["v2"]["owner_states"], "value"),
        ("v2.links", spec["v2"]["links"], "value"),
        ("v2.tunnel_states", spec["v2"]["tunnel_states"], "value"),
        ("v2.pair_kinds", spec["v2"]["pair_kinds"], "value"),
        ("v2.adv_flags", spec["v2"]["adv_flags"], "value"),
    ]:
        unique(label, _values(group, key))
    unique("gatt message types", {
        **_values(spec["gatt"]["ctrl_messages"]), **_values(spec["gatt"]["record_types"]),
        **_values(spec["gatt"]["plain_messages"]),
    })
    unique("icons", {i["name"]: i["id"] for i in spec["icons"]})

    def in_range(label: str, values: dict[str, int], lo: int, hi: int) -> None:
        for name, value in values.items():
            if not lo <= value <= hi:
                errors.append(f"{label}.{name} = {value:#x} outside {lo:#x}..{hi:#x}")

    in_range("mesh.opcodes", _values(spec["mesh"]["opcodes"]), 0x00, 0x3F)
    in_range("gatt.ctrl_messages", _values(spec["gatt"]["ctrl_messages"]), 0x01, 0x0F)
    in_range("gatt.record_types", _values(spec["gatt"]["record_types"]), 0x10, 0x2F)
    in_range("gatt.plain_messages", _values(spec["gatt"]["plain_messages"]), 0xC0, 0xCF)

    for name in [f.max for m in messages for f in m.fields if f.max]:
        if name not in const:
            errors.append(f"max: {name} is not a constant")

    def expect(label: str, actual: int, wanted: int) -> None:
        if actual != wanted:
            errors.append(f"{label} = {actual}, expected {wanted}")

    frame = const["SERIAL_MAX_FRAME"]
    expect("SERIAL_HEADER_LEN", const["SERIAL_HEADER_LEN"], by_name["serial_header"].fixed_len)
    expect("SERIAL_MAX_PAYLOAD", const["SERIAL_MAX_PAYLOAD"],
           frame - const["SERIAL_HEADER_LEN"] - const["SERIAL_CRC_LEN"])
    if const["SERIAL_MAX_ENCODED"] < frame + math.ceil(frame / 254) + 1:
        errors.append("SERIAL_MAX_ENCODED is below the COBS worst case")
    expect("LAYOUT_MAX_CHUNKS", const["LAYOUT_MAX_CHUNKS"],
           math.ceil(const["LAYOUT_HARD_MAX"] / const["LAYOUT_CHUNK_DATA_MAX"]))
    if const["LAYOUT_MAX_CHUNKS"] > 32:
        errors.append("LAYOUT_MAX_CHUNKS exceeds the 32-bit LAYOUT_STATUS.missing bitmap")
    expect("ATT_VALUE_MAX", const["ATT_VALUE_MAX"], const["ATT_MTU"] - 3)
    expect("FRAG_PAYLOAD_MAX", const["FRAG_PAYLOAD_MAX"], const["ATT_VALUE_MAX"] - 1)
    expect("TAG_PLANE_DATA_MAX", const["TAG_PLANE_DATA_MAX"],
           const["TAG_RECORD_PAYLOAD_MAX"] - by_name["rec_plane_data"].fixed_len)
    expect("TAG_RECORD_WIRE_MAX", const["TAG_RECORD_WIRE_MAX"],
           1 + 4 + const["TAG_RECORD_PAYLOAD_MAX"] + const["TAG_RECORD_MIC_LEN"])
    if const["TAG_RECORD_BUF"] < const["TAG_RECORD_WIRE_MAX"]:
        errors.append("TAG_RECORD_BUF is smaller than TAG_RECORD_WIRE_MAX")
    expect("SETUP_PAYLOAD_LEN", const["SETUP_PAYLOAD_LEN"], 1 + 4 + const["SETUP_SECRET_LEN"])
    expect("SETUP_CODE_LEN", const["SETUP_CODE_LEN"], math.ceil(const["SETUP_PAYLOAD_LEN"] * 8 / 5) + 1)
    if const["TUNNEL_MSG_MAX"] > const["SERIAL_MAX_PAYLOAD"] // 2:
        errors.append("TUNNEL_MSG_MAX must leave room in a serial frame")
    if const["PAIR_MSG_MAX"] > 2 * const["TAG_RECORD_BUF"]:
        errors.append("PAIR_MSG_MAX exceeds the tag's two record buffers")
    ident = by_name["ident2"]
    if 1 + ident.fixed_len > const["TUNNEL_MSG_MAX"]:
        errors.append("ident2 does not fit a tunnel message")
    offset = spec["v2"]["uicr_identity_offset"]
    if offset < by_name["enrollment2"].fixed_len or offset + by_name["uicr_identity"].fixed_len > 128:
        errors.append("uicr_identity_offset overlaps enrollment2 or leaves UICR.CUSTOMER")

    for m in messages:
        longest = m.fixed_len + (const[m.tail.max] if m.tail and m.tail.max else 0)
        if m.c_name.startswith("mesh_") and longest > const["MESH_MAX_VENDOR_PARAMS"]:
            errors.append(f"{m.c_name}: {longest} bytes exceed MESH_MAX_VENDOR_PARAMS")
        if m.c_name.startswith("ctrl_") and 1 + longest > const["TAG_CTRL_MSG_MAX"]:
            errors.append(f"{m.c_name}: {1 + longest} bytes exceed TAG_CTRL_MSG_MAX")
        if m.c_name.startswith("rec_") and longest > const["TAG_RECORD_PAYLOAD_MAX"]:
            errors.append(f"{m.c_name}: {longest} bytes exceed TAG_RECORD_PAYLOAD_MAX")
        if m.len_macro[len("CTAG_"):] in const and const[m.len_macro[len("CTAG_"):]] != m.fixed_len:
            errors.append(f"{m.len_macro} collides with a constant of a different value")
    if errors:
        raise SpecError("spec consistency:\n  " + "\n  ".join(errors))


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------

def fmt_int(value: int) -> str:
    if isinstance(value, HexInt):
        return f"0x{value:0{value.digits}X}"
    return str(value)


def gatt_uuid(base: str, short: int) -> uuid.UUID:
    raw = bytearray(uuid.UUID(base).bytes)
    raw[2:4] = short.to_bytes(2, "big")
    return uuid.UUID(bytes=bytes(raw))


def cbor_key_comments(text: str) -> dict[str, str]:
    block = text.split("  cbor_keys:", 1)[1].split("  message_types:", 1)[0]
    comments: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"\s+(\w+):\s*\d+\s*(?:#\s*(.*))?$", line)
        if match and match.group(2):
            comments[match.group(1)] = match.group(2).strip()
    return comments


def banner(sha: str) -> list[str]:
    return [
        "DO NOT EDIT: generated by scripts/tags/codegen.py from the pinned protocol contract",
        "(app/tags/runtime/protocol/pinned/spec.yaml, from the cremind-tag firmware repository).",
        f"spec sha256: {sha}",
    ]


# ---------------------------------------------------------------------------
# ids.py
# ---------------------------------------------------------------------------


def gen_ids_py(spec: dict[str, Any], sha: str, text: str) -> str:
    L: list[str] = ['"""Protocol identifiers and constants (mirror of include/ctag/proto_ids.h).', ""]
    L += banner(sha)
    L += ['"""', "", "from __future__ import annotations", "", "import uuid",
          "from enum import IntEnum, IntFlag", "from typing import Final", "",
          f"SPEC_VERSION: Final = {spec['spec_version']}", f'SPEC_SHA256: Final = "{sha}"', "",
          "# ---- Constants (spec: constants) ----"]
    for name, entry in spec["constants"].items():
        value = entry["value"]
        if isinstance(value, list):
            L.append(f"{name}: Final[tuple[int, ...]] = ({', '.join(str(v) for v in value)},)"
                     f"  # {entry['doc']}")
        else:
            L.append(f"{name}: Final = {fmt_int(value)}  # {entry['doc']}")
    L.append("")

    def enum(name: str, base: str, doc: str, members: list[tuple[str, int, str]]) -> None:
        L.extend(["", f"class {name}({base}):", f'    """{doc}"""', ""])
        for member, value, member_doc in members:
            comment = f"  # {member_doc}" if member_doc else ""
            L.append(f"    {member} = {fmt_int(value)}{comment}")
        L.append("")

    def items(group: dict[str, Any], key: str = "value") -> list[tuple[str, int, str]]:
        return [(n, v[key] if isinstance(v, dict) else v,
                 v.get("doc", "") if isinstance(v, dict) else "") for n, v in group.items()]

    enum("Status", "IntEnum", "Status codes shared by serial, mesh, GATT and receipts.",
         items(spec["status_codes"]))
    enum("DeliveryStage", "IntEnum", "Delivery stages.", items(spec["delivery_stages"]))
    enum("DeliveryOutcome", "IntEnum", "Terminal delivery outcomes.",
         items(spec["delivery_outcomes"]))
    enum("SerialFlag", "IntFlag", "Serial header flags bits.", items(spec["serial"]["flags"]))
    enum("SerialMsg", "IntEnum", "Serial message types.",
         [(n, v["value"], f"{v['dir']}: {v['doc']}") for n, v in spec["serial"]["message_types"].items()])
    comments = cbor_key_comments(text)
    enum("CborKey", "IntEnum", "Integer keys of serial CBOR maps (field name = member name, lower case).",
         [(n.upper(), v, comments.get(n, "")) for n, v in spec["serial"]["cbor_keys"].items()])
    enum("TagCommand", "IntEnum", "Tag commands.", items(spec["tag_commands"]))
    enum("NodeRole", "IntEnum", "Node roles.", items(spec["node_roles"]))
    enum("MeshModel", "IntEnum", "Vendor model ids (company MESH_COMPANY_ID).",
         [(n, v["id"], f"on {v['on']}: {v['doc']}") for n, v in spec["mesh"]["models"].items()])
    enum("MeshOp", "IntEnum", "6-bit vendor opcode numbers.",
         [(n, v["value"], f"handled by {v['model']}") for n, v in spec["mesh"]["opcodes"].items()])
    L += ["",
          "def mesh_vendor_opcode(op: int) -> int:",
          '    """3-octet opcode value as Zephyr\'s BT_MESH_MODEL_OP_3(op, MESH_COMPANY_ID)."""',
          "    return ((0xC0 | op) << 16) | MESH_COMPANY_ID", "", "",
          "def mesh_opcode_bytes(op: int) -> bytes:",
          '    """On-air opcode octets: 0xC0 | op, then the company id little-endian."""',
          '    return bytes([0xC0 | op]) + MESH_COMPANY_ID.to_bytes(2, "little")', "", ""]
    layout = spec["layout"]
    L.append(f"LAYOUT_MAGIC: Final = {fmt_int(layout['magic'])}  # bytes 'C','L'")
    L.append("")
    enum("Color", "IntEnum", "Layout colours.", items(layout["colors"]))
    enum("QrEcc", "IntEnum", "QR error-correction levels (= Nayuki QrCode.Ecc ordinal).",
         items(layout["qr_ecc"]))
    enum("LayoutCmd", "IntEnum", "Layout command ops.",
         [(n, v["value"], v.get("doc", "")) for n, v in layout["commands"].items()])
    enum("Icon", "IntEnum", "Built-in icon ids (glyph ids of face 0).",
         [(i["name"].upper(), i["id"], "") for i in spec["icons"]])

    gatt = spec["gatt"]
    L += ["", f'GATT_BASE_UUID: Final = uuid.UUID("{gatt["base_uuid"]}")',
          f"GATT_SERVICE_SHORT: Final = {fmt_int(gatt['service_short'])}",
          f'GATT_SERVICE_UUID: Final = uuid.UUID("{gatt_uuid(gatt["base_uuid"], gatt["service_short"])}")']
    enum("GattChr", "IntEnum", "Characteristic short ids.",
         [(n, v["short"], f"{', '.join(v['props'])}: {v['doc']}") for n, v in gatt["characteristics"].items()])
    L.append("GATT_CHR_UUIDS: Final[dict[GattChr, uuid.UUID]] = {")
    for n, v in gatt["characteristics"].items():
        L.append(f'    GattChr.{n}: uuid.UUID("{gatt_uuid(gatt["base_uuid"], v["short"])}"),')
    L += ["}", ""]
    for name, entry in gatt["fragment_header"].items():
        L.append(f"FRAG_{name}: Final = {fmt_int(entry['value'])}  # {entry['doc']}")
    L.append("")
    enum("CtrlMsg", "IntEnum", "CTRL message types.",
         [(n, v["value"], v["dir"]) for n, v in gatt["ctrl_messages"].items()])
    enum("RecordType", "IntEnum", "Authenticated record types.",
         [(n, v["value"], v["dir"]) for n, v in gatt["record_types"].items()])
    enum("RecordDir", "IntEnum", "Record nonce direction byte.", items(gatt["record_dir"]))
    enum("PlainMsg", "IntEnum", "Plaintext STATUS message types.",
         [(n, v["value"], v["dir"]) for n, v in gatt["plain_messages"].items()])
    L.append("")
    for name, label in spec["crypto"].items():
        L.append(f"CRYPTO_{name.upper()}: Final = {label.encode()!r}")
    L.append("")
    v2 = spec["v2"]
    enum("GrantOp", "IntEnum", "Grant operations (grant key 1).", items(v2["grant_ops"]))
    enum("OwnerState", "IntEnum", "Device ownership states.", items(v2["owner_states"]))
    enum("Link", "IntEnum", "Noise prologue link byte.", items(v2["links"]))
    enum("TunnelState", "IntEnum", "EVT_TUNNEL states.", items(v2["tunnel_states"]))
    enum("PairKind", "IntEnum", "First byte of a PAIR / tunnel message.", items(v2["pair_kinds"]))
    enum("AdvFlag", "IntFlag", "Tag advertising flags.", items(v2["adv_flags"]))
    L.append("")
    for name, label in v2["crypto"].items():
        L.append(f"V2_{name.upper()}: Final = {label.encode()!r}")
    L.append(f"V2_UICR_IDENTITY_OFFSET: Final = {v2['uicr_identity_offset']}")
    L += ["", f"ENROLLMENT_MAGIC: Final = {fmt_int(spec['enrollment']['magic'])}  # bytes 'C','T','A','G'", ""]
    enum("Board", "IntEnum", "Board ids.", items(spec["boards"]))
    enum("Panel", "IntEnum", "Panel ids.", items(spec["panels"]))
    L.append("")
    for name, value in spec["fontpack"].items():
        L.append(f"FONTPACK_{name.upper()}: Final = {fmt_int(value)}")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# msgs.py
# ---------------------------------------------------------------------------

_PY_MSGS_PRELUDE = '''

class MessageError(ValueError):
    """A fixed-layout message could not be packed or unpacked."""


class TruncatedError(MessageError):
    """Input shorter than the message's fixed part (C: -EINVAL)."""


class OversizeError(MessageError):
    """Input or a trailing field longer than allowed (C: -EMSGSIZE)."""


class FixedMessage(Protocol):
    """Shape shared by every generated message class."""

    LEN: ClassVar[int]

    def pack(self) -> bytes: ...

    @classmethod
    def unpack(cls, buf: bytes) -> Self: ...


def _pack(layout: struct.Struct, *values: int | bytes) -> bytes:
    try:
        return layout.pack(*values)
    except struct.error as exc:
        raise MessageError(str(exc)) from None


def _check_bytes(value: bytes, size: int, name: str) -> None:
    if len(value) != size:
        raise MessageError(f"{name}: expected {size} bytes, got {len(value)}")


def _check_len(buf: bytes, fixed: int, longest: int | None) -> None:
    if len(buf) < fixed:
        raise TruncatedError(f"{len(buf)} bytes, need {fixed}")
    if longest is not None and len(buf) > longest:
        raise OversizeError(f"{len(buf)} bytes, at most {longest}")
'''


def _py_message(m: Message) -> list[str]:
    fmt = "<" + "".join(_STRUCT_CODES[f.type] if f.kind == "int" else f"{f.size}s" for f in m.fixed)
    L = ["", "", "@dataclass(frozen=True, slots=True)", f"class {m.py_name}:"]
    doc = m.title
    if m.variable:
        doc += f"; fixed part only (followed by {m.variable.type})"
    L.append(f'    """{doc}."""')
    L.append("")
    if m.type_ref:
        L.append(f"    TYPE: ClassVar[int] = {m.type_ref[0]}.{m.type_ref[1]}")
    L.append(f"    LEN: ClassVar[int] = {m.fixed_len}")
    tail = m.tail
    if tail:
        L.append(f"    MAX_LEN: ClassVar[int] = {m.fixed_len} + {tail.max}")
    L.append(f'    _S: ClassVar[struct.Struct] = struct.Struct("{fmt}")')
    if m.fields:
        L.append("")
    for f in m.fields:
        ptype = "int" if f.kind == "int" else "bytes"
        default = f" = {fmt_int(f.default)}" if f.default is not None else ""
        comment = f"  # {f.doc}" if f.doc else ""
        L.append(f"    {f.name}: {ptype}{default}{comment}")
    L += ["", "    def pack(self) -> bytes:"]
    for f in m.fixed:
        if f.kind == "bytes_n":
            L.append(f'        _check_bytes(self.{f.name}, {f.size}, "{f.name}")')
    if tail:
        L += [f"        if len(self.{tail.name}) > {tail.max}:",
              f'            raise OversizeError(f"{tail.name}: {{len(self.{tail.name})}} bytes > {tail.max}")']
    values = ", ".join(f"self.{f.name}" for f in m.fixed)
    packed = f"_pack(self._S{', ' + values if values else ''})"
    L.append(f"        return {packed}{f' + bytes(self.{tail.name})' if tail else ''}")
    L += ["", "    @classmethod", "    def unpack(cls, buf: bytes) -> Self:"]
    if tail:
        names = [f.name for f in m.fixed]
        L += ["        _check_len(buf, cls.LEN, cls.MAX_LEN)",
              f"        ({', '.join(names)},) = cls._S.unpack_from(buf)",
              f"        return cls({', '.join(names)}, bytes(buf[cls.LEN :]))"]
    elif m.variable:
        L += ["        _check_len(buf, cls.LEN, None)", "        return cls(*cls._S.unpack_from(buf))"]
    else:
        L += ["        _check_len(buf, cls.LEN, cls.LEN)", "        return cls(*cls._S.unpack_from(buf))"]
    return L


def gen_msgs_py(spec: dict[str, Any], sha: str, messages: list[Message]) -> str:
    refs = sorted({m.type_ref[0] for m in messages if m.type_ref})
    maxes = sorted({f.max for m in messages for f in m.fields if f.max})
    L = ['"""Fixed-layout protocol messages (mirror of include/ctag/proto_msgs.h).', ""]
    L += banner(sha)
    L += ["",
          "Each dataclass packs its fields little-endian in spec order; the",
          "type/opcode byte is framing and not part of the layout. ``unpack`` rejects",
          "short input (TruncatedError) and input longer than the layout",
          "(OversizeError); a trailing ``bytes`` field takes the rest up to its max;",
          "messages followed by a variable part decode only their fixed part.",
          '"""', "", "from __future__ import annotations", "", "import struct",
          "from dataclasses import dataclass", "from typing import ClassVar, Protocol, Self", "",
          f"from .ids import {', '.join(maxes + refs)}"]
    L.append(_PY_MSGS_PRELUDE.rstrip("\n"))
    for m in messages:
        L += _py_message(m)
    registries = [("MESH_MESSAGES", "MeshOp", "mesh_"), ("LAYOUT_COMMANDS", "LayoutCmd", "layout_cmd_"),
                  ("CTRL_MESSAGES", "CtrlMsg", "ctrl_"), ("RECORD_MESSAGES", "RecordType", "rec_"),
                  ("PLAIN_MESSAGES", "PlainMsg", "plain_")]
    L += ["", ""]
    for reg, enum, prefix in registries:
        L.append(f"{reg}: dict[{enum}, type[FixedMessage]] = {{")
        for m in messages:
            if m.c_name.startswith(prefix) and m.type_ref:
                L.append(f"    {enum}.{m.type_ref[1]}: {m.py_name},")
        L.append("}")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def generate() -> dict[Path, str]:
    text = spec_text()
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    spec = load_spec(text)
    messages = collect_messages(spec)
    check_spec(spec, messages)
    return {
        OUT_IDS_PY: gen_ids_py(spec, sha, text),
        OUT_MSGS_PY: gen_msgs_py(spec, sha, messages),
    }


def stale_outputs(outputs: dict[Path, str]) -> list[Path]:
    stale = []
    for path, content in outputs.items():
        current = path.read_bytes().decode("utf-8").replace("\r\n", "\n") if path.exists() else None
        if current != content:
            stale.append(path)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a generated file is stale")
    args = parser.parse_args(argv)
    try:
        outputs = generate()
    except SpecError as exc:
        print(f"codegen: {exc}", file=sys.stderr)
        return 2
    if args.check:
        stale = stale_outputs(outputs)
        for path in stale:
            print(f"stale: {path.relative_to(ROOT).as_posix()}", file=sys.stderr)
        return 1 if stale else 0
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
