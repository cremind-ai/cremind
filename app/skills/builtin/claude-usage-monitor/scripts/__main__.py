# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""CLI for the claude-usage-monitor skill. Run: uv run scripts/__main__.py <command>.

Every command prints one JSON object. Commands that change the monitor's state go
through the running monitor's local API, so its in-memory state stays authoritative;
while it is stopped they edit its saved state directly.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from usage_monitor import common as C  # noqa: E402
from usage_monitor import profiles as P  # noqa: E402
from usage_monitor import switch as switching  # noqa: E402
from usage_monitor import usage as U  # noqa: E402
from usage_monitor.events import EVENT_TYPES  # noqa: E402
from usage_monitor.live import poll_once  # noqa: E402
from usage_monitor.monitor import Monitor, disabled_error, iso, send_test_alert, when_clock  # noqa: E402

START_COMMAND = f"cremind skill-events listener-start {C.SKILL_NAME}"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, never via a proxy


class Failure(Exception):
    """A command that could not do what was asked; printed as ``{"error": ...}``."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


# ---------------------------------------------------------------- the running monitor


def _request(url: str, method: str = "GET", body: Any = None, headers: dict | None = None, timeout: float = 3.0) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with _NO_PROXY.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def running_monitor(paths: C.Paths) -> dict | None:
    """``runtime.json`` of the monitor if it answers, else None (a force-killed monitor
    leaves the file behind, so the file alone proves nothing)."""
    runtime = C.read_json(paths.runtime)
    if not isinstance(runtime, dict) or not runtime.get("url"):
        return None
    try:
        health = _request(f"{runtime['url']}api/health", timeout=2.0)
    except (OSError, ValueError):
        return None
    return runtime if isinstance(health, dict) and health.get("ok") else None


def start_listener() -> tuple[bool, str]:
    """Ask Cremind to start the listener — the same call as the Events page's Start
    button, which also registers it to start again on every boot."""
    server = os.environ.get("CREMIND_SERVER", "").strip().rstrip("/")
    token = os.environ.get("CREMIND_TOKEN", "").strip()
    if not server or not token:
        return False, "not running under Cremind (no CREMIND_SERVER / CREMIND_TOKEN)"
    try:
        out = _request(f"{server}/api/skills/{C.SKILL_NAME}/listener-start", "POST", {}, {"Authorization": f"Bearer {token}"}, timeout=60.0)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return False, f"Cremind refused to start the listener ({e.code}): {detail}"
    except (OSError, ValueError) as e:
        return False, f"could not reach Cremind at {server}: {e}"
    return bool(isinstance(out, dict) and out.get("ok")), json.dumps(out)


def wait_running(paths: C.Paths, timeout_s: float) -> dict | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        runtime = running_monitor(paths)
        if runtime:
            return runtime
        time.sleep(0.5)
    return None


def call_monitor(paths: C.Paths, path: str, body: dict, method: str = "POST") -> Any:
    runtime = running_monitor(paths)
    if not runtime:
        return None
    return _request(runtime["url"] + path.lstrip("/"), method, body)


def open_in_browser(url: str) -> tuple[bool, str | None]:
    """Open ``url`` in the default browser of the computer Cremind runs on."""
    if C.in_container():
        return False, "Cremind runs in a container, which has no desktop to open a browser on"
    try:
        if sys.platform == "win32":
            os.startfile(url)  # noqa: S606 - ShellExecute: the user's default browser, a new tab
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return False, "no desktop session on the computer Cremind runs on"
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return False, str(e)
    return True, None


# ---------------------------------------------------------------- summaries


def _dur(ms: float) -> str:
    m = max(0, U.js_round(ms / U.MIN))
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 48:
        return f"{h}h {m % 60}m" if m % 60 else f"{h}h"
    return f"{h // 24}d {h % 24}h"


def _pct(w: dict | None) -> str:
    return f"{'≈' if w and w.get('estimated') else ''}{U.js_round(w['pct'])}%" if w else "—"


def _window(w: dict | None, now: float) -> dict | None:
    if not w:
        return None
    out: dict[str, Any] = {
        "used": _pct(w),
        "left_pct": max(0, U.js_round(100 - w["pct"])),
        "estimated": bool(w.get("estimated")),
    }
    if w.get("resetsAt"):
        out["resets"] = f"{when_clock(w['resetsAt'], now)} (in {_dur(w['resetsAt'] - now)})"
        out["resets_at"] = iso(w["resetsAt"])
    elif w.get("rolledOver"):
        out["resets"] = "reset — a new window opens with the next reply"
    return out


def _forecast_text(hero: dict, s: dict, now: float) -> str | None:
    hit = next((k for k in ("weekly", "session") if hero.get(k) and hero[k]["pct"] >= 100 and hero[k].get("resetsAt")), None)
    names = {"session": "5-hour", "weekly": "Weekly"}
    if hit:
        w = hero[hit]
        return f"{names[hit]} limit reached · resets {when_clock(w['resetsAt'], now)} (in {_dur(w['resetsAt'] - now)})"
    f = hero.get("forecast")
    if not f:
        return None
    first = f.get("first")
    if first:
        kind = first["kind"]
        crit = s["weeklyCritPct"] if kind == "weekly" else s["critPct"]
        text = f"{names[kind]} limit reached at ~{when_clock(first['limitAt'], now)} (in {_dur(first['limitAt'] - now)})"
        if first["switchAt"] > now:
            text += f"; switch by ~{when_clock(first['switchAt'], now)} ({crit}% of the {names[kind].lower()} limit)"
        return text
    return "Pace: idle" if f.get("idle") else "At this pace both limits reset before they run out"


def _recommendation_text(state: dict, hero: dict | None) -> str:
    rec = state.get("recommendation")
    accounts = state["accounts"]
    now = state["now"]
    if rec and rec.get("id"):
        nxt = next(a for a in accounts if a["id"] == rec["id"])
        if rec.get("why"):
            text = f"Next account: {nxt['displayName']} — {rec['why']}."
        else:
            text = f"Next account: {nxt['displayName']} (5-hour {_pct(nxt['session'])}, weekly {_pct(nxt['weekly'])} used)."
        if nxt.get("switchable"):
            return f"{text} Switch to it in one step, no sign-in: switch {_ref(nxt)}"
        return f"{text} Its login isn't saved here yet: add-account <name>, /login with it once; after that it switches in one step."
    if rec and rec.get("freeAt"):
        who = next((a for a in accounts if a["id"] == rec.get("freeId")), None)
        name = who["displayName"] if who else "The first one"
        return f"Every other account is used up. {name} frees up at {when_clock(rec['freeAt'], now)} (in {_dur(rec['freeAt'] - now)})."
    others = [a for a in accounts if a["id"] != (hero or {}).get("id")]
    if not others and state.get("disabledAccounts"):
        return "Every other account is disabled, so there is none to switch to (enable <account> brings one back)."
    if not others:
        return "Only one account is known so far. Add the others with add-account."
    return "No usage figures for the other accounts yet — they appear once their profiles are signed in (open-account, then /login)."


def _switching(state: dict) -> dict | None:
    """Manual or automatic switching, what happens now, and who is next (planner.py)."""
    p = state.get("plan")
    if not p:
        return None
    now = state["now"]
    auto = p["mode"] == "auto"
    t = p["thresholds"]
    out: dict[str, Any] = {
        "mode": "automatic" if auto else "manual (alerts only; you or the agent switch)",
        "now": p["action"]["text"],
    }
    if auto:
        out["switches_at"] = f"{t['session']:g}% of the 5-hour limit or {t['weekly']:g}% of the weekly one" + (", and early to use up a week that resets soon" if p.get("early") else "")
    if p.get("owner"):
        out["note"] = f"{p['owner']} runs automatic switching for this computer's Claude Code, so it doesn't run from here"
    if p.get("next"):
        out["next_in_line"] = f"{p['next']['account']}: {p['next']['why']}"
    if p.get("setAside"):
        out["set_aside"] = [f"{x['account']} (week full, back {when_clock(x['until'], now)})" for x in p["setAside"]]
    if p.get("hoursLeft") is not None:
        hours = p["hoursLeft"]
        pace = "your current pace" if p["pace"]["measured"] else "a typical pace"
        out["work_left_this_week"] = f"about {hours:.0f} h at {pace} ({p['pace']['pctPerHour']:g}% of a 5-hour window per hour), before the weekly resets"
    return out


LIVE_PROBLEMS = ("no_login", "expired", "signin", "slow", "denied", "error")


def _ref(a: dict) -> str:
    """How to name an account to ``switch``: its email, else its short name, else its id."""
    return a.get("email") or a.get("label") or a["id"]


def _figures(a: dict) -> str:
    estimated = any((a.get(k) or {}).get("estimated") for k in ("session", "weekly"))
    if not a["hasData"]:
        return "none yet"
    if a.get("source") == "api" and not estimated:
        return "official: Anthropic's own figures, the ones claude.ai shows"
    return "estimated since the last official reading" if estimated else f"official ({a.get('source') or 'Claude Code'})"


def _disabled_accounts(state: dict) -> list[dict] | None:
    """Accounts the user disabled: left out of everything else, never suggested nor switched to."""
    rows = [{"account": d["displayName"], "disabled_since": when_clock(d["since"], state["now"]), "enable_with": f"enable {_ref(d)}"} for d in state.get("disabledAccounts") or []]
    return rows or None


def summarize(state: dict, running: bool, url: str | None) -> dict:
    now = state["now"]
    s = state["settings"]
    hero = next((a for a in state["accounts"] if a["id"] == state.get("heroId")), None)
    accounts = []
    for a in state["accounts"]:
        live = a.get("live") or {}
        row: dict[str, Any] = {
            "account": a["displayName"],
            "email": a["email"] if a.get("email") and a["email"] != a["displayName"] else None,
            "plan": a["plan"] or None,
            "status": a["status"]["label"],
            "in_use": a["active"] or a["running"],
            "your_claude_code_uses_it": a["active"],
            "signed_in_here": a["signedIn"] or None,
            "five_hour": _window(a["session"], now),
            "weekly": _window(a["weekly"], now),
            "figures": _figures(a),
            "figures_as_of": iso(a["updatedAt"]),
        }
        if a.get("switchable"):
            row["switch_with"] = f"switch {_ref(a)}"
        if a.get("disabled"):
            row["disabled"] = f"yes: shown only while your Claude Code still uses it, never switched to again (enable {_ref(a)} undoes it)"
        if live.get("state") in LIVE_PROBLEMS and live.get("detail"):
            row["live_figures_problem"] = live["detail"]
        accounts.append(row)
    out: dict[str, Any] = {"monitor_running": running}
    if url:
        out["dashboard"] = url
    if not running:
        out["note"] = (
            "The monitor is not running, so no alerts are raised. Figures come straight from Anthropic where a valid "
            f"login was found, otherwise they are the last known ones. Start it with: {START_COMMAND}"
        )
    out["live_figures"] = "on" if s.get("live", True) else "off (estimates only; turn on with: settings --live on)"
    if hero:
        rate = hero.get("rate") or {}
        out["running_account"] = {
            "account": hero["displayName"],
            "status": hero["status"]["label"],
            "pace_per_hour": {k: f"{rate[k]['perHour']:.1f}%" for k in ("session", "weekly") if rate.get(k)} or None,
            "forecast": _forecast_text(hero, s, now),
        }
    out["recommendation"] = _recommendation_text(state, hero)
    switching_now = _switching(state)
    if switching_now:
        out["switching"] = switching_now
    known = [a for a in state["accounts"] if a["hasData"]]
    if known:
        left = sum(max(0, 100 - U.pct_of(a["weekly"])) for a in known)
        ready = sum(1 for a in known if U.pct_of(a["session"]) < s["critPct"] and U.pct_of(a["weekly"]) < s["weeklyCritPct"])
        out["weekly_headroom"] = f"{U.js_round(left)}% (≈ {left / 100:.1f} accounts); {ready} ready now"
    out["accounts"] = accounts
    disabled = _disabled_accounts(state)
    if disabled:
        out["disabled_accounts"] = disabled
    out["recent_alerts"] =[f"{datetime.fromtimestamp(e['t'] / 1000):%a %H:%M} {e['text']}" for e in state["events"] if e.get("kind") == "alert"][:5]
    out["alert_settings"] = {k: s[k] for k in ("warnPct", "critPct", "weeklyWarnPct", "weeklyCritPct", "etaAlertMin", "events")}
    return out


# ---------------------------------------------------------------- commands


def cmd_status(args: argparse.Namespace, paths: C.Paths) -> dict:
    runtime = running_monitor(paths)
    if runtime:
        # Asks Anthropic for every account first (a few seconds at most).
        try:
            state = _request(f"{runtime['url']}api/refresh", "POST", {"wait": True}, timeout=20.0)["state"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            state = _request(f"{runtime['url']}api/state")  # a monitor from before live figures
        return summarize(state, True, runtime["url"])
    monitor = Monitor(paths, readonly=True)
    monitor.refresh_readonly()
    if monitor.state["settings"].get("live", True):
        poll_once(monitor)
    return summarize(monitor.api_state(), False, None)


def cmd_dashboard(args: argparse.Namespace, paths: C.Paths) -> dict:
    runtime = running_monitor(paths)
    started = None
    if not runtime and not args.no_start:
        ok, detail = start_listener()
        started = ok
        runtime = wait_running(paths, 25.0) if ok else None
        if not runtime:
            raise Failure("The Claude Usage Monitor is not running and could not be started.", detail=detail, fix=START_COMMAND)
    if not runtime:
        raise Failure("The Claude Usage Monitor is not running.", fix=START_COMMAND)
    url = runtime["url"]
    out: dict[str, Any] = {"running": True, "url": url, "link": f"[Claude Usage Monitor]({url})"}
    if started:
        out["started_monitor"] = True
    if args.open:
        opened, why = open_in_browser(url)
        out["opened_in_browser"] = opened
        if why:
            out["not_opened_because"] = why
    out["note"] = "The dashboard is served on the computer running Cremind; the link works in a browser on that computer."
    return out


def _cached_usage(profile: P.ClaudeProfile) -> str:
    cfg = C.read_json(profile.config_file)
    u = U.from_claude_cache(cfg.get("cachedUsageUtilization")) if isinstance(cfg, dict) else None
    if not u:
        return "no usage loaded yet"

    def pct(w: dict | None) -> str:
        return f"{U.js_round(w['pct'])}%" if w else "—"

    minutes = U.js_round((C.now_ms() - u["at"]) / U.MIN)
    ago = f"{minutes} min ago" if minutes < 60 else f"{U.js_round(minutes / 60)} h ago" if minutes < 48 * 60 else f"{U.js_round(minutes / 1440)} days ago"
    return f"5-hour {pct(u['session'])}, weekly {pct(u['weekly'])} (as of {ago})"


def cmd_accounts(args: argparse.Namespace, paths: C.Paths) -> dict:
    kept = {e["name"]: e.get("account") for e in P.read_registry(paths)}
    state = C.read_json(paths.state)
    saved = state.get("accounts") if isinstance(state, dict) and isinstance(state.get("accounts"), dict) else {}
    disabled = {k for k, v in saved.items() if isinstance(v, dict) and v.get("disabledAt")}
    known = {}
    rows = []
    for p in P.list_profiles(paths):
        a = P.signed_in_account(p)
        if a:
            known[a["id"]] = a.get("email") or a["id"]
        row = {
            "profile": p.name,
            "kind": {"main": "your Claude Code (VS Code, terminals)", "cremind": "this Cremind profile's own Claude Code login", "extra": "extra profile"}[p.kind],
            "folder": str(p.dir),
            "account": f"{a.get('email') or a['id']} · {C.plan_label(a)}".rstrip(" ·") if a else "not signed in",
            "usage": _cached_usage(p) if a else None,
        }
        if a and a["id"] in disabled:
            row["disabled"] = f"yes: never switched to until it is enabled again (enable {a.get('email') or a['id']})"
        elif a and p.kind == "extra":
            row["switch_with"] = f"switch {a.get('email') or a['id']}"
        elif not a and kept.get(p.name):
            row["keeps_place_of"] = kept[p.name]  # its login is in your Claude Code now
        rows.append(row)
    for row in rows:
        if row.get("keeps_place_of"):
            row["keeps_place_of"] = f"{known.get(row['keeps_place_of'], row['keeps_place_of'])} (its login is in use elsewhere now; it returns here when you switch away from it)"
    out: dict[str, Any] = {"profiles": rows}
    if not P.read_registry(paths):
        out["hint"] = "No extra profiles yet. Add one per extra account with: add-account <name>"
    return out


def _next_steps(profile: P.ClaudeProfile, opened: bool) -> list[str]:
    first = "A Claude Code window opened on the computer running Cremind. In it:" if opened else f"Start Claude Code in this profile ({P.launch_hint(profile)}), then:"
    return [
        first,
        "1. Sign in with /login, using the account to add.",
        "2. Type /exit.",
        "The monitor fetches the account's official figures from Anthropic within seconds. Your Claude Code keeps its account until you switch: then `switch <email>` (or Switch on the dashboard) moves this login into it in one step, no sign-in.",
    ]


def cmd_add_account(args: argparse.Namespace, paths: C.Paths) -> dict:
    name = args.name
    if not P.NAME_RE.match(name):
        raise Failure("Use letters, digits, - and _ (up to 32 characters, starting with a letter or digit).")
    if name.lower() in P.RESERVED:
        raise Failure(f'"{name}" is reserved; pick another name.')
    if any(e["name"].lower() == name.lower() for e in P.read_registry(paths)):
        raise Failure(f'A profile named "{name}" already exists.', fix=f"open-account {name}")
    profile = P.extra_profile(name)
    P.prepare_profile_dir(profile)

    def add(entries: list[dict]) -> list[dict]:
        if not any(e["name"].lower() == name.lower() for e in entries):
            entries.append({"name": name, "addedAt": datetime.now().astimezone().isoformat(timespec="seconds")})
        return entries

    P.update_registry(paths, add)
    account = P.signed_in_account(profile)
    if account:
        return {"created": name, "folder": str(profile.dir), "signed_in": account.get("email") or account["id"]}
    opened = (not args.no_open) and P.open_terminal(profile)
    return {"created": name, "folder": str(profile.dir), "opened_claude_code": opened, "next_steps": _next_steps(profile, opened)}


def cmd_open_account(args: argparse.Namespace, paths: C.Paths) -> dict:
    profile = P.find_profile(paths, args.name)
    if not profile:
        raise Failure(f'No profile named "{args.name}".', fix="accounts")
    opened = P.open_terminal(profile)
    then = "Sign in with /login if its login has expired or was revoked, then /exit. The monitor fetches its figures by itself."
    if not opened:
        return {"opened_claude_code": False, "run_yourself": P.launch_hint(profile), "then": then}
    return {"opened_claude_code": True, "profile": profile.name, "then": then}


def cmd_remove_account(args: argparse.Namespace, paths: C.Paths) -> dict:
    entry = next((e for e in P.read_registry(paths) if e["name"].lower() == args.name.lower()), None)
    if not entry:
        raise Failure(f'No extra profile named "{args.name}".', fix="accounts")
    P.update_registry(paths, lambda entries: [e for e in entries if e["name"].lower() != entry["name"].lower()])
    folder = P.extra_profile(entry["name"]).dir
    if args.delete_files:
        shutil.rmtree(folder, ignore_errors=True)
        return {"removed": entry["name"], "deleted_folder": str(folder)}
    return {"removed": entry["name"], "kept_folder": str(folder), "note": "Its folder and login remain; pass --delete-files to delete them."}


def _switched(r: dict) -> dict:
    out: dict[str, Any] = {"switched": True, "your_claude_code_now_uses": r.get("email") or r["account"]}
    if r.get("fromEmail"):
        out["previous_account"] = r["fromEmail"]
    if r.get("parkedIn"):
        out["previous_login_kept_in"] = f"profile {r['parkedIn']}" + (" (new)" if r.get("createdProfile") else "")
    elif r.get("dropped"):
        out["previous_login_kept_in"] = "its own profile, where it was signed in already"
    out["checked_with_anthropic"] = bool(r.get("verified"))
    out["note"] = (
        "Done, without signing in: VS Code and terminal Claude Code sessions use this account from their next message, "
        "and Cremind's Claude Code tool too unless it has a login of its own. Switch back the same way."
    )
    if r.get("warning"):
        out["warning"] = r["warning"]
    return out


def cmd_switch(args: argparse.Namespace, paths: C.Paths) -> dict:
    runtime = running_monitor(paths)
    if runtime:
        try:
            out = _request(f"{runtime['url']}api/switch", "POST", {"account": args.account, "force": args.force}, timeout=90.0)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            if e.code == 409:
                raise Failure(body.get("error") or "Could not switch.", reason=body.get("reason"), fix=body.get("fix")) from None
            if e.code != 404 or body.get("reason") == "unknown":
                raise Failure(f'No account matches "{args.account}".', fix="status (each account's switch_with names it)") from None
            # A monitor from before switching: the files are the truth, and it follows them.
        else:
            if not out.get("result"):  # already the account in use
                return {"switched": False, "note": out.get("error")}
            return _switched(out["result"])
    monitor = Monitor(paths, readonly=True)
    monitor.refresh_readonly()
    account = monitor.find_account(args.account)
    if not account:
        raise Failure(f'No account matches "{args.account}".', fix="status (each account's switch_with names it)")
    if account.get("disabledAt") and account["id"] != monitor.state["activeId"]:
        e = disabled_error(account)
        raise Failure(str(e), reason=e.reason, fix=e.fix)
    try:
        result = switching.switch_to(paths, account["id"], force=args.force)
    except switching.SwitchError as e:
        if e.reason == "already":
            return {"switched": False, "note": str(e)}
        raise Failure(str(e), reason=e.reason, fix=e.fix) from None
    return _switched(result)


def cmd_plan(args: argparse.Namespace, paths: C.Paths) -> dict:
    """What should happen to the account your Claude Code uses, now: the same rules automatic
    switching follows, with every other account ranked and the reason. For the agent deciding
    on an alert, and for "which account next?"."""
    runtime = running_monitor(paths)
    if runtime:
        state = _request(f"{runtime['url']}api/state")
    else:
        monitor = Monitor(paths, readonly=True)
        monitor.refresh_readonly()
        if monitor.state["settings"].get("live", True):
            poll_once(monitor)
        state = monitor.api_state()
    p = state["plan"]
    act = p["action"]
    accounts = {a["id"]: a for a in state["accounts"]}
    out: dict[str, Any] = {"do": act["do"], "what": act["text"]}
    if act["do"] == "switch" and act.get("to") in accounts:
        out["switch_to"] = accounts[act["to"]]["displayName"]
        out["switch_command"] = f"switch {_ref(accounts[act['to']])}"
    out["switching"] = _switching(state)
    if p.get("current"):
        cur = p["current"]
        out["your_claude_code_uses"] = f"{cur['account']}: 5-hour {cur['session']}%, weekly {cur['weekly']}%"
    out["ranking"] = [{"account": c["account"], "can_switch_to_it_now": c["ok"], "why": c["why"]} for c in p["candidates"]]
    disabled = _disabled_accounts(state)
    if disabled:
        out["disabled_accounts"] = disabled
    if not runtime:
        out["note"] = f"The monitor is not running, so nothing switches automatically and the figures may be old. Start it with: {START_COMMAND}"
    return out


def cmd_rotation(args: argparse.Namespace, paths: C.Paths) -> dict:
    runtime = running_monitor(paths)
    monitor = Monitor(paths, readonly=bool(runtime))
    account = monitor.find_account(args.account)
    if not account:
        raise Failure(f'No account matches "{args.account}".', fix="status")
    on = args.state == "on"
    if runtime:
        call_monitor(paths, f"api/accounts/{account['id']}", {"rotation": on})
    else:
        monitor.set_rotation(account["id"], on)
    who = account.get("email") or account["id"]
    return {"account": who, "in_rotation": on, "note": f"{who} {'may be suggested and switched to again' if on else 'is never suggested nor switched to'}."}


def _disabled(r: dict) -> dict:
    out: dict[str, Any] = {"disabled": r["account"]}
    if r.get("switchedTo"):
        moved = r.get("switch") or {}
        out["your_claude_code_now_uses"] = r["switchedTo"]
        if moved.get("parkedIn"):
            out["its_login_kept_in"] = f"profile {moved['parkedIn']}" + (" (new)" if moved.get("createdProfile") else "")
        out["checked_with_anthropic"] = bool(moved.get("verified"))
        if moved.get("warning"):
            out["warning"] = moved["warning"]
    out["note"] = f"{r['account']} is hidden from the dashboard, status and plan, and is never suggested nor switched to — by hand or automatically — until `enable` brings it back."
    if r.get("switchedTo"):
        out["note"] += f" VS Code and terminal Claude Code sessions carry on with {r['switchedTo']} from their next message, with no sign-in."
    return out


def cmd_disable(args: argparse.Namespace, paths: C.Paths) -> dict:
    """Hide an account and keep Claude Code off it. Disabling the account your Claude Code uses
    switches it to another account first, which takes the user's confirmation (--confirm)."""
    runtime = running_monitor(paths)
    monitor = Monitor(paths, readonly=bool(runtime))  # while the monitor is stopped, its saved state is changed here
    monitor.refresh_readonly()
    account = monitor.find_account(args.account)
    if not account:
        raise Failure(f'No account matches "{args.account}".', fix="status")
    try:
        if runtime:
            try:
                result = _request(f"{runtime['url']}api/accounts/{account['id']}", "POST", {"disabled": True, "confirm": args.confirm}, timeout=90.0)["result"]
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise Failure(f'No account matches "{args.account}".', fix="status") from None
                if e.code == 400:
                    raise Failure("The running monitor is older than this command.", fix="restart the claude-usage-monitor listener (Cremind's Processes page), then try again") from None
                if e.code != 409:
                    raise
                try:
                    body = json.loads(e.read() or b"{}")
                except ValueError:
                    body = {}
                raise switching.SwitchError(body.get("reason") or "error", body.get("error") or "Could not disable it", body.get("fix")) from None
        else:
            if account["id"] == monitor.state["activeId"] and monitor.state["settings"].get("live", True):
                poll_once(monitor)  # fresh figures pick the account that takes over

            def switch(target: str, why: dict) -> dict:
                moved = switching.switch_to(paths, target)
                monitor.refresh_readonly()  # your Claude Code's account, as the files say now
                return moved

            result = monitor.set_disabled(account["id"], True, confirm=args.confirm, switch=switch)
    except switching.SwitchError as e:
        fix = f"Tell the user and ask them to confirm. Only if they do: disable {_ref(account)} --confirm" if e.reason == "confirm" else e.fix
        raise Failure(str(e), reason=e.reason, fix=fix) from None
    return _disabled(result)


