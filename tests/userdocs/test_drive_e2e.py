"""Google Drive as a User Document Search source, through the real engine.

The engine is the real one — extractor subprocesses, an in-memory Qdrant, the
deterministic fake embedder of :mod:`test_engine_e2e` — and only Google is
fake: an in-memory Drive with the client contract's shapes (the same fake the
Drive source's own tests use), a token file that is a dict, and a clock the
7-day purge timer reads.

What must hold, in the user's words:

- turning Drive on indexes what the account lets Cremind see, and later
  changes arrive through the change feed;
- renaming a file re-cards it without downloading it again;
- editing a few lines re-embeds only the changed chunks;
- trashing a file removes it, vectors included;
- many files vanishing at once is held for a decision, then applied;
- revoked access hides Drive at once and deletes its index only after
  7 days, on a fresh confirmation — an unreachable Drive is never purged;
- linking another Google account purges and re-indexes;
- a profile can search Drive with its local folder off, and gets the tool;
- two profiles never see each other's Drive;
- a Drive hold never stalls the local folder.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")
pytest.importorskip("qdrant_client")

from app.userdocs import service as svc_module  # noqa: E402
from app.userdocs import settings as uds  # noqa: E402
from app.userdocs import state as uds_state  # noqa: E402
from app.userdocs import types as t  # noqa: E402
from app.userdocs.sources import drive as drv  # noqa: E402

from .test_drive_source import FILE_SCOPE, MIME_DOC, FakeDrive  # noqa: E402
from .test_engine_e2e import _para, _wait, env  # noqa: E402,F401

try:  # the image test needs Pillow; everything else runs without it
    from .test_engine_vision_e2e import vision  # noqa: E402,F401
except pytest.skip.Exception:
    @pytest.fixture
    def vision():
        pytest.skip("Pillow is not installed")

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

DAY = 86400.0


class Clock:
    """Real time plus an offset: the purge timer can be moved a week ahead
    while everything else (waits, housekeeping) keeps real time."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture
def drive(env, monkeypatch):
    ns = SimpleNamespace(fakes={}, tokens={}, forgot=[], citations=[], clock=Clock())
    monkeypatch.setattr(drv, "_now", ns.clock)
    monkeypatch.setattr(drv, "UNLINK_RECHECK_S", 0.0)
    monkeypatch.setattr(drv, "_default_client_factory",
                        lambda profile, stop: ns.fakes.setdefault(profile, FakeDrive(None)))
    monkeypatch.setattr(drv, "_token_present", lambda p: ns.tokens.get(p) is not None)
    monkeypatch.setattr(drv, "_token_info", lambda p: ns.tokens.get(p))
    monkeypatch.setattr(drv, "_forget_access_token", ns.forgot.append)
    monkeypatch.setattr(drv, "_purge_citations", lambda p: ns.citations.append(p) or 0)
    monkeypatch.setattr(svc_module, "_purge_drive_citations", ns.citations.append)

    def link(profile: str, *, ident: str | None = None) -> FakeDrive:
        fake = ns.fakes.setdefault(profile, FakeDrive(None))
        fake.ident = ident or f"cid|{profile}"
        ns.tokens[profile] = {"email": f"{profile}@example.com", "scopes": ["openid", FILE_SCOPE]}
        return fake

    def unlink(profile: str) -> None:
        ns.tokens[profile] = None
        ns.fakes[profile].ident = None

    def enable(profile: str, **options) -> None:
        env.storage.upsert_source(profile, "drive", enabled=True, options=options)

    ns.link, ns.unlink, ns.enable = link, unlink, enable
    return ns


# ── helpers ────────────────────────────────────────────────────────────────


def _work_drive(fake: FakeDrive) -> None:
    """Work/report.txt (long), a loose note, a Google Doc."""
    fake.folder("F", "Work")
    fake.put("a", "report.txt", parents=["F"],
             data=("Quarterly zebracorn report.\n\n" + "\n\n".join(_para(i) for i in range(6))).encode())
    fake.put("b", "loose.txt", data=b"Bravo marmoset notes for the garden.")
    fake.put("d", "Plan", mime=MIME_DOC, data=b"# Plan\n\nShip the narwhal feature\\.")


