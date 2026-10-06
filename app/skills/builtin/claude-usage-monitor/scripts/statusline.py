"""Claude Code ``statusLine`` command (installed with ``__main__.py statusline install``).

Claude Code pipes session JSON — including ``rate_limits`` for Pro/Max plans — to stdin
after each response. This hands a snapshot to the monitor (``.monitor/inbox``) and prints
a one-line usage summary for the terminal. It never reads login tokens.

Claude Code runs it with ``python -I -S``: standard library only, and no ``uv``.
"""

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from usage_monitor import common as C  # noqa: E402
from usage_monitor import usage as U  # noqa: E402


def _ansi(code: str):
    return lambda s: f"\x1b[{code}m{s}\x1b[0m"


dim = _ansi("2")
tone = {"good": _ansi("32"), "warning": _ansi("33"), "critical": _ansi("31"), "accent": _ansi("36")}


def read_stdin(timeout_s: float) -> str:
    box: list[bytes] = []

    def reader() -> None:
        try:
            box.append(sys.stdin.buffer.read())
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout_s)
    return box[0].decode("utf-8", errors="replace") if box else ""


def clock(ms: float, now: float) -> str:
    d = datetime.fromtimestamp(U.js_round(ms / U.MIN) * U.MIN / 1000)  # resets land on xx:59:59.9
    hm = d.strftime("%H:%M")
    return f"{d.strftime('%a')} {hm}" if ms - now > 20 * U.HOUR else hm


def dur(ms: float) -> str:
    m = max(0, U.js_round(ms / U.MIN))
    if m < 60:
        return f"{m}m"
    h = m // 60
    return f"{h}h{m % 60:02d}m" if h < 48 else f"{h // 24}d{h % 24}h"


def bar(pct: float, cells: int = 8) -> str:
    filled = max(0, min(cells, U.js_round(pct / 100 * cells)))
    return "█" * filled + "░" * (cells - filled)


def render(data: dict, account: dict | None, now: float, paths: C.Paths) -> str:
    summary = C.read_json(paths.summary)
    summary = summary if isinstance(summary, dict) else {}
    fresh = now - (summary.get("at") or 0) < 15 * U.MIN
    s = {**U.DEFAULT_SETTINGS, **(summary.get("settings") or {})}
    info = ((summary.get("accounts") or {}).get(account["id"]) if account else None) or {}
    sep = dim(" │ ")
    parts = [f"{tone['accent']('◆')} {info.get('label') or (account or {}).get('email') or 'Claude'}"]

    u = U.from_statusline(data.get("rate_limits"), now)
    if not u:
        parts.append(dim("usage appears after the first reply"))
    else:
        # The projected limit time goes on whichever limit the monitor expects to run out first.
        def limit_on(kind: str, w: dict) -> bool:
            limit_at = info.get("limitAt")
            return bool(fresh and limit_at and (info.get("limitKind") or "session") == kind and w.get("resetsAt") and limit_at < w["resetsAt"])

        def meter(label: str, w: dict, warn: float, crit: float, show_reset: bool, kind: str) -> str:
            paint = tone["critical"] if w["pct"] >= crit else tone["warning"] if w["pct"] >= warn else tone["good"]
            pct = U.js_round(w["pct"])
            txt = f"{label} " + paint(f"{bar(w['pct'])} {pct}%")
            if w.get("resetsAt") and show_reset:
                resets = clock(w["resetsAt"], now)
                txt += dim(f" ↻{resets} ({dur(w['resetsAt'] - now)})" if kind == "session" else f" ↻{resets}")
            if limit_on(kind, w):
                txt += " " + paint(f"limit ~{clock(info['limitAt'], now)}")
            return txt

        if u["session"]:
            w = U.project(u["session"], now)
            parts.append(meter("5h", w, s["warnPct"], s["critPct"], True, "session"))
        if u["weekly"]:
            w = U.project(u["weekly"], now)
            parts.append(meter("7d", w, s["weeklyWarnPct"], s["weeklyCritPct"], w["pct"] >= s["weeklyWarnPct"], "weekly"))

    nxt = summary.get("next")
    if fresh and nxt and (not account or nxt.get("id") != account["id"]):
        figures = f"(5h {nxt.get('sessionPct')}% · 7d {nxt.get('weeklyPct')}%)"
        parts.append(f"{dim('next →')} {nxt.get('label')} {dim(figures)}")
    elif fresh and summary.get("freeAt"):
        parts.append(dim(f"no spare account until {clock(summary['freeAt'], now)}"))
    return sep.join(parts)


def main() -> None:
    raw = read_stdin(1.5)
    try:
        data = json.loads(raw or "{}") or {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    now = C.now_ms()
    paths = C.default_paths()
    account = C.read_active_account()
    session_id = str(data.get("session_id") or "unknown")
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    try:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id)[:80]
        C.write_json_atomic(
            paths.inbox / f"{safe}.json",
            {
                "v": 1,
                "at": now,
                "sessionId": session_id,
                "cwd": workspace.get("current_dir") or data.get("cwd") or None,
                "model": model.get("display_name") or None,
                "version": data.get("version") or None,
                "configDir": os.environ.get("CLAUDE_CONFIG_DIR") or None,
                "account": account,
                "rateLimits": data.get("rate_limits") or None,
            },
        )
    except OSError:
        pass
    sys.stdout.buffer.write(render(data, account, now, paths).encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a status line must always print something
        sys.stdout.buffer.write(b"usage monitor: unavailable")
