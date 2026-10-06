"""Incremental reader for Claude Code session transcripts (``<profile>/projects/**/*.jsonl``).

Every session writes one — terminal and VS Code alike — so this is the live signal for
sessions that never run a status line. Only timestamps, model ids, token counts and
usage-limit notices are read; message content is never inspected or stored.

There is no file watcher (the skill runs on the standard library alone), so the reader
polls in three layers, cheapest first:

- every poll, the files that grew in the last half hour (the live sessions);
- every few seconds, the folders whose modification time changed — a new session or
  sub-agent transcript is a new file, which touches its folder;
- every minute, the whole tree, which also catches an old session being resumed.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

from .pricing import reply_cost

RESCAN_MS = float(os.environ.get("CLAUDE_USAGE_MONITOR_RESCAN_MS") or 60_000)  # whole-tree safety net
DISCOVER_MS = min(5_000.0, RESCAN_MS)  # folders whose mtime changed
HOT_MS = 30 * 60_000  # files read this recently are checked on every poll
READ_BUDGET = 48 * 1024 * 1024  # bytes per poll, so a large backlog never stalls a tick
SEEN_MAX = 20_000
STALE_MS = 8 * 24 * 3600_000  # offsets of files idle this long are forgotten
# A folder changed this close to being listed may change again within the same clock
# tick without its mtime moving, so it is listed again until it has been quiet this long.
RACY_NS = 2_000_000_000


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


class TranscriptTail:
    """Reads what each transcript gained since the last poll.

    ``store`` is the persisted ``{"since", "offsets"}`` of one profile (mutated in
    place); ``seen`` holds the reply ids already counted, shared by all profiles.
    """

    def __init__(self, root: str, store: dict, seen: dict[str, None]) -> None:
        self.root = root
        self.store = store
        self.store.setdefault("offsets", {})
        self.seen = seen  # an insertion-ordered set: a dict with None values
        self.dirty: set[str] = set()
        self.hot: dict[str, float] = {}  # rel path -> when it last grew
        self.dirs: dict[str, tuple[int, int]] = {}  # folder (rel, "" = root) -> (mtime_ns, listed at ns)
        self.last_scan = 0.0
        self.last_discover = 0.0
        self.caught_up = False
        self.changed = False

    def close(self) -> None:
        """Nothing to release; kept so a profile swap reads like the original."""

    # ------------------------------------------------------------ finding work

    def _abs(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def _consider(self, rel: str, size: int, mtime_ms: float) -> None:
        off = self.store["offsets"].get(rel)
        if off is None and mtime_ms < (self.store.get("since") or 0):
            return  # untouched since tracking began
        if off != size:
            self.dirty.add(rel)

    def _list(self, rel_dir: str, recurse: bool) -> None:
        """List one folder: note its mtime, consider its transcripts, and descend into
        sub-folders (all of them when ``recurse``, otherwise only new ones)."""
        path = self._abs(rel_dir)
        try:
            self.dirs[rel_dir] = (os.stat(path).st_mtime_ns, time.time_ns())
            entries = list(os.scandir(path))
        except OSError:
            self.dirs.pop(rel_dir, None)
            return
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if recurse or rel not in self.dirs:
                        self._list(rel, recurse)
                elif entry.name.endswith(".jsonl"):
                    st = entry.stat()
                    self._consider(rel, st.st_size, st.st_mtime_ns / 1e6)
            except OSError:
                continue

    def rescan(self, now: float) -> None:
        """Walk the whole tree: mark every transcript that grew since it was last read."""
        self.last_scan = self.last_discover = now
        self.dirs = {}
        self._list("", recurse=True)
        # Forget files that disappeared or have been idle for over 8 days.
        for rel in list(self.store["offsets"]):
            try:
                if now - os.stat(self._abs(rel)).st_mtime_ns / 1e6 > STALE_MS:
                    del self.store["offsets"][rel]
                    self.changed = True
            except OSError:
                del self.store["offsets"][rel]
                self.changed = True

    def discover(self, now: float) -> None:
        """Re-list only the folders whose mtime changed: that is where new files appear."""
        self.last_discover = now
        for rel_dir, (mtime_ns, listed_ns) in list(self.dirs.items()):
            try:
                current = os.stat(self._abs(rel_dir)).st_mtime_ns
            except OSError:
                self.dirs.pop(rel_dir, None)
                continue
            if current != mtime_ns or listed_ns - mtime_ns < RACY_NS:
                self._list(rel_dir, recurse=False)

    def _check_hot(self, now: float) -> None:
        for rel, at in list(self.hot.items()):
            if now - at > HOT_MS:
                del self.hot[rel]
                continue
            try:
                size = os.stat(self._abs(rel)).st_size
            except OSError:
                del self.hot[rel]
                continue
            if size != self.store["offsets"].get(rel):
                self.dirty.add(rel)

    # ------------------------------------------------------------ reading

    def poll(self, now: float) -> list[dict]:
        """New events since the last poll:
        ``{"kind": "reply", "t", "usd", "model"}`` | ``{"kind": "limit", "t", "type", "resetsAt"}``
        | ``{"kind": "unpriced", "t", "model"}``."""
        if now - self.last_scan >= RESCAN_MS:
            self.rescan(now)
        elif now - self.last_discover >= DISCOVER_MS:
            self.discover(now)
        self._check_hot(now)
        out: list[dict] = []
        budget = READ_BUDGET
        for rel in sorted(self.dirty):
            if budget <= 0:
                break
            self.dirty.discard(rel)
            budget -= self._read_new(rel, out, budget, now)
        if not self.dirty and self.last_scan:
            self.caught_up = True
        return out

    def _read_new(self, rel: str, out: list[dict], budget: int, now: float) -> int:
        path = self._abs(rel)
        try:
            st = os.stat(path)
        except OSError:
            if self.store["offsets"].pop(rel, None) is not None:
                self.changed = True
            self.hot.pop(rel, None)
            return 0
        off = self.store["offsets"].get(rel)
        if off is None:
            if st.st_mtime_ns / 1e6 < (self.store.get("since") or 0):
                self.store["offsets"][rel] = st.st_size
                self.changed = True
                return 0
            off = 0
        if st.st_size < off:
            off = 0  # rewritten
        if st.st_size == off:
            return 0

        length = min(st.st_size - off, budget)
        try:
            with open(path, "rb") as f:
                f.seek(off)
                buf = f.read(length)
        except OSError:
            self.dirty.add(rel)  # locked or vanished mid-read: retry next poll
            return 0
        self.hot[rel] = now
        end = buf.rfind(b"\n")
        if end < 0:
            if length >= READ_BUDGET:
                self.store["offsets"][rel] = off + length  # one enormous line: skip it
                self.changed = True
            elif off + length < st.st_size:
                self.dirty.add(rel)  # budget ran out mid-line: retry next poll
            # else the writer is mid-line; the hot check reports the rest
            return length
        self.store["offsets"][rel] = off + end + 1
        self.changed = True
        if off + end + 1 < st.st_size:
            self.dirty.add(rel)  # more left for the next poll
        for line in buf[:end].split(b"\n"):
            self._parse(line, out)
        return length

    def _parse(self, line: bytes, out: list[dict]) -> None:
        has_usage = b'"usage"' in line
        has_limit = b'"quotaLimits"' in line
        if not has_usage and not has_limit:
            return
        try:
            o = json.loads(line)
        except ValueError:
            return
        if not isinstance(o, dict):
            return
        t = _parse_time(o.get("timestamp"))
        if t is None or not t >= (self.store.get("since") or 0):
            return

        q = o.get("quotaLimits")
        if isinstance(q, dict) and q.get("status") == "rejected" and q.get("rateLimitType"):
            resets = q.get("resetsAt")
            try:
                resets_at = float(resets) * 1000 if resets else None
            except (TypeError, ValueError):
                resets_at = None
            out.append({"kind": "limit", "t": t, "type": str(q["rateLimitType"]), "resetsAt": resets_at})

        m = o.get("message")
        if o.get("type") != "assistant" or not isinstance(m, dict):
            return
        model = m.get("model")
        if not isinstance(m.get("usage"), dict) or not model or model == "<synthetic>":
            return
        # A streamed reply is written as one line per content block, all with the same ids.
        key = f"{m.get('id')}:{o.get('requestId')}"
        if key in self.seen:
            return
        self.seen[key] = None
        if len(self.seen) > SEEN_MAX:
            del self.seen[next(iter(self.seen))]
        usd = reply_cost(model, m["usage"])
        if usd is None:
            out.append({"kind": "unpriced", "t": t, "model": str(model)})
        elif usd > 0:
            out.append({"kind": "reply", "t": t, "usd": usd, "model": str(model)})