def _start(env, drive, *profiles: str, local: tuple[str, ...] = ()) -> None:
    for p in local:
        env.enable(p, getattr(env, p))
    env.svc.start()
    for p in profiles:
        _wait(lambda: env.svc.runtime(p) is not None and env.svc.runtime(p).drive.enabled)
    for p in local:
        _wait(lambda: env.svc.runtime(p) is not None and env.svc.runtime(p).active)


def _rows(rt) -> dict[str, dict]:
    return {r["drive_file_id"]: r for r in rt.db.list_files(source="drive", limit=10_000)}


def _local(rt) -> dict[str, dict]:
    return {r["rel_path"]: r for r in rt.db.list_files(source="local", limit=10_000)}


def _settled(env, profile: str, pred=None, timeout: float = 60.0):
    """Wait for ``pred`` (the change a test is waiting for), then until
    nothing is queued, syncing or in flight and every chunk has a vector."""
    if pred is not None:
        _wait(lambda: (rt := env.svc.runtime(profile)) is not None and rt.db is not None and pred(rt), timeout)

    def done():
        rt = env.svc.runtime(profile)
        if rt is None or rt.db is None or rt.in_flight or rt.scanning:
            return False
        if rt.drive.view()["state"] == "syncing":
            return False
        if rt.db.count_by_status("drive").get("dirty") or rt.db.count_by_status("local").get("dirty"):
            return False
        return rt.db.stats()["chunks_without_vectors"] == 0 and rt
    return _wait(done, timeout)


def _indexed(*ids: str):
    def pred(rt) -> bool:
        rows = _rows(rt)
        return all(rows.get(i, {}).get("status") in ("indexed", "metadata_only") for i in ids)
    return pred


def _sync(env, profile: str, *, full: bool = False) -> None:
    env.svc.control(profile, "rescan" if full else "sync_now", source="drive")


def _synced_once_more(env, drive, profile: str) -> None:
    """Ask for a sync and wait until it has talked to Drive and finished
    (for a held source, where no row change says it ran)."""
    fake = drive.fakes[profile]
    before = len(fake.calls)
    _sync(env, profile)
    _wait(lambda: len(fake.calls) > before + 1)
    _settled(env, profile)


def _engine(profile: str):
    from app.userdocs.query import open_engine

    access = open_engine(profile)
    assert access.engine is not None, access.message
    return access.engine


def _names(profile: str, query: str, **filters) -> list[str]:
    """Files the search found by keyword (the fake embedder's vectors mean
    nothing, so a vector-only neighbour is not a match)."""
    out = _engine(profile).search(query, filters=filters or None, expand=False)
    return [g.file["name"] for g in out.groups if g.file and set(g.evidence) - {"vector"}]


def _collection_count(env, rt) -> int:
    return env.store.count(rt.db.active_collection()["name"])


def _body_ids(rt, fid: int) -> set[int]:
    return {c.id for c in rt.db.get_chunks(fid) if c.ordinal >= 0}


# ── sync, then changes ──────────────────────────────────────────────────────


def test_first_sync_indexes_drive_next_to_the_folder_and_then_follows_changes(env, drive):
    (env.alice / "notes.md").write_text("# Garden\n\nLocal pelican notes.\n", encoding="utf-8")
    fake = drive.link("alice")
    _work_drive(fake)
    drive.enable("alice")
    _start(env, drive, "alice", local=("alice",))
    rt = _settled(env, "alice", lambda rt: _indexed("a", "b", "d")(rt) and "notes.md" in _local(rt))

    rows = _rows(rt)
    assert {k: r["rel_path"] for k, r in rows.items()} == {
        "a": "Drive/Work/report.txt", "b": "Drive/loose.txt", "d": "Drive/Plan"}
    assert all(r["status"] == "indexed" and r["source"] == "drive" for r in rows.values())
    assert rows["a"]["drive_web_link"] == "https://drive.google.com/file/d/a/view"
    # Content went through the real extractors, from memory.
    assert {"report.txt", "loose.txt", "Plan.md"} <= set(env.extracted)
    assert _local(rt)["notes.md"]["status"] == "indexed"
    assert _collection_count(env, rt) == rt.db.stats()["chunks"]
    acts = rt.db.list_activity(limit=50)
    assert any(a["source"] == "drive" and a["kind"] == "added" and "report.txt" in a["message"] for a in acts)

    # Both sources answer one search; the source filter picks one.
    assert "report.txt" in _names("alice", "zebracorn")
    assert "Plan" in _names("alice", "narwhal")
    assert "notes.md" in _names("alice", "pelican")
    assert _names("alice", "zebracorn", source="local") == []
    assert _names("alice", "pelican", source="drive") == []

    snap = uds_state.build_snapshot("alice")
    assert snap["enabled"] and snap["state"] == "idle"
    assert snap["drive"]["state"] == "idle" and snap["drive"]["counts"]["indexed"] == 3
    assert snap["stages"]["indexed"] == 4  # both sources

    # A file created later arrives through the change feed.
    fake.put("e", "late.txt", parents=["F"], data=b"Late arriving okapi memo.")
    _sync(env, "alice")
    _settled(env, "alice", _indexed("e"))
    assert _rows(rt)["e"]["rel_path"] == "Drive/Work/late.txt"
    assert "late.txt" in _names("alice", "okapi")


