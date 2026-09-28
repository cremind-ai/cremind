"""Moving gateway workers from Cremind Connect into Cremind (app/tags/hosting/migration.py).

A worker of THIS server (its recorded authority id) moves with everything
it holds — controller key, credentials, the delivery queue's database — and
nothing is made anew; another server's worker is left running in Connect.
Every step is journaled: a failure before Cremind adopts the worker puts
Connect back exactly as it was, a crash after it resumes, and Connect is
retired only once none of its workers is left enabled.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("cbor2")

from app.tags.hosting.migration import Journal, migrate, migrate_worker, summary  # noqa: E402
from app.tags.hosting.paths import RuntimePaths  # noqa: E402
from app.tags.runtime.connect.setup_flow import CONTROLLER_SCHEMA, read_controller_key, write_private  # noqa: E402
from app.tags.runtime.connect.workerdir import WorkerSpec, load_worker, write_worker  # noqa: E402
from app.tags.runtime.secrets import FileBackend, SecretStore  # noqa: E402
from app.tags.runtime.secure import identity  # noqa: E402

OURS = "a1" * 16
THEIRS = "b2" * 16
HW = "tagc_hardware0001." + "H" * 43
CT = "tagc_content00001." + "C" * 43


def make_worker(workers_dir: Path, worker_id: str, companion_id: str, authority_id: str) -> Path:
    directory = workers_dir / worker_id
    directory.mkdir(parents=True)
    private, _public = identity.x25519_generate()
    write_private(directory / "controller.key",
                  json.dumps({"schema": CONTROLLER_SCHEMA, "private_key": private.hex()}) + "\n")
    store = SecretStore(FileBackend(directory / "secrets.json"))
    store.set_credential("hardware", HW)
    store.set_credential("content", CT)
    write_worker(directory, WorkerSpec(worker_id, "https://cremind.example.org", "anna", companion_id, "c3" * 16,
                                       True, extra={"profile_id": "pid-anna", "authority_id": authority_id,
                                                    "authority_pub": "d4" * 32, "installation_id": "e5" * 16,
                                                    "gateway_ik": "f6" * 32}))
    db = sqlite3.connect(directory / "companion.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY, card TEXT)")
    db.executemany("INSERT INTO queue (card) VALUES (?)", [("pinned note",), ("weather",)])
    db.commit()
    db.close()
    return directory


class FakeConnect:
    """Connect's side: its service over IPC (faked) and its directory."""

    def __init__(self, root: Path, stops_after: int = 1) -> None:
        self.paths = SimpleNamespace(workers_dir=root / "workers", service_lock=root / "service.lock")
        self.paths.workers_dir.mkdir(parents=True)
        self.calls: list[tuple[str, str]] = []
        self.polls = 0
        self.stops_after = stops_after

    def disable(self, worker_id: str) -> None:
        self.calls.append(("disable", worker_id))

    def enable(self, worker_id: str) -> None:
        self.calls.append(("enable", worker_id))

    def running(self, worker_id: str) -> bool:
        self.polls += 1
        return self.polls <= self.stops_after

    def retire(self) -> None:
        self.calls.append(("retire", ""))


async def _no_wait(_seconds: float) -> None:
    return None


def adopter(calls: list[str], fail: bool = False):
    async def adopt(companion_id: str) -> dict:
        calls.append(companion_id)
        if fail:
            raise RuntimeError("connection_not_found")
        return {"companion_id": companion_id, "generation": 3, "profile": {"name": "anna", "id": "pid-anna"}}
    return adopt


def run_migration(runtime, connect, adopt, started=None, **kwargs):
    return asyncio.run(migrate(runtime, connect.paths, authority_id=OURS, host_id="host-srv", adopt=adopt,
                               service=connect, start=(started.append if started is not None else None),
                               sleep=_no_wait, **kwargs))


def test_a_worker_of_this_server_moves_with_everything_and_others_stay(tmp_path) -> None:
    runtime = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    connect = FakeConnect(tmp_path / "connect", stops_after=2)
    source = make_worker(connect.paths.workers_dir, "w-ours", "comp-1", OURS)
    other = make_worker(connect.paths.workers_dir, "w-theirs", "comp-9", THEIRS)
    key_before = read_controller_key(source)
    adopted: list[str] = []
    started: list[str] = []

    [outcome] = run_migration(runtime, connect, adopter(adopted), started)
    assert outcome.as_json() == {"worker_id": "w-ours", "companion_id": "comp-1", "state": "complete", "detail": ""}
    assert adopted == ["comp-1"] and started == ["w-ours"]
    assert connect.calls == [("disable", "w-ours")], "only this server's worker was touched; Connect not retired"

    moved = runtime.workers_dir / "w-ours"
    spec = load_worker(moved) if (moved / "worker.json").exists() else None
    assert spec is not None and spec.worker_id == "w-ours"
    assert spec.enabled, "Connect's copy is disabled; the one Cremind runs is not"
    assert spec.extra["host_id"] == "host-srv" and spec.extra["generation"] == 3
    assert spec.extra["migrated_from"] == "cremind-connect"
    assert read_controller_key(moved) == key_before, "the same controller key: nothing is paired again"
    store = SecretStore(FileBackend(moved / "secrets.json"))
    assert (store.get_credential("hardware"), store.get_credential("content")) == (HW, CT)
    db = sqlite3.connect(moved / "companion.sqlite3")
    assert [r[0] for r in db.execute("SELECT card FROM queue ORDER BY id")] == ["pinned note", "weather"]
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()

    left = load_worker(source)
    assert left.enabled is False and left.extra["migrated_to"] == "cremind", "Connect's copy stays, disabled"
    assert (source / "controller.key").exists(), "Cremind never deletes Connect's copy"
    assert load_worker(other).enabled is True, "another server's worker keeps running in Connect"
    journal = json.loads((runtime.migration_dir / "w-ours.json").read_text(encoding="utf-8"))
    assert journal["state"] == "complete"
    assert [s["step"] for s in journal["steps"]] == ["detected", "validated", "disabled", "stopped", "copied",
                                                    "verified", "adopted", "published", "started"]
    assert summary(runtime) == {"moved": 1, "failed": 0, "rolled_back": 0}


