"""Image captions through the real engine, with a fake vision model.

- no consent → the photo is indexed by name/EXIF and waits (``awaiting_consent``);
- consent given → the waiting photo is captioned without re-reading anything
  else, and the caption is searchable;
- a copy of the same photo is never sent to the vision model again;
- the daily cap holds: the next photo waits for tomorrow (``over_cap``);
- only the resolved Specialized Vision Model is ever built.
"""

from __future__ import annotations

import base64
import os
import shutil
from io import BytesIO

import pytest

pytest.importorskip("a2a")
PIL_Image = pytest.importorskip("PIL.Image")

from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.documents import runtime as rt_module  # noqa: E402
from app.documents.vision import captioner, resolver  # noqa: E402

from .test_engine_e2e import _files, _idle, _start, _wait, env  # noqa: E402,F401

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


def _photo(path, color=(30, 160, 40)):
    im = PIL_Image.new("RGB", (800, 600), color)
    exif = im.getexif()
    exif[0x010F] = "Apple"       # Make
    exif[0x0110] = "iPhone 14"   # Model
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = "2026:09:20 14:05:00"  # DateTimeOriginal
    buf = BytesIO()
    im.save(buf, format="JPEG", exif=exif, quality=95)
    path.write_bytes(buf.getvalue() + os.urandom(30_000))  # >20 KB, unique bytes


def _pixel(messages):
    """The top-left colour of the image in a vision request."""
    for part in messages[-1]["content"]:
        if part.get("type") == "image_url":
            data = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
            with PIL_Image.open(BytesIO(data)) as im:
                return im.convert("RGB").getpixel((0, 0))
    return None


class FakeVisionLLM:
    """Captions every photo as "two puppies"; an image *check* counts two
    dogs in a blue photo and three in a red one — the caption was wrong
    about the red one."""

    provider_name, model_name = "openai", "gpt-4o"
    calls = 0
    checks = 0

    async def chat_completion(self, **kw):
        messages = kw["messages"]
        if messages[0]["content"] == captioner._VERIFY_SYSTEM:
            FakeVisionLLM.checks += 1
            r, _g, b = _pixel(messages)
            dogs = 2 if b > r else 3
            data = f'{{"counts": {{"dogs": {dogs}}}, "matches": {"true" if dogs == 2 else "false"}, "why": "x"}}'
        else:
            FakeVisionLLM.calls += 1
            data = ('{"kind": "photo", "summary": "Two puppies playing in a park.", '
                    '"animals": [{"species": "dog", "count": 2}], '
                    '"scene": {"setting": "outdoor", "place_type": "park"}, "tags": ["puppy", "park"]}')
        yield {"type": ChatCompletionTypeEnum.CONTENT, "data": data}
        yield {"type": ChatCompletionTypeEnum.DONE, "usage": {"input_tokens": 50, "output_tokens": 30}}


@pytest.fixture
def vision(env, monkeypatch):
    FakeVisionLLM.calls = FakeVisionLLM.checks = 0
    built = []
    monkeypatch.setattr(resolver, "resolve_dedicated_vision",
                        lambda profile: resolver.VisionResolution(True, "openai", "gpt-4o"))

    def build(profile, res):
        built.append((res.provider, res.model))
        return FakeVisionLLM()

    monkeypatch.setattr(resolver, "build_vision_llm", build)
    import app.lib.llm.base as base

    monkeypatch.setattr(base, "done_chunk_token_usage", lambda resp: resp.get("usage") or {})

    async def no_usage(*a, **k):
        return None

    monkeypatch.setattr(captioner, "_record_usage", no_usage)
    monkeypatch.setattr(rt_module.ProfileRuntime, "caption_cap", lambda self: 1)
    return built


def _consent(env):
    env.svc.control("alice", "consent_vision", model="openai/gpt-4o")