def test_a_rename_recards_without_downloading_again(env, drive):
    fake = drive.link("alice")
    _work_drive(fake)
    drive.enable("alice")
    _start(env, drive, "alice")
    rt = _settled(env, "alice", _indexed("a", "b", "d"))
    a = _rows(rt)["a"]
    body = _body_ids(rt, int(a["id"]))
    fetched, extracted, embedded = len(fake.called("fetch")), len(env.extracted), env.embedder.passages

    fake.rename("a", "annual.txt")
    _sync(env, "alice")
    _settled(env, "alice", lambda rt: _rows(rt)["a"]["rel_path"] == "Drive/Work/annual.txt")

    row = _rows(rt)["a"]
    assert row["cite_id"] == a["cite_id"] and row["status"] == "indexed"
    assert _body_ids(rt, int(row["id"])) == body
    card = next(r for r in rt.db.chunks_of_file(int(row["id"])) if r["ctype"] == t.CTYPE_FILE_CARD)
    assert "Drive/Work/annual.txt" in card["text"]
    assert len(fake.called("fetch")) == fetched and len(env.extracted) == extracted
    assert env.embedder.passages - embedded == 1  # the new card, nothing else
    assert "annual.txt" in _names("alice", "annual")


def test_editing_a_few_lines_reembeds_only_the_changed_chunks(env, drive):
    fake = drive.link("alice")
    paras = [_para(i) for i in range(80)]
    fake.put("long", "long.md", data=("# Report\n\n" + "\n\n".join(paras)).encode())
    drive.enable("alice")
    _start(env, drive, "alice")
    rt = _settled(env, "alice", _indexed("long"))
    fid = int(_rows(rt)["long"]["id"])
    before = {c.text_hash: c.id for c in rt.db.get_chunks(fid)}
    assert len(before) > 10
    embedded = env.embedder.passages

    paras[40] = paras[40].replace("word40_3", "EDITED")
    fake.edit("long", ("# Report\n\n" + "\n\n".join(paras)).encode())
    _sync(env, "alice")
    _settled(env, "alice", lambda rt: any(c.text_hash not in before for c in rt.db.get_chunks(fid)))

    after = {c.text_hash: c.id for c in rt.db.get_chunks(fid)}
    assert 1 <= len(set(after) - set(before)) <= 3
    assert all(after[h] == before[h] for h in set(after) & set(before))
    assert env.embedder.passages - embedded <= 3
    msgs = [a["message"] for a in rt.db.list_activity(limit=20)]
    assert any("chunks re-embedded" in m and "long.md" in m for m in msgs), msgs


def test_trashing_a_file_removes_it_and_its_vectors(env, drive):
    fake = drive.link("alice")
    _work_drive(fake)
    drive.enable("alice")
    _start(env, drive, "alice")
    rt = _settled(env, "alice", _indexed("a", "b", "d"))
    assert "loose.txt" in _names("alice", "marmoset")

    fake.trash("b")
    _sync(env, "alice")
    _settled(env, "alice", lambda rt: "b" not in _rows(rt))
    _wait(lambda: _collection_count(env, rt) == rt.db.stats()["chunks"])
    assert "loose.txt" not in _names("alice", "marmoset")


