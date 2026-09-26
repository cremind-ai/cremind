"""Vision for Documentation search: strict model resolution, consent, eligibility,
and the caption itself.

The load-bearing promise is the first test group: captioning uses the
Specialized Vision Model the user picked, and when there is none it stops —
it never quietly falls back to the main model, which the model-group resolver
would otherwise do.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from app.documents.vision import captioner, resolver


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def get(self, table, key, profile="admin"):
        return self.rows.get(key)


class _Mgr:
    def __init__(self, rows, *, auth_ok=True):
        self.config_storage = _Store(rows)
        self._auth_ok = auth_ok

    def _parse_group_value(self, raw):
        p, m = raw.split("/", 1)
        return p, m

    def _model_auth_compatible(self, p, m, profile):
        return self._auth_ok


@pytest.fixture
def vision(monkeypatch):
    state = {"enabled": True, "rows": {"model_group.vision": "openai/gpt-4o"}, "auth_ok": True, "capable": True}
    import app.config as cfg

    monkeypatch.setattr(cfg, "vision_feature_enabled", lambda profile=None: state["enabled"])
    monkeypatch.setattr(cfg, "model_supports_vision", lambda p, m, profile=None: state["capable"])
    monkeypatch.setattr(resolver, "_manager", lambda: _Mgr(state["rows"], auth_ok=state["auth_ok"]))
    return state


def test_resolves_the_chosen_vision_model(vision):
    r = resolver.resolve_dedicated_vision("alice")
    assert (r.ok, r.provider, r.model) == (True, "openai", "gpt-4o")


@pytest.mark.parametrize("change,reason", [
    ({"enabled": False}, resolver.REASON_DISABLED),
    ({"rows": {}}, resolver.REASON_UNSET),
    ({"auth_ok": False}, resolver.REASON_AUTH),
    ({"capable": False}, resolver.REASON_NOT_CAPABLE),
])
def test_never_falls_back_to_the_main_model(vision, change, reason):
    vision.update(change)
    r = resolver.resolve_dedicated_vision("alice")
    assert not r.ok and r.reason == reason


def test_building_the_llm_uses_exactly_the_checked_model(vision, monkeypatch):
    built = {}
    import app.lib.llm.factory as factory

    def fake_create(provider, model, **kw):
        built.update(provider=provider, model=model)
        return SimpleNamespace(provider_name=provider, model_name=model)

    monkeypatch.setattr(factory, "create_llm_provider", fake_create)
    monkeypatch.setattr(_Mgr, "_get_group_reasoning_effort", lambda self, g, profile=None: None, raising=False)
    r = resolver.resolve_dedicated_vision("alice")
    resolver.build_vision_llm("alice", r)
    assert built == {"provider": "openai", "model": "gpt-4o"}
    with pytest.raises(ValueError):
        resolver.build_vision_llm("alice", resolver.VisionResolution(False, reason="vision_disabled"))


def test_consent_is_per_provider_and_model():
    ok = resolver.VisionResolution(True, "openai", "gpt-4o")
    opts = {"caption_consent": {"provider": "openai", "model": "gpt-4o", "at": 1}}
    assert resolver.consent_matches(opts, ok)
    assert not resolver.consent_matches(opts, resolver.VisionResolution(True, "anthropic", "claude"))
    assert not resolver.consent_matches({}, ok)


@pytest.mark.parametrize("kw,expected", [
    (dict(width=4000, height=3000, size_bytes=2_000_000, rel_path="Photos/park.jpg"), True),
    (dict(width=64, height=64, size_bytes=50_000, rel_path="Photos/tiny.png"), False),
    (dict(width=4000, height=3000, size_bytes=5_000, rel_path="Photos/small.jpg"), False),
    (dict(width=512, height=512, size_bytes=50_000, rel_path="app/assets/logo.png"), False),
    (dict(width=512, height=512, size_bytes=50_000, rel_path="favicon.ico"), False),
])
def test_icons_and_tiny_images_are_not_worth_a_vision_call(kw, expected):
    ok, _ = captioner.eligible(options={"caption": {"enabled": True, "min_px": 256, "min_kb": 20}}, **kw)
    assert ok is expected


def test_captions_off_means_no_calls():
    ok, reason = captioner.eligible(width=4000, height=3000, size_bytes=2_000_000, rel_path="a.jpg",
                                    options={"caption": {"enabled": False}})
    assert not ok and reason == "captions_off"


def test_caption_text_carries_counts_scene_and_exif():
    data = {
        "kind": "photo", "summary": "Two puppies playing on grass.",
        "animals": [{"species": "dog", "count": 2}], "people_count": 0,
        "scene": {"setting": "outdoor", "place_type": "park"},
        "tags": ["puppy", "park"], "tags_local": ["chó", "công viên"],
    }
    text = captioner.render_caption_text(data, {"taken_at": "2026-09-20T14:05:00", "make": "Apple", "model": "iPhone 14",
                                                "gps": {"lat": 21.03, "lon": 105.85}})
    assert "Photo taken 2026-09-20 14:05 with Apple iPhone 14" in text
    assert "2 dogs" in text and "park" in text and "công viên" in text
    assert "21.0300, 105.8500" in text


def test_caption_json_is_parsed_leniently():
    assert captioner.parse_caption_json('Sure! ```json\n{"summary": "x"}\n```')["summary"] == "x"
    assert captioner.parse_caption_json("just prose")["summary"] == "just prose"
    assert captioner.parse_caption_json("") == {}


def test_prepare_jpeg_rotates_and_shrinks():
    Image = pytest.importorskip("PIL.Image")
    im = Image.new("RGB", (3000, 1000), (200, 10, 10))
    exif = im.getexif()
    exif[0x0112] = 6  # rotate 90° CW on display
    buf = BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes())
    out = captioner.prepare_jpeg(data=buf.getvalue(), max_side=1024)
    with Image.open(BytesIO(out)) as small:
        assert max(small.size) == 1024
        assert small.size[1] > small.size[0]  # the EXIF rotation was applied


def test_run_vision_parses_and_records_usage(monkeypatch):
    from app.constants import ChatCompletionTypeEnum

    recorded = []

    class FakeLLM:
        provider_name, model_name = "openai", "gpt-4o"

        async def chat_completion(self, **kw):
            assert kw["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": '{"summary": "A dog.", "animals": [{"species": "dog", "count": 1}]}'}
            yield {"type": ChatCompletionTypeEnum.DONE, "usage": {"input_tokens": 100, "output_tokens": 20}}

    async def fake_record(llm, usage, profile, label):
        recorded.append((profile, label, usage))

    monkeypatch.setattr(captioner, "_record_usage", fake_record)
    import app.lib.llm.base as base

    monkeypatch.setattr(base, "done_chunk_token_usage", lambda resp: resp.get("usage") or {})
    res = captioner.run_vision(FakeLLM(), "alice", b"\xff\xd8fake", mode="image", exif={})
    assert "A dog." in res.text and res.data["animals"][0]["species"] == "dog"
    assert res.tokens_in == 100 and res.tokens_out == 20
    assert recorded and recorded[0][0] == "alice" and "caption" in recorded[0][1]
