---
description: "Wipe, purge, clear or reset ONE profile's data — pick components to delete (conversations, memory, uploads, usage/cost records, running background processes, schedules, watchers, skill-events, channels, LLM keys, OAuth tokens, tools/MCP, skills, documents, browser login, app settings) or run a preset: `working` clears runtime data but keeps config, `factory` also strips credentials and customization back to a fresh-provisioned baseline. Scoped to the token's own profile and irreversible; distinct from `cremind backup restore` (whole-system) and `cremind conv delete-all` (conversations only)."
---

# `cremind clean` — Reset one profile's data

`cremind clean` wipes data for **one profile** — the profile the presented
`CREMIND_TOKEN` was minted for. There is no `--profile` flag: the server resolves
the target from the token, so a token can only ever clean its own profile.

Every mode is **irreversible**. `clean` never touches other profiles, never deletes
the profile itself, and never clears server-wide config — so even a full factory
reset leaves the Setup Wizard marked complete (it does **not** re-run). If you want a
recoverable, whole-system operation instead, use
[`cremind backup`](./%5Bcli%5Dcremind%20backup.md); to clear only conversations, use
[`cremind conv delete-all`](./%5Bcli%5Dcremind%20conv.md).

## The three modes

| Mode         | Command                     | What it does                                                                 |
|--------------|-----------------------------|------------------------------------------------------------------------------|
| **Custom**   | `cremind clean components`  | Purge any subset of components you pick with per-component flags.             |
| **Working**  | `cremind clean working`     | Preset: clear all **runtime data**, keep every configuration and credential. |
| **Factory**  | `cremind clean factory`     | Preset: working reset **plus** strip all config/credentials/customization back to a fresh-provisioned baseline (no LLM configured, default persona, default skills, only the auto `main` channel). |

Presets are expanded on the server, so the two clients (CLI + web UI) always agree
on exactly what `working` and `factory` mean.

## Components

The custom mode selects from these components (grouped as they appear in the web UI).
The last two columns show which preset includes each one.

| Group | Flag | Removes | `working` | `factory` |
|-------|------|---------|:---------:|:---------:|
| **Conversations, memory & uploads** | `--conversations` | Chat history + messages | ✅ | ✅ |
| | `--memory` | Long-term memory facts (+ memory embeddings) | ✅ | ✅ |
| | `--uploads` | Uploaded chat files | ✅ | ✅ |
| | `--plans` | Plan-mode files | ✅ | ✅ |
| **Usage & event-run history** | `--usage` | Token/cost usage records | ✅ | ✅ |
| | `--event-runs` | Event-run history | ✅ | ✅ |
| **Automation & channels** | `--processes` | Running background processes (shells the agent started) | ✅ | ✅ |
| | `--schedules` | Schedule / calendar rules | ✅ | ✅ |
| | `--file-watchers` | File-watcher rules | ✅ | ✅ |
| | `--skill-events` | Skill-event subscriptions | ✅ | ✅ |
| | `--channels` | External channels (keeps `main`) | ❌ | ✅ |
| **Config & credentials** | `--llm-config` | LLM providers, API keys, model groups | ❌ | ✅ |
| | `--oauth-tokens` | OAuth tokens (Google, etc.) | ❌ | ✅ |
| | `--tool-configs` | Tools/MCP registrations + their configs | ❌ | ✅ |
| | `--skills` | Reset persona + skills to shipped defaults | ❌ | ✅ |
| | `--documents` | Documents + their embeddings | ❌ | ✅ |
| | `--browser-login` | Saved browser login state | ❌ | ✅ |
| | `--app-settings` | Reset app settings (`user_config`) to defaults | ❌ | ✅ |

## Global flags

`CREMIND_TOKEN` is required for every subcommand. All subcommands accept the
root-level `--json` flag, which prints the raw server response (a per-component
report) instead of a table. Destructive subcommands take `--yes` / `-y` to skip the
confirmation prompt (for scripts).

## Subcommands

### `cremind clean components`

**Purpose.** Purge a custom subset of the current profile's data.

**Syntax.**

```bash
cremind clean components [--<component> ...] [--all] [--yes]
```

**Flags.**

| Flag | Meaning |
|------|---------|
| `--conversations` | Chat history + messages. |
| `--memory` | Long-term memory facts. |
| `--uploads` | Uploaded chat files. |
| `--plans` | Plan-mode files. |
| `--usage` | Token/cost usage records. |
| `--event-runs` | Event-run history. |
| `--processes` | Kill running background processes started by the agent. |
| `--schedules` | Schedule / calendar rules. |
| `--file-watchers` | File-watcher rules. |
| `--skill-events` | Skill-event subscriptions. |
| `--channels` | External channels (keeps `main`). |
| `--llm-config` | LLM providers, keys, model groups. |
| `--oauth-tokens` | OAuth tokens. |
| `--tool-configs` | Tools/MCP + their configs. |
| `--skills` | Reset persona + skills to shipped defaults. |
| `--documents` | Documents + their embeddings. |
| `--browser-login` | Saved browser login state. |
| `--app-settings` | Reset app settings to defaults. |
| `--all` | Select every component (equivalent to `factory`). |
| `--yes`, `-y` | Skip the confirmation prompt. |