def test_many_files_vanishing_at_once_are_held_until_confirmed(env, drive, monkeypatch):
    from app.userdocs.discovery.guard import RootGuard

    # The real floor is 200; 20 exercises the same path quickly.
    monkeypatch.setattr(RootGuard, "MASS_DELETE_MIN_FILES", 20)
    fake = drive.link("alice")
    for i in range(30):
        fake.put(f"n{i:03}", f"n{i:03}.txt", data=f"note {i} capybara{i:03} ".encode() + _para(i, 8).encode())
    drive.enable("alice")
    _start(env, drive, "alice")
    rt = _settled(env, "alice", _indexed(*(f"n{i:03}" for i in range(30))), timeout=120)

    for i in range(20):
        fake.delete(f"n{i:03}", log=False)  # gone, and the feed never said so
    _sync(env, "alice", full=True)
    _wait(lambda: (rt.drive.view()["confirmation"] or {}).get("kind") == "mass_delete")
    view = rt.drive.view()
    assert view["state"] == "awaiting_confirmation"
    assert view["confirmation"] == {"kind": "mass_delete", "source": "drive", "missing": 20, "total": 30}
    assert rt.db.count_by_status("drive") == {"missing": 20, "indexed": 10}
    # Hidden from search while the user decides, and every answer says so.
    assert _names("alice", "capybara005") == []
    notes = _engine("alice").search("capybara025", expand=False).notes
    assert any("Many Google Drive files vanished" in n for n in notes), notes

    env.svc.control("alice", "confirm_deletions", source="drive")
    _wait(lambda: rt.db.count_by_status("drive") == {"indexed": 10})
    assert rt.drive.view()["confirmation"] is None
    assert "n025.txt" in _names("alice", "capybara025")


# ── holds ───────────────────────────────────────────────────────────────────


def test_revoked_access_hides_drive_then_purges_it_after_seven_days(env, drive):
    (env.alice / "notes.md").write_text("# Garden\n\nLocal pelican notes.\n", encoding="utf-8")
    fake = drive.link("alice")
    _work_drive(fake)
    drive.enable("alice")
    _start(env, drive, "alice", local=("alice",))
    rt = _settled(env, "alice", lambda rt: _indexed("a", "b", "d")(rt) and "notes.md" in _local(rt))

    fake.down("auth_revoked")
    _sync(env, "alice")
    _wait(lambda: rt.drive.view()["reason"] == "auth_revoked")
    view = rt.drive.view()
    assert view["state"] == "hold"
    purge_at = view["detail"]["purge_at"]
    assert purge_at and abs(purge_at / 1000 - (drive.clock() + 7 * DAY)) < 120
    # Hidden at once — from search and from the header counts — with the day
    # the index goes; nothing is deleted yet.
    assert _names("alice", "zebracorn") == []
    out = _engine("alice").search("pelican", expand=False)
    assert {g.file["name"] for g in out.groups} == {"notes.md"}  # vectors cannot reach Drive either
    assert out.overview["files"] == 1
    assert any("hidden until Google is re-linked" in n and "removed on" in n for n in out.notes), out.notes
    assert len(_rows(rt)) == 3

    # The local folder keeps working while Drive is held.
    (env.alice / "more.md").write_text("# More\n\nAnother flamingo note.\n", encoding="utf-8")
    _settled(env, "alice", lambda rt: _local(rt).get("more.md", {}).get("status") == "indexed")
    assert "more.md" in _names("alice", "flamingo")

    # A retry within the week never resets the timer.
    drive.clock.advance(3 * DAY)
    _synced_once_more(env, drive, "alice")
    assert rt.drive.view()["detail"]["purge_at"] == purge_at and len(_rows(rt)) == 3

    # A week after the first confirmation, re-confirmed: the Drive index goes.
    drive.clock.advance(4 * DAY + 3600)
    about = len(fake.called("about"))
    _sync(env, "alice")
    _wait(lambda: not _rows(rt))
    assert len(fake.called("about")) > about  # confirmed afresh before deleting
    assert drive.citations == ["alice"]
    view = rt.drive.view()
    assert view["state"] == "hold" and view["reason"] == "auth_revoked" and view["detail"]["purged_at"]
    _wait(lambda: _collection_count(env, rt) == rt.db.stats()["chunks"])
    # The folder's index is untouched.
    assert {"notes.md", "more.md"} <= set(_local(rt))
    assert "notes.md" in _names("alice", "pelican")