def test_connect_is_retired_once_nothing_is_left_in_it(tmp_path) -> None:
    runtime = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    connect = FakeConnect(tmp_path / "connect")
    make_worker(connect.paths.workers_dir, "w-1", "comp-1", OURS)
    make_worker(connect.paths.workers_dir, "w-2", "comp-2", OURS)
    outcomes = run_migration(runtime, connect, adopter([]))
    assert [o.state for o in outcomes] == ["complete", "complete"]
    assert connect.calls[-1] == ("retire", "")


def test_a_refusal_before_adoption_puts_connect_back_as_it_was(tmp_path) -> None:
    runtime = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    connect = FakeConnect(tmp_path / "connect")
    source = make_worker(connect.paths.workers_dir, "w-1", "comp-1", OURS)
    [outcome] = run_migration(runtime, connect, adopter([], fail=True))
    assert outcome.state == "rolled_back" and "did not take it over" in outcome.detail
    assert load_worker(source).enabled is True and "migrated_to" not in load_worker(source).extra
    assert connect.calls == [("disable", "w-1"), ("enable", "w-1")]
    assert not (runtime.workers_dir / "w-1").exists() and not (runtime.workers_dir / ".migrating-w-1").exists()
    assert json.loads((runtime.migration_dir / "w-1.json").read_text(encoding="utf-8"))["state"] == "rolled_back"


def test_a_broken_worker_is_left_alone(tmp_path) -> None:
    runtime = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    connect = FakeConnect(tmp_path / "connect")
    source = make_worker(connect.paths.workers_dir, "w-1", "comp-1", OURS)
    (source / "controller.key").unlink()
    [outcome] = run_migration(runtime, connect, adopter([]))
    assert outcome.state == "rolled_back" and "controller key" in outcome.detail
    assert connect.calls == [], "validated before anything changed"
    assert load_worker(source).enabled is True


def test_a_worker_connect_does_not_stop_stays_there(tmp_path) -> None:
    runtime = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    connect = FakeConnect(tmp_path / "connect", stops_after=10_000)
    source = make_worker(connect.paths.workers_dir, "w-1", "comp-1", OURS)
    [outcome] = run_migration(runtime, connect, adopter([]), stop_timeout_s=0.0)
    assert outcome.state == "rolled_back" and "did not stop" in outcome.detail
    assert load_worker(source).enabled is True and connect.calls[-1] == ("enable", "w-1")


def test_a_crash_after_adoption_resumes(tmp_path) -> None:
    runtime = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    connect = FakeConnect(tmp_path / "connect")
    source = make_worker(connect.paths.workers_dir, "w-1", "comp-1", OURS)
    spec = load_worker(source)
    # The previous start got as far as "adopted" (Connect's copy disabled), then the copy was lost.
    journal = Journal.open(runtime, "w-1", source)
    for step in ("detected", "validated", "disabled", "stopped", "copied", "verified"):
        journal.step(step)
    journal.step("adopted", generation=5, profile_id="pid-anna")
    write_worker(source, WorkerSpec(spec.worker_id, spec.server_origin, spec.profile, spec.companion_id,
                                    spec.gateway_device_id, False, extra=spec.extra))
    adopted: list[str] = []
    outcome = asyncio.run(migrate_worker(runtime, source, spec, host_id="host-srv", adopt=adopter(adopted),
                                         service=connect, sleep=_no_wait))
    assert outcome.state == "complete" and adopted == [], "not adopted twice"
    moved = load_worker(runtime.workers_dir / "w-1")
    assert moved.extra["generation"] == 5 and moved.extra["host_id"] == "host-srv"
    assert moved.enabled, "copied again from Connect's disabled directory, and runnable here"


def test_a_moved_worker_enabled_again_in_connect_is_disabled_again(tmp_path) -> None:
    runtime = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    connect = FakeConnect(tmp_path / "connect")
    source = make_worker(connect.paths.workers_dir, "w-1", "comp-1", OURS)
    run_migration(runtime, connect, adopter([]))
    spec = load_worker(source)
    write_worker(source, WorkerSpec(spec.worker_id, spec.server_origin, spec.profile, spec.companion_id,
                                    spec.gateway_device_id, True, extra=spec.extra))  # someone re-enabled it
    connect.calls.clear()
    [outcome] = run_migration(runtime, connect, adopter([]))
    assert outcome.detail == "already moved" and load_worker(source).enabled is False
    assert ("disable", "w-1") in connect.calls