def cmd_enable(args: argparse.Namespace, paths: C.Paths) -> dict:
    runtime = running_monitor(paths)
    monitor = Monitor(paths, readonly=bool(runtime))
    monitor.refresh_readonly()
    account = monitor.find_account(args.account)
    if not account:
        raise Failure(f'No account matches "{args.account}".', fix="status")
    if runtime:
        call_monitor(paths, f"api/accounts/{account['id']}", {"disabled": False})
    else:
        monitor.set_disabled(account["id"], False)
    who = _ref(account)
    out: dict[str, Any] = {"enabled": who, "note": f"{who} is shown again, and may be suggested and switched to like any other account."}
    if any(rt.p.kind == "extra" and monitor.current_account(rt.name) == account["id"] for rt in monitor.profiles):
        out["switch_with"] = f"switch {who}"
    return out


def cmd_settings(args: argparse.Namespace, paths: C.Paths) -> dict:
    body: dict[str, Any] = {}
    for flag, key in (
        ("warn_pct", "warnPct"),
        ("crit_pct", "critPct"),
        ("weekly_warn_pct", "weeklyWarnPct"),
        ("weekly_crit_pct", "weeklyCritPct"),
        ("eta_minutes", "etaAlertMin"),
        ("auto_session_pct", "autoSessionPct"),
        ("auto_weekly_pct", "autoWeeklyPct"),
    ):
        value = getattr(args, flag)
        if value is not None:
            body[key] = value
    if args.alerts is not None:
        body["events"] = args.alerts == "on"
    if args.live is not None:
        body["live"] = args.live == "on"
    if args.switching is not None:
        body["switchMode"] = args.switching
    if args.early is not None:
        body["autoEarly"] = args.early == "on"
    if not body:
        runtime = running_monitor(paths)
        settings = _request(f"{runtime['url']}api/state")["settings"] if runtime else Monitor(paths, readonly=True).state["settings"]
        return {"settings": settings}
    out = call_monitor(paths, "api/settings", body)
    if out is None:
        out = {"settings": Monitor(paths).apply_settings(body)}
    result: dict[str, Any] = {"settings": out["settings"]}
    if body.get("switchMode") == "auto":
        result["note"] = (
            "In automatic mode the account in use raises no heads-up alerts (limit_warning, switch_now, limit_soon): "
            "switches are reported as auto_switched, no_account_available and auto_switch_failed. If the user gets "
            "their Claude usage alerts in a conversation, check `cremind skill-events list` and subscribe that "
            "conversation to these three too, with the same action — otherwise they hear nothing."
        )
    return result