def test_revocation_is_reconfirmed_before_the_purge(env, drive):
    fake = drive.link("alice")
    _work_drive(fake)
    drive.enable("alice")
    _start(env, drive, "alice")
    rt = _settled(env, "alice", _indexed("a", "b", "d"))

    fake.down("auth_revoked")
    _sync(env, "alice")
    _wait(lambda: rt.drive.view()["reason"] == "auth_revoked")

    # Access came back before anyone looked again: past the date, the purge
    # finds Google answering, keeps everything and lifts the hold.
    fake.up()
    drive.clock.advance(8 * DAY)
    _sync(env, "alice")
    _settled(env, "alice", lambda rt: rt.drive.view()["state"] == "idle")
    assert len(_rows(rt)) == 3 and drive.citations == []
    assert rt.db.get_source_state("drive")["state"] == "live"
    assert "report.txt" in _names("alice", "zebracorn")


def test_an_unreachable_drive_is_never_purged_and_recovers_by_itself(env, drive):
    fake = drive.link("alice")
    _work_drive(fake)
    drive.enable("alice")
    _start(env, drive, "alice")
    rt = _settled(env, "alice", _indexed("a", "b", "d"))

    fake.down("unreachable")
    _sync(env, "alice")
    _wait(lambda: rt.drive.view()["reason"] == "drive_unreachable")
    assert rt.drive.view()["detail"]["purge_at"] is None
    # Still searchable, flagged as possibly out of date.
    out = _engine("alice").search("zebracorn", expand=False)
    assert "report.txt" in [g.file["name"] for g in out.groups]
    assert any("can't be reached" in n for n in out.notes), out.notes

    drive.clock.advance(30 * DAY)
    _synced_once_more(env, drive, "alice")
    assert len(_rows(rt)) == 3 and drive.citations == []
    assert rt.drive.view()["reason"] == "drive_unreachable"

    fake.up()
    _sync(env, "alice")
    _settled(env, "alice", lambda rt: rt.drive.view()["state"] == "idle")
    assert rt.db.get_source_state("drive")["state"] == "live"
    assert len(_rows(rt)) == 3


def test_another_google_account_purges_and_reindexes(env, drive):
    fake = drive.link("alice")
    _work_drive(fake)
    drive.enable("alice")
    _start(env, drive, "alice")
    rt = _settled(env, "alice", _indexed("a", "b", "d"))
    old_ids = {r["id"] for r in _rows(rt).values()}

    # The skill is re-linked to someone else's Google account.
    for fid in list(fake.items):
        fake.delete(fid, log=False)
    fake.ident = "cid|someone-else"
    fake.put("z", "their.txt", data=b"Their own quokka file.")
    _sync(env, "alice")
    _settled(env, "alice", lambda rt: set(_rows(rt)) == {"z"} and _indexed("z")(rt))

    assert drive.forgot == ["alice"]  # the old access token was dropped first
    assert drive.citations == ["alice"]
    assert not old_ids & {r["id"] for r in _rows(rt).values()}
    assert rt.db.get_source_state("drive")["drive_account_key"] == "cid|someone-else"
    msgs = [a["message"] for a in rt.db.list_activity(limit=50)]
    assert "Drive account changed — re-indexing" in msgs
    assert _names("alice", "zebracorn") == [] and "their.txt" in _names("alice", "quokka")


def test_unlinking_google_suspends_and_purges_drive_only(env, drive):
    (env.alice / "notes.md").write_text("# Garden\n\nLocal pelican notes.\n", encoding="utf-8")
    fake = drive.link("alice")
    _work_drive(fake)
    drive.enable("alice")
    _start(env, drive, "alice", local=("alice",))
    rt = _settled(env, "alice", lambda rt: _indexed("a", "b", "d")(rt) and "notes.md" in _local(rt))

    # What the Google unlink through Cremind does (app.google.unlink / api).
    drive.unlink("alice")
    assert uds_state.suspend_drive("alice") is True
    assert uds_state.request_purge("alice", "drive") is True
    _wait(lambda: not _rows(rt) and rt.drive.view()["reason"] == "drive_unlinked")
    assert drive.citations == ["alice"]
    assert rt.drive.view()["detail"]["purge_at"] is None  # nothing left to time
    assert "notes.md" in _names("alice", "pelican")
    assert os.path.exists(rt.db.path)

    # Linked again: Drive comes back by itself on the next try.
    drive.link("alice")
    _sync(env, "alice")
    _settled(env, "alice", _indexed("a", "b", "d"))
    assert rt.drive.view()["state"] == "idle"
    assert "report.txt" in _names("alice", "zebracorn")


# ── profiles ────────────────────────────────────────────────────────────────