def test_photos_wait_for_consent_then_get_captioned_once(env, vision):
    _photo(env.alice / "park.jpg")
    _start(env, "alice")
    rt = _idle(env, "alice")
    row = _files(rt)["park.jpg"]
    assert row["caption_state"] == "awaiting_consent"
    assert row["is_camera_photo"] == 1 and row["taken_at"]
    assert FakeVisionLLM.calls == 0

    _consent(env)
    _wait(lambda: rt.requeue_waiting_vision() >= 0 and _files(rt)["park.jpg"]["caption_state"] == "done", 60)
    _idle(env, "alice")
    assert FakeVisionLLM.calls == 1
    assert vision == [("openai", "gpt-4o")]
    hits = rt.db.fts_search('"puppies"', limit=5)
    assert hits, "the caption must be searchable"
    caption = env.storage.get_caption("alice", _files(rt)["park.jpg"]["sha256"])
    assert caption and "2 dogs" in caption["caption_text"]

    # A copy of the same photo reuses the cached caption: no second call, and
    # the daily cap (1) is not consumed again.
    shutil.copyfile(env.alice / "park.jpg", env.alice / "park copy.jpg")
    _wait(lambda: _files(rt).get("park copy.jpg", {}).get("caption_state") == "done", 60)
    assert FakeVisionLLM.calls == 1


def test_the_daily_cap_holds(env, vision):
    _photo(env.alice / "a.jpg", (10, 10, 200))
    _photo(env.alice / "b.jpg", (200, 10, 10))
    env.enable("alice", env.alice)
    _consent(env)
    _start(env, "alice")
    rt = _idle(env, "alice")
    states = sorted(r["caption_state"] for r in _files(rt).values())
    assert states == ["done", "over_cap"]
    assert FakeVisionLLM.calls == 1


def test_a_failed_caption_refunds_its_slot_and_retry_all_tries_again(env, vision, monkeypatch):
    class Flaky(FakeVisionLLM):
        async def chat_completion(self, **kw):
            raise RuntimeError("provider 503")
            yield  # pragma: no cover — an async generator that fails at once

    monkeypatch.setattr(rt_module.ProfileRuntime, "caption_cap", lambda self: 5)
    monkeypatch.setattr(resolver, "build_vision_llm", lambda profile, res: Flaky())
    _photo(env.alice / "park.jpg")
    env.enable("alice", env.alice)
    _consent(env)
    _start(env, "alice")
    rt = _idle(env, "alice")
    assert _files(rt)["park.jpg"]["caption_state"] == "failed"
    day = captioner.local_day("alice")
    assert env.storage.vision_usage("alice", day)["captions"] == 0  # refunded

    monkeypatch.setattr(resolver, "build_vision_llm", lambda profile, res: FakeVisionLLM())
    assert env.svc.control("alice", "retry_failed")["files"] == 1
    _wait(lambda: _files(rt)["park.jpg"]["caption_state"] == "done", 60)
    assert env.storage.vision_usage("alice", day)["captions"] == 1


def test_images_indexed_with_descriptions_off_are_described_once_turned_on(env, vision):
    from app.documents import state as uds_state

    _photo(env.alice / "park.jpg")
    env.enable("alice", env.alice)
    _consent(env)
    opts = env.storage.get_source("alice", "local")["options"]
    env.storage.upsert_source("alice", "local", options={**opts, "caption": {**opts["caption"], "enabled": False}})
    _start(env, "alice")
    rt = _idle(env, "alice")
    assert _files(rt)["park.jpg"]["caption_state"] == "captions_off"
    assert rt.requeue_waiting_vision() == 0 and FakeVisionLLM.calls == 0

    env.storage.upsert_source("alice", "local", options={**opts, "caption": {**opts["caption"], "enabled": True}})
    uds_state.notify_settings_changed("alice", "local")
    _wait(lambda: rt.requeue_waiting_vision() >= 0 and _files(rt)["park.jpg"]["caption_state"] == "done", 60)
    assert FakeVisionLLM.calls == 1