**Behavior.** You must select at least one component (or `--all`), otherwise the
command errors. Unless `--yes` is passed it asks for confirmation, then prints a
`COMPONENT / REMOVED` table and a summary line (`cleaned N item(s) [custom] …`). With
`--json` it prints the raw report `{ "cleaned": {...}, "errors": {...}, "total": N }`.
If any component errors, the others still run and the command exits non-zero.

**Examples.**

```bash
# Just clear usage stats and chat history (with a confirmation prompt)
$ cremind clean components --usage --conversations

# Scripted: drop only the usage records, no prompt
$ cremind clean components --usage --yes

# Structured output for pipelines
$ cremind clean components --schedules --file-watchers --json | jq .cleaned
```

### `cremind clean working`

**Purpose.** Working-data reset — wipe all runtime data but keep every
configuration and credential (so the profile keeps working afterward).

**Syntax.**

```bash
cremind clean working [--yes]
```

**Behavior.** Clears conversations, memory, uploads, plans, usage, event-runs,
running background processes, schedules, file-watchers and skill-events. LLM config,
OAuth tokens, tools/MCP, skills, channels and app settings are **untouched** (and
registered autostart processes keep running — only ad-hoc runtime processes are
killed). Prompts unless `--yes`. Prints the same `COMPONENT / REMOVED` table +
summary; `--json` prints the raw report.

**Example.**

```bash
$ cremind clean working --yes
```

### `cremind clean factory`

**Purpose.** Full factory reset — the working reset **plus** stripping all
post-setup customization, returning the profile to a fresh-provisioned baseline.

**Syntax.**

```bash
cremind clean factory [--confirm-profile <name>] [--yes]
```

**Flags.**

| Flag | Meaning |
|------|---------|
| `--confirm-profile <name>` | Non-interactive guard: must equal this profile's name, or the command aborts. |
| `--yes`, `-y` | Skip the y/n prompt. The typed profile name is still required. |

**Behavior.** On top of the `working` set it also removes LLM config, OAuth tokens,
tools/MCP registrations + configs, external channels (keeping `main`), documents +
embeddings and browser login, and resets persona + skills + app settings to their
shipped defaults. The result is a profile that looks brand-new (no LLM configured),
but the profile itself and all server-wide config are kept, so **the Setup Wizard
does not re-run** and you stay signed in.

Because this is high-consequence, it is guarded by a **type-the-profile-name**
confirmation: interactively you must type the exact profile name; in scripts pass
`--confirm-profile <name>` (which must match). `--yes` alone only skips the extra
y/n prompt — it never bypasses the typed-name guard.

**Examples.**

```bash
# Interactive: prompts you to type the profile name, then confirm
$ cremind clean factory

# Scripted: the typed-name guard is satisfied by --confirm-profile
$ cremind clean factory --confirm-profile alice --yes
```

## Finding this in the web UI

Every mode has a control in the profile's **Danger Zone**:

> **Sidebar → Settings → Profiles → Danger Zone**

The component checklist there mirrors `cremind clean components`; the
**Working-data reset** and **Full factory reset** buttons mirror `cremind clean
working` / `cremind clean factory`; and the factory reset's type-the-profile-name
dialog mirrors the CLI's `--confirm-profile` guard. The Danger Zone acts on the
profile you are signed in as — the same profile the CLI token resolves to.

## Troubleshooting

**It cleaned the wrong profile** — `clean` always targets the profile of the
presented `CREMIND_TOKEN`; there is no override. Point `CREMIND_TOKEN` at the profile
you mean (or sign in as that profile in the UI).

**Factory reset didn't re-run the Setup Wizard** — By design. Factory reset keeps the
profile row and all server-wide config (including the `setup_complete` flag), so the
wizard stays complete and you are not signed out. It only blanks *this* profile's
data and customization.

**`409 busy`** — A backup, restore, or blueprint import is in progress. `clean`
refuses to run concurrently with those (they touch the same data). Wait for it to
finish (`cremind backup status`) and retry.

**I want it all back** — There is no undo. Restore from a backup archive with
`cremind backup restore` if you have one; otherwise the data is gone.

**A component reported an error but others succeeded** — Each component is
independent and best-effort; a single failure is recorded in the `errors` map and the
command exits non-zero, but the remaining components still ran. Re-running is safe
(the operation is idempotent).