def test_a_drive_only_profile_searches_and_gets_the_tool(env, drive):
    from app.userdocs import gate
    from app.userdocs.index import index_path

    gate.clear_cache()
    fake = drive.link("bob")
    _work_drive(fake)
    drive.enable("bob")  # no local folder at all
    _start(env, drive, "bob")
    rt = _settled(env, "bob", _indexed("a", "b", "d"))

    assert env.storage.get_source("bob", "local") is None and rt.active is False
    snap = uds_state.build_snapshot("bob")
    assert snap["enabled"] is True and snap["state"] == "idle" and snap["tool_mode"] == "normal"
    assert snap["drive"]["state"] == "idle"
    assert "report.txt" in _names("bob", "zebracorn")
    assert gate.userdocs_tool_available("bob", None) is True
    assert gate.userdocs_tool_available("bob", {"source": "channel"}) is False

    # Drive off: its index goes (never the whole profile's file), the index
    # closes with nothing left on, and the tool is gone.
    env.storage.upsert_source("bob", "drive", enabled=False)
    uds_state.request_purge("bob", "drive")
    uds_state.notify_settings_changed("bob", "drive")
    _wait(lambda: rt.db is None)
    assert os.path.exists(index_path(rt.uid))
    assert rt.ensure_db().count_by_status("drive") == {}
    assert drive.citations == ["bob"]
    assert uds_state.build_snapshot("bob")["enabled"] is False
    assert gate.userdocs_tool_available("bob", None) is False


def test_two_profiles_drives_are_isolated(env, drive):
    a = drive.link("alice")
    a.put("x", "alice-plan.txt", data=b"Alice zebracorn plan.")
    b = drive.link("bob")
    b.put("x", "bob-notes.txt", data=b"Bob marmoset notes.")
    drive.enable("alice")
    drive.enable("bob")
    _start(env, drive, "alice", "bob")
    ra = _settled(env, "alice", _indexed("x"))
    rb = _settled(env, "bob", _indexed("x"))

    assert _rows(ra)["x"]["name"] == "alice-plan.txt" and _rows(rb)["x"]["name"] == "bob-notes.txt"
    assert ra.db.path != rb.db.path
    assert _names("alice", "marmoset") == [] and _names("bob", "zebracorn") == []

    # Bob's revoked access hides Bob's Drive only; purging it leaves Alice's.
    b.down("auth_revoked")
    _sync(env, "bob")
    _wait(lambda: rb.drive.view()["reason"] == "auth_revoked")
    assert ra.drive.view()["state"] == "idle"
    assert "alice-plan.txt" in _names("alice", "zebracorn")
    uds_state.request_purge("bob", "drive")
    _wait(lambda: not _rows(rb))
    assert drive.citations == ["bob"] and set(_rows(ra)) == {"x"}
    assert "alice-plan.txt" in _names("alice", "zebracorn")


# ── controls ────────────────────────────────────────────────────────────────


def test_reindex_and_retry_take_drive_paths_and_ids(env, drive):
    from .test_drive_source import FakeError

    fake = drive.link("alice")
    _work_drive(fake)
    fake.put("a2", "second.txt", parents=["F"], data=b"Second work file.")
    drive.enable("alice")
    _start(env, drive, "alice")
    _settled(env, "alice", _indexed("a", "a2", "b", "d"))

    # A display path takes its subtree ("Drive/" may be left out).
    before = len(env.extracted)
    out = env.svc.control("alice", "reindex", targets=["Work"], source="drive")
    assert out["files"] == 2
    _settled(env, "alice", lambda rt: len(env.extracted) >= before + 2)
    assert sorted(env.extracted[before:]) == ["report.txt", "second.txt"]

    # A Drive file id works too; a failure is retried per source.
    fake.fail_ids[("fetch", "b")] = FakeError("http", "boom")
    assert env.svc.control("alice", "reindex", targets=["b"], source="drive")["files"] == 1
    _settled(env, "alice", lambda rt: _rows(rt)["b"]["status"] == "error")
    del fake.fail_ids[("fetch", "b")]
    assert env.svc.control("alice", "retry_failed", source="local")["files"] == 0
    assert env.svc.control("alice", "retry_failed", source="drive")["files"] == 1
    _settled(env, "alice", _indexed("b"))

    from app.userdocs.errors import NotEnabled, UnknownAction

    with pytest.raises(UnknownAction):
        env.svc.control("alice", "sync_now", source="dropbox")
    env.storage.upsert_source("bob", "local", enabled=False)
    with pytest.raises(NotEnabled):
        env.svc.control("bob", "sync_now", source="drive")