def _two_captioned(env, monkeypatch):
    """Red and blue photos, both captioned "two puppies", cap high enough to
    also cover the checks."""
    monkeypatch.setattr(rt_module.ProfileRuntime, "caption_cap", lambda self: 50)
    monkeypatch.setattr(captioner, "daily_cap", lambda options: 50)
    _photo(env.alice / "red.jpg", (220, 20, 20))
    _photo(env.alice / "blue.jpg", (20, 20, 220))
    env.enable("alice", env.alice)
    _consent(env)
    _start(env, "alice")
    rt = _idle(env, "alice")
    assert {r["caption_state"] for r in _files(rt).values()} == {"done"}
    from app.documents.query import open_engine

    access = open_engine("alice")
    assert access.engine is not None, access.message
    return rt, access.engine


def test_verify_images_reranks_by_what_the_vision_model_sees(env, vision, monkeypatch):
    rt, engine = _two_captioned(env, monkeypatch)
    ask = dict(filters={"types": ["image"]}, image_objects=[{"label": "puppy", "count": 2}], expand=False)

    plain = engine.search("two puppies in a park", **ask)
    assert {g.file["name"] for g in plain.groups} == {"red.jpg", "blue.jpg"}
    assert FakeVisionLLM.checks == 0

    checked = engine.search("two puppies in a park", verify_images=True, **ask)
    assert FakeVisionLLM.checks == 2
    assert [g.file["name"] for g in checked.groups] == ["blue.jpg", "red.jpg"]
    assert any("2 image(s) re-checked" in n for n in checked.notes)
    red = checked.groups[1].passages[0].reasons
    assert "checked: 3 dog, not 2" in red and "checked: does not match the description" in red
    # Each check came out of the same daily quota as the captions.
    day = captioner.local_day("alice")
    assert env.storage.vision_usage("alice", day)["captions"] == 4
    assert vision == [("openai", "gpt-4o")] * len(vision)


def test_verify_images_never_falls_back_to_another_model(env, vision, monkeypatch):
    rt, engine = _two_captioned(env, monkeypatch)
    built_before = len(vision)
    monkeypatch.setattr(resolver, "resolve_dedicated_vision",
                        lambda profile: resolver.VisionResolution(False, None, None, "vision_model_unset"))

    out = engine.search("two puppies", filters={"types": ["image"]}, verify_images=True, expand=False)
    assert len(out.groups) == 2
    assert any("Image check unavailable (vision_model_unset)" in n for n in out.notes)
    assert FakeVisionLLM.checks == 0 and len(vision) == built_before


class GoldenVisionLLM:
    """Captions by colour: green = two puppies in a park (the right photo),
    red = a cat in a park, blue = a screenshot showing two puppies."""

    provider_name, model_name = "openai", "gpt-4o"

    async def chat_completion(self, **kw):
        messages = kw["messages"]
        r, g, b = _pixel(messages)
        if messages[0]["content"] == captioner._VERIFY_SYSTEM:
            dogs = 2 if g > max(r, b) or b > max(r, g) else 0
            data = f'{{"counts": {{"dog": {dogs}}}, "matches": {"true" if g > max(r, b) else "false"}}}'
        elif g > max(r, b):
            data = ('{"kind": "photo", "summary": "Two puppies playing on the grass in a park.", '
                    '"animals": [{"species": "dog", "count": 2}], '
                    '"scene": {"setting": "outdoor", "place_type": "park"}, "tags": ["puppy", "park", "grass"]}')
        elif r > max(g, b):
            data = ('{"kind": "photo", "summary": "A cat sitting on a bench in a park.", '
                    '"animals": [{"species": "cat", "count": 1}], '
                    '"scene": {"setting": "outdoor", "place_type": "park"}, "tags": ["cat", "park"]}')
        else:
            data = ('{"kind": "screenshot", "summary": "A chat app screenshot with a picture of two puppies.", '
                    '"animals": [{"species": "dog", "count": 2}], "tags": ["screenshot", "puppy"]}')
        yield {"type": ChatCompletionTypeEnum.CONTENT, "data": data}
        yield {"type": ChatCompletionTypeEnum.DONE, "usage": {"input_tokens": 50, "output_tokens": 30}}


def _screenshot(path, color):
    im = PIL_Image.new("RGB", (1170, 800), color)
    buf = BytesIO()
    im.save(buf, format="PNG")
    path.write_bytes(buf.getvalue() + os.urandom(30_000))