def cmd_label(args: argparse.Namespace, paths: C.Paths) -> dict:
    runtime = running_monitor(paths)
    monitor = Monitor(paths, readonly=bool(runtime))
    account = monitor.find_account(args.account)
    if not account and runtime:
        state = _request(f"{runtime['url']}api/state")
        ref = args.account.strip().lower()
        match = [a for a in state["accounts"] if ref in (a["id"].lower(), (a.get("email") or "").lower(), (a.get("label") or "").lower())]
        account = {"id": match[0]["id"]} if match else None
    if not account:
        raise Failure(f'No account matches "{args.account}".', fix="status")
    if runtime:
        call_monitor(paths, f"api/accounts/{account['id']}", {"label": args.label})
    else:
        monitor.set_label(account["id"], args.label)
    return {"account": account.get("email") or account["id"], "label": args.label.strip()[:40]}


def cmd_test_alert(args: argparse.Namespace, paths: C.Paths) -> dict:
    out = call_monitor(paths, "api/test-alert", {"type": args.type})
    if out is None:
        written = send_test_alert(args.type, None)
        out = {"ok": True, "type": args.type, "file": written.name}
    out["note"] = f"Every subscription to the {args.type} event fires; with none, Cremind drops the event."
    return out


def cmd_statusline(args: argparse.Namespace, paths: C.Paths) -> dict:
    settings_file = P.main_profile().settings_file
    try:
        raw = settings_file.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raw = ""
    try:
        settings = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        raise Failure(f"Could not parse {settings_file}: {e}. Nothing was changed.") from None
    current = settings.get("statusLine")
    command = C.statusline_command()

    if args.action == "status":
        return {"installed": C.is_our_statusline(current), "other_status_line": bool(current) and not C.is_our_statusline(current), "settings_file": str(settings_file)}

    def backup_and_save() -> str | None:
        backup = None
        if raw:
            paths.backups.mkdir(parents=True, exist_ok=True)
            backup = paths.backups / f"settings.json.{datetime.now():%Y-%m-%dT%H-%M-%S}.bak"
            backup.write_text(raw, encoding="utf-8")
        C.write_json_atomic(settings_file, settings, indent=2)
        return str(backup) if backup else None

    if args.action == "remove":
        if not C.is_our_statusline(current):
            return {"removed": False, "note": "Claude Code is not using this status line."}
        del settings["statusLine"]
        return {"removed": True, "backup": backup_and_save()}

    if current and not C.is_our_statusline(current) and not args.force:
        raise Failure("Claude Code already has a different status line.", current=current, fix="statusline install --force (settings.json is backed up first)")
    if current and current.get("command") == command:
        return {"installed": True, "note": "Already installed. Claude Code sends usage to the monitor after each reply."}
    settings["statusLine"] = {"type": "command", "command": command, "padding": 0}
    return {"installed": True, "settings_file": str(settings_file), "command": command, "backup": backup_and_save(), "note": "Running sessions pick this up automatically; usage appears after their next reply."}


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-usage-monitor", description="Claude subscription usage across accounts.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="usage of every account, the forecast and which account to switch to")
    p = sub.add_parser("dashboard", help="the dashboard link (starts the monitor if needed)")
    p.add_argument("--open", action="store_true", help="also open it in the browser of the computer running Cremind")
    p.add_argument("--no-start", action="store_true", help="don't start the monitor when it is not running")
    sub.add_parser("accounts", help="Claude Code profiles and the account signed in to each")
    p = sub.add_parser("switch", help="make another account the one your Claude Code uses, with its saved login (no sign-in)")
    p.add_argument("account", help="email, short name, profile name, or account id")
    p.add_argument("--force", action="store_true", help="switch even while that account is in use in its own Claude Code window")
    p = sub.add_parser("add-account", help="create a Claude Code profile for another account")
    p.add_argument("name")
    p.add_argument("--no-open", action="store_true", help="don't open Claude Code in it")
    p = sub.add_parser("open-account", help="open Claude Code in a profile (to sign in again)")
    p.add_argument("name")
    p = sub.add_parser("remove-account", help="stop tracking an extra profile")
    p.add_argument("name")
    p.add_argument("--delete-files", action="store_true", help="also delete its folder and login")
    sub.add_parser("plan", help="what should happen to the account your Claude Code uses now, and every other account ranked with the reason")
    p = sub.add_parser("rotation", help="whether an account may be suggested and switched to")
    p.add_argument("account", help="email, short name, profile name, or account id")
    p.add_argument("state", choices=("on", "off"))
    p = sub.add_parser("disable", help="hide an account, and never use nor switch to it until it is enabled again")
    p.add_argument("account", help="email, short name, profile name, or account id")
    p.add_argument("--confirm", action="store_true", help="the user confirmed: the account your Claude Code uses is switched away from first")
    p = sub.add_parser("enable", help="bring a disabled account back")
    p.add_argument("account", help="email, short name, profile name, or account id")
    p = sub.add_parser("settings", help="show or change switching and alert settings")
    p.add_argument("--switching", choices=("auto", "manual"), help="switch your Claude Code automatically, or alerts only")
    p.add_argument("--auto-session-pct", type=int, help="automatic switching: 5-hour threshold, %% (50-99)")
    p.add_argument("--auto-weekly-pct", type=int, help="automatic switching: weekly threshold, %% (50-100); the account is set aside until its week resets")
    p.add_argument("--early", choices=("on", "off"), help="automatic switching: also switch early to use up a week that resets soon")
    p.add_argument("--warn-pct", type=int, help="5-hour heads-up, %%")
    p.add_argument("--crit-pct", type=int, help="5-hour switch-now, %%")
    p.add_argument("--weekly-warn-pct", type=int, help="weekly heads-up, %%")
    p.add_argument("--weekly-crit-pct", type=int, help="weekly switch-now, %%")
    p.add_argument("--eta-minutes", type=int, help="alert when a limit is this many minutes away (0 = off)")
    p.add_argument("--alerts", choices=("on", "off"), help="send alerts to Cremind events")
    p.add_argument("--live", choices=("on", "off"), help="fetch each account's official figures from Anthropic")
    p = sub.add_parser("label", help="give an account a short name")
    p.add_argument("account", help="email, current name, or account id")
    p.add_argument("label")
    p = sub.add_parser("test-alert", help="send a test alert event")
    p.add_argument("--type", choices=EVENT_TYPES, default="limit_warning")
    p = sub.add_parser("statusline", help="the optional Claude Code status line")
    p.add_argument("action", choices=("install", "remove", "status"))
    p.add_argument("--force", action="store_true", help="replace a different status line")
    return parser


COMMANDS = {
    "status": cmd_status,
    "dashboard": cmd_dashboard,
    "accounts": cmd_accounts,
    "switch": cmd_switch,
    "plan": cmd_plan,
    "rotation": cmd_rotation,
    "disable": cmd_disable,
    "enable": cmd_enable,
    "add-account": cmd_add_account,
    "open-account": cmd_open_account,
    "remove-account": cmd_remove_account,
    "settings": cmd_settings,
    "label": cmd_label,
    "test-alert": cmd_test_alert,
    "statusline": cmd_statusline,
}


def _print(payload: dict) -> None:
    sys.stdout.buffer.write((json.dumps(C.js_safe(payload), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    paths = C.default_paths()
    try:
        # Stdout carries the one JSON answer. Anything else a command prints — the monitor's own
        # log lines, when a setting changes while the monitor isn't running — goes to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            out = COMMANDS[args.command](args, paths)
        _print(out)
        return 0
    except Failure as e:
        _print({"error": str(e), **e.extra})
        return 1
    except (OSError, ValueError) as e:
        _print({"error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