# ── images ──────────────────────────────────────────────────────────────────


def test_drive_photos_are_captioned_from_memory_and_rechecked_by_download(env, drive, vision, monkeypatch, tmp_path):
    from app.userdocs import runtime as rt_module
    from app.userdocs.vision import captioner

    from .test_drive_source import FakeError
    from .test_engine_vision_e2e import FakeVisionLLM, _photo

    monkeypatch.setattr(rt_module.ProfileRuntime, "caption_cap", lambda self: 50)
    monkeypatch.setattr(captioner, "daily_cap", lambda options: 50)
    _photo(tmp_path / "red.jpg", (220, 20, 20))
    _photo(tmp_path / "blue.jpg", (20, 20, 220))
    fake = drive.link("alice")
    fake.put("r", "red.jpg", mime="image/jpeg", data=(tmp_path / "red.jpg").read_bytes())
    fake.put("u", "blue.jpg", mime="image/jpeg", data=(tmp_path / "blue.jpg").read_bytes())
    drive.enable("alice")
    # Consent lives on the profile's local row, even with the folder off.
    env.svc.control("alice", "consent_vision", model="openai/gpt-4o")
    _start(env, drive, "alice")
    rt = _settled(env, "alice", lambda rt: {r["caption_state"] for r in _rows(rt).values()} == {"done"})

    # Captioned from the bytes already downloaded for indexing.
    assert FakeVisionLLM.calls == 2 and len(fake.called("fetch")) == 2
    assert all(r["is_camera_photo"] == 1 and r["taken_at"] for r in _rows(rt).values())
    assert rt.db.fts_search('"puppies"', limit=5)

    engine = _engine("alice")
    ask = dict(filters={"types": ["image"]}, image_objects=[{"label": "puppy", "count": 2}], expand=False)
    checked = engine.search("two puppies in a park", verify_images=True, **ask)
    assert FakeVisionLLM.checks == 2 and len(fake.called("fetch")) == 4
    assert [g.file["name"] for g in checked.groups] == ["blue.jpg", "red.jpg"]
    assert any("2 image(s) re-checked" in n for n in checked.notes)

    # A photo Drive will not hand over is skipped, and the answer says so.
    fake.fail_ids[("fetch", "r")] = FakeError("not_downloadable")
    again = _engine("alice").search("two puppies in a park", verify_images=True, **ask)
    assert FakeVisionLLM.checks == 3
    assert any("skipped for 1 Google Drive image" in n for n in again.notes), again.notes


# ── settings ────────────────────────────────────────────────────────────────


def test_narrowing_the_drive_folders_is_counted_then_applied(env, drive):
    fake = drive.link("alice")
    fake.folder("F", "Work")
    fake.folder("G", "Home")
    fake.put("a", "a.txt", parents=["F"], data=b"Work walrus memo.")
    fake.put("h1", "h1.txt", parents=["G"], data=b"Home heron list.")
    fake.put("h2", "h2.txt", parents=["G"], data=b"Home ibis list.")
    drive.enable("alice", include_folders=["F", "G"])
    _start(env, drive, "alice")
    _settled(env, "alice", _indexed("a", "h1", "h2"))

    cur = env.storage.get_source("alice", "drive")
    narrow = {"options": {**cur["options"], "include_folders": ["F"]}}
    plan = uds.plan_source_change("alice", "drive", cur, narrow)
    assert plan.destructive
    assert [(e["kind"], e["files"]) for e in plan.to_dict()["effects"]] == [("purge_out_of_scope", 2)]
    widen = {"options": {**cur["options"], "include_folders": ["F", "G", "K"]}}
    assert uds.plan_source_change("alice", "drive", cur, widen).effects == []

    env.storage.upsert_source("alice", "drive", options=narrow["options"])
    uds_state.notify_settings_changed("alice", "drive")
    _settled(env, "alice", lambda rt: set(_rows(rt)) == {"a"})
    assert _names("alice", "heron") == [] and "a.txt" in _names("alice", "walrus")


# ── the pieces, without an engine ──────────────────────────────────────────


