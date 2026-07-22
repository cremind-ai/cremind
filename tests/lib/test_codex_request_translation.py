"""Tests for the Codex request-translation helpers (app/lib/llm/openai_codex.py).

Pure functions: chat.completions messages/tools/response_format → Responses body,
the reasoning-effort matrix, the strict key allowlist, and content-part mapping.
"""

from __future__ import annotations

from app.lib.llm import openai_codex as oc


def _find(items, type_, role=None):
    return [i for i in items if i.get("type") == type_ and (role is None or i.get("role") == role)]


# ── instructions split ──────────────────────────────────────────────────────

def test_first_system_becomes_instructions():
    instr, items = oc._convert_messages_to_input([
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ])
    assert instr == "You are helpful."
    assert _find(items, "message", "developer") == []


def test_second_system_becomes_developer_item():
    instr, items = oc._convert_messages_to_input([
        {"role": "system", "content": "primary"},
        {"role": "system", "content": "extra"},
        {"role": "user", "content": "hi"},
    ])
    assert instr == "primary"
    dev = _find(items, "message", "developer")
    assert len(dev) == 1 and dev[0]["content"][0]["text"] == "extra"


def test_no_system_uses_default_instructions():
    instr, _ = oc._convert_messages_to_input([{"role": "user", "content": "hi"}])
    assert instr == oc._DEFAULT_INSTRUCTIONS


# ── tool-call / tool-result round trip ──────────────────────────────────────

def test_assistant_tool_calls_and_tool_result_order():
    _, items = oc._convert_messages_to_input([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "working", "tool_calls": [
            {"id": "call_abc", "type": "function", "function": {"name": "f", "arguments": '{"x":1}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "result-text"},
    ])
    types = [i["type"] for i in items]
    # assistant message, then its function_call, then the function_call_output
    assert types == ["message", "message", "function_call", "function_call_output"]
    fc = _find(items, "function_call")[0]
    assert fc["call_id"] == "call_abc" and fc["name"] == "f" and fc["arguments"] == '{"x":1}'
    out = _find(items, "function_call_output")[0]
    assert out["call_id"] == "call_abc" and out["output"] == "result-text"


def test_call_id_clamped_to_64():
    long_id = "call_" + "z" * 100
    _, items = oc._convert_messages_to_input([
        {"role": "assistant", "tool_calls": [
            {"id": long_id, "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
    ])
    assert len(_find(items, "function_call")[0]["call_id"]) == 64


def test_empty_input_gets_placeholder():
    _, items = oc._convert_messages_to_input([{"role": "system", "content": "only system"}])
    assert len(items) == 1 and items[0]["role"] == "user"


# ── tools / tool_choice ─────────────────────────────────────────────────────

def test_tools_flattened():
    tools = [{"type": "function", "function": {
        "name": "f", "description": "d", "parameters": {"type": "object", "properties": {"a": {}}}, "strict": True,
    }}]
    out = oc._convert_tools(tools)
    assert out == [{
        "type": "function", "name": "f", "description": "d",
        "parameters": {"type": "object", "properties": {"a": {}}}, "strict": True,
    }]


def test_tools_default_parameters():
    out = oc._convert_tools([{"type": "function", "function": {"name": "f"}}])
    assert out[0]["parameters"] == {"type": "object", "properties": {}}


def test_tool_choice_variants():
    assert oc._convert_tool_choice("auto") == "auto"
    assert oc._convert_tool_choice("none") == "none"
    assert oc._convert_tool_choice("required") == "required"
    assert oc._convert_tool_choice({"type": "function", "function": {"name": "f"}}) == {"type": "function", "name": "f"}
    assert oc._convert_tool_choice(None) is None


# ── response_format → text.format ───────────────────────────────────────────

def test_text_format_json_schema():
    rf = {"type": "json_schema", "json_schema": {"name": "Out", "schema": {"type": "object"}, "strict": True}}
    assert oc._text_format(rf) == {"format": {"type": "json_schema", "name": "Out", "schema": {"type": "object"}, "strict": True}}


def test_text_format_json_object_and_none():
    assert oc._text_format({"type": "json_object"}) == {"format": {"type": "json_object"}}
    assert oc._text_format({"type": "text"}) is None
    assert oc._text_format(None) is None


# ── reasoning-effort matrix ─────────────────────────────────────────────────

def test_resolve_effort_matrix():
    assert oc._resolve_effort("high", None) == "high"
    assert oc._resolve_effort("", "high") == "none"       # instant mode
    assert oc._resolve_effort(None, "low") == "low"       # default fallback
    assert oc._resolve_effort(None, None) == "medium"     # hard default
    assert oc._resolve_effort("minimal", None) == "low"


# ── full body assembly + allowlist ──────────────────────────────────────────

def test_build_body_forced_fields_and_allowlist():
    body = oc._build_request_body(
        model="gpt-5.6-sol",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f"}}],
        tool_choice="auto",
        response_format={"type": "json_object"},
        reasoning_effort="high",
        default_reasoning_effort=None,
        session_id="sess-1",
    )
    assert set(body).issubset(oc._ALLOWLIST)
    assert body["store"] is False and body["stream"] is True
    assert body["prompt_cache_key"] == "sess-1"
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["text"] == {"format": {"type": "json_object"}}
    assert body["tool_choice"] == "auto"


def test_build_body_no_include_when_effort_none():
    body = oc._build_request_body(
        "gpt-5.6-sol", [{"role": "user", "content": "hi"}], None, None, None,
        reasoning_effort="", default_reasoning_effort=None, session_id="s",
    )
    assert body["reasoning"] == {"effort": "none", "summary": "auto"}
    assert "include" not in body


# ── content parts ───────────────────────────────────────────────────────────

def test_image_data_url_to_input_image():
    parts = oc._user_content_parts([
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC", "detail": "high"}},
    ])
    assert {"type": "input_text", "text": "look"} in parts
    img = [p for p in parts if p["type"] == "input_image"][0]
    assert img["image_url"] == "data:image/png;base64,ABC" and img["detail"] == "high"


def test_audio_part_dropped():
    parts = oc._user_content_parts([
        {"type": "text", "text": "t"},
        {"type": "input_audio", "input_audio": {"data": "..."}},
    ])
    assert all(p["type"] != "input_audio" for p in parts)


def test_server_id_regex():
    assert oc._SERVER_ID_RE.match("rs_123")
    assert oc._SERVER_ID_RE.match("fc_abc")
    assert oc._SERVER_ID_RE.match("resp_x")
    assert oc._SERVER_ID_RE.match("msg_y")
    assert not oc._SERVER_ID_RE.match("call_abc")