def test_golden_two_puppies_at_the_park_that_i_photographed(env, vision, monkeypatch):
    """Query 2 of the design: "images of 2 puppies at the park that I
    photographed". The right photo wins over a cat in the same park, a
    screenshot of puppies, and a document about puppies in the park."""
    monkeypatch.setattr(resolver, "build_vision_llm", lambda profile, res: GoldenVisionLLM())
    monkeypatch.setattr(rt_module.ProfileRuntime, "caption_cap", lambda self: 50)
    monkeypatch.setattr(captioner, "daily_cap", lambda options: 50)
    (env.alice / "Photos").mkdir()
    _photo(env.alice / "Photos" / "IMG_2041.jpg", (40, 200, 60))
    _photo(env.alice / "Photos" / "IMG_2042.jpg", (210, 40, 40))
    _screenshot(env.alice / "Screenshot 2026-09-21 at 10.02.png", (40, 60, 210))
    (env.alice / "puppy notes.md").write_text(
        "# Park day\n\nTook the two puppies to the park; they played on the grass for an hour.\n",
        encoding="utf-8")
    env.enable("alice", env.alice)
    row = env.storage.get_source("alice", "local")
    env.storage.upsert_source("alice", "local", options={
        **(row.get("options") or {}), "identity": {"camera_devices": ["iPhone 14"]}})
    _consent(env)
    _start(env, "alice")
    rt = _idle(env, "alice")
    assert {_files(rt)[n]["caption_state"] for n in
            ("Photos/IMG_2041.jpg", "Photos/IMG_2042.jpg", "Screenshot 2026-09-21 at 10.02.png")} == {"done"}

    from app.documents.query import open_engine

    engine = open_engine("alice").engine
    out = engine.search(
        "2 puppies at the park",
        filters={"types": ["image"], "taken_by": "me", "image_origin": "camera"},
        image_objects=[{"label": "puppy", "count": 2}], expand=False,
    )
    names = [g.file["name"] for g in out.groups]
    assert names[0] == "IMG_2041.jpg", names
    assert "puppy notes.md" not in names          # a document is not an image
    best = out.groups[0].passages[0].reasons
    assert "taken with your camera" in best and "caption: 2 dog" in best

    # The same ask, re-checked by the vision model, keeps the right answer
    # first and pushes the screenshot and the cat down.
    checked = engine.search(
        "2 puppies at the park", filters={"types": ["image"], "taken_by": "me"},
        image_objects=[{"label": "puppy", "count": 2}], verify_images=True, expand=False,
    )
    assert checked.groups[0].file["name"] == "IMG_2041.jpg"
    assert checked.groups[0].score > 2 * max(g.score for g in checked.groups[1:])


def test_consent_names_the_current_model_and_never_rides_a_settings_save(env, vision):
    from app.api.documents import _build_patch
    from app.documents.errors import EngineError

    env.enable("alice", env.alice)
    # No model named, or another model: refused, nothing recorded.
    for shown in (None, "anthropic/claude-sonnet-5"):
        with pytest.raises(EngineError) as exc:
            env.svc.control("alice", "consent_vision", model=shown)
        assert exc.value.code == "VisionModelChanged"
    assert (env.storage.get_source("alice", "local")["options"] or {}).get("caption_consent") is None

    # A settings PUT carrying consent keeps everything else and drops it.
    cur = env.storage.get_source("alice", "local")
    patch = _build_patch("alice", "local", {"options": {
        "caption_consent": {"provider": "openai", "model": "gpt-4o", "at": 1},
        "caption": {"daily_cap": 5},
    }}, cur)
    assert patch["options"]["caption"]["daily_cap"] == 5
    assert patch["options"].get("caption_consent") is None


def test_verify_images_needs_consent_for_the_current_model(env, vision, monkeypatch):
    rt, engine = _two_captioned(env, monkeypatch)
    env.svc.control("alice", "revoke_vision_consent")

    out = engine.search("two puppies", filters={"types": ["image"]}, verify_images=True, expand=False)
    assert any("has not been allowed" in n for n in out.notes)
    assert FakeVisionLLM.checks == 0