def test_suspend_drive_is_a_no_op_without_an_engine(monkeypatch):
    monkeypatch.setattr(uds_state, "_drive_suspend_handler", None)
    assert uds_state.suspend_drive("alice") is False
    seen: list[str] = []
    monkeypatch.setattr(uds_state, "_drive_suspend_handler", seen.append)
    assert uds_state.suspend_drive("alice") is True and seen == ["alice"]


def test_drive_notes_say_what_a_drive_hold_means_for_an_answer():
    import datetime as dt

    from app.userdocs.query.engine import drive_notes, status_notes

    utc = dt.timezone.utc
    purge = dt.datetime(2026, 10, 2, 12, tzinfo=utc).timestamp() * 1000
    hold = {"enabled": True, "state": "hold", "reason": "auth_revoked", "detail": {"purge_at": purge}}
    assert drive_notes(hold, utc) == [
        "Google Drive results are hidden until Google is re-linked (access was revoked); "
        "the Drive index is removed on 2026-10-02."]
    unlinked = {**hold, "reason": "drive_unlinked", "detail": {"purge_at": None, "purged_at": purge}}
    assert "Drive index was removed" in drive_notes(unlinked)[0]
    assert "no longer linked" in drive_notes(unlinked)[0]
    down = {"enabled": True, "state": "hold", "reason": "drive_unreachable", "detail": {"kind": "unreachable"}}
    assert "may be out of date" in drive_notes(down)[0]
    many = {**down, "detail": {"why": "many_unavailable"}}
    assert "Many Google Drive files became unavailable" in drive_notes(many)[0]
    odd = {"enabled": True, "state": "hold", "reason": "drive_misconfigured", "detail": {"why": "folders_required"}}
    assert drive_notes(odd) == ["Google Drive syncing is on hold (folders_required): Drive results may be out of date."]
    held = {"enabled": True, "state": "awaiting_confirmation", "reason": "mass_delete"}
    assert "vanished" in drive_notes(held)[0]
    assert drive_notes({"enabled": True, "state": "idle"}) == []
    assert drive_notes({"enabled": False, "state": "disabled"}) == []
    # The folder's own notes are unchanged, and the Drive one follows them.
    notes = status_notes({"state": "paused", "reason": "user", "drive": down})
    assert notes[0].startswith("Syncing is paused") and "can't be reached" in notes[1]


def test_first_seen_counts_from_each_sources_own_first_sync():
    import datetime as dt

    from app.userdocs.query import filters as F

    w = F.make_window(dt.date(2026, 9, 1), dt.date(2026, 9, 30), dt.timezone.utc)
    inside = w.start + 86400
    cut = {"local": inside - 10, "drive": inside + 10}
    local = {"source": "local", "first_seen_at": inside}
    drive_row = {"source": "drive", "first_seen_at": inside}
    assert F.matched_date(local, "any", w, cut) == ("first_seen_at", inside)
    assert F.matched_date(drive_row, "any", w, cut) is None  # still Drive's first sync
    assert F.matched_date(local, "any", w, inside - 10) == ("first_seen_at", inside)  # a float still works

    f = F.parse_filters({"date_from": "2026-09-01", "date_to": "2026-09-30"})
    where, params = F.file_conditions(f, F.Scope(window=w, first_seen_cutoff=cut))
    sql = " AND ".join(where)
    assert sql.count("source = ? AND first_seen_at > ?") == 2
    assert "drive" in params and "local" in params


def test_narrowing_include_folders_is_destructive_even_without_counts(monkeypatch):
    monkeypatch.setattr(uds, "_effect_counter", None)
    cur = {"enabled": True, "updated_at": 1, "options": {"include_folders": ["F", "G"]}}
    narrowed = uds.plan_source_change("p", "drive", cur, {"options": {"include_folders": ["F"]}})
    assert [e.kind for e in narrowed.effects] == ["purge_out_of_scope"] and narrowed.destructive
    same = uds.plan_source_change("p", "drive", cur, {"options": {"include_folders": ["G", "F"]}})
    assert same.effects == []
    # A chosen set in place of "everything granted" narrows too.
    everything = {**cur, "options": {"include_folders": []}}
    assert uds.plan_source_change("p", "drive", everything, {"options": {"include_folders": ["F"]}}).destructive
    # Turning Drive off is one purge, not two.
    off = uds.plan_source_change("p", "drive", cur, {"enabled": False, "options": {"include_folders": ["F"]}})
    assert [e.kind for e in off.effects] == ["purge_drive"]
