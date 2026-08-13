---
description: "**Unlink, disconnect, revoke, or remove a linked Google account** from the Google Suite skills (`gmail`, `gcalendar`, `gdrive`, `gsheets`, `gdocs`), and show which Google account each one currently uses. Use this whenever the user wants to sign out of Google, log out, switch Google accounts, remove the wrong account, undo a link, revoke Cremind's access to their Gmail/Calendar/Drive/Sheets/Docs, or clean up before handing a machine over. `cremind google status` reports the linked address per skill; `cremind google unlink <skill>` deletes this machine's copy of the credentials and revokes Cremind's access at Google; `cremind google unlink --all` does it for every Google skill at once. Two facts change the answer and must not be guessed: **Google treats Cremind as ONE app**, so revoking one skill's grant can end the grant for every other Google skill linked to the same address — a plain per-skill unlink therefore declines to revoke while a sibling still shares it, and says so; and **unlinking gdrive destroys its per-file grants permanently**, so re-linking is not enough and the user has to pick the files again. Re-linking always needs a fresh Google consent, which runs from chat via the skill itself, not from this command group. Distinct from `cremind calendar google disconnect`, which clears the credential connected on the Calendar & Schedule page rather than the gcalendar skill's."
---

# `cremind google` — linked Google accounts

`cremind google` shows and removes the Google account links behind the Google
Suite skills.

**Linking happens in chat**, through the skill that needs it — each of `gmail`,
`gcalendar`, `gdrive`, `gsheets` and `gdocs` owns its own OAuth token and runs its
own consent flow. This group is the other half: reporting what is linked, and
taking a link apart.

Three things about unlinking are easy to get wrong.

**Google treats Cremind as one app.** All five skills share a single OAuth client,
so Google's revoke ends the grant for an *(app, account)* pair — not for one
skill. If the same address is linked in more than one Google skill, revoking any
one of them can invalidate the rest. So a plain `cremind google unlink <skill>`
**deletes the local credentials but declines to revoke** while a sibling still
shares the grant, and tells you which skills those are. Use `--all` (or unlink the
siblings too) to actually end the grant at Google; `--force-revoke` overrides the
guard deliberately.

**Revoking is a one-way door.** The refresh token is the only thing that can
revoke a grant, and unlinking deletes it. If the revoke call fails, the local
credentials are still wiped — Cremind can no longer use the account — but the grant
stays listed on the user's Google account and **re-running the command cannot fix
it**. The only remedy is removing Cremind by hand at
<https://myaccount.google.com/connections>.

**Unlinking a skill with a listener deregisters it.** `gcalendar` and `gdrive` run
a background listener; unlinking stops it *and* removes its autostart
registration. After re-linking, register the listener again (Settings → Tools &
Skills → the skill → Register Process) or its `event_changed` / `file_changed`
automations stay silent.

## Finding this in the web UI

> **Sidebar → Settings → GSuite** — one page for every Google service. Its
> **Accounts** group is the same per-skill inventory (`status`) with the per-skill
> **Unlink** buttons (`unlink <skill>`) and the **Unlink all Google accounts**
> action (`unlink --all`); its **Google Drive file access** group below covers
> per-file grants (see `cremind drive`). The **Calendar & Schedule** page's "via
> gcalendar skill" badge also unlinks that one skill.

## Global flags

`--json` is a **root** flag, so it goes before the group:

```bash
cremind --json google status     # correct
cremind google status --json     # WRONG - not a subcommand flag
```

All commands need a token (`CREMIND_TOKEN`, or the per-profile token the CLI
resolves automatically) and act on the caller's own profile.

## Commands

| Command | Arguments | Flags |
|---|---|---|
| `status` | — | — |
| `unlink` | `[SKILL]` | `--all`, `--no-revoke`, `--force-revoke`, `--yes`/`-y` |

### `cremind google status`

**Purpose.** Show which Google account each installed Google Suite skill is
linked to, whether the skill is enabled, whether its listener is registered,
whether a Google push channel is live, and how many event automations would go
idle if it were unlinked.

**Syntax.**

```bash
cremind google status
```

**Behavior.** Lists only *installed* skills. A skill can be linked while disabled
— the credential lives in the skill directory, not in the registry — and that is
reported as `ENABLED: no` with an address still shown. When one address is linked
in several skills, a shared-grant note is printed to stderr.

**Example.**

```console
$ cremind google status
SKILL      LINKED AS       ENABLED  LISTENER     WATCH   SUBS
gcalendar  u@example.com   yes      registered   active  2
gdrive     u@example.com   yes      not started  —       0
gmail      —               no       —            —       0

Note: Google lists Cremind as one app, so gcalendar, gdrive share the grant for
u@example.com. Revoking any one of them may end the others.
```

### `cremind google unlink`

**Purpose.** Delete this machine's copy of a Google credential and revoke
Cremind's access at Google.

**Syntax.**

```bash
cremind google unlink gcalendar
cremind google unlink --all
cremind google unlink gdrive --no-revoke --yes
```

**Flags.**

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `SKILL` | argument | — | Which skill to unlink: `gcalendar`, `gdocs`, `gdrive`, `gmail`, `gsheets`. Required unless `--all` |
| `--all` | bool | off | Unlink every Google Suite skill for this profile. Always revokes, since no sibling is left to protect |
| `--no-revoke` | bool | off | Wipe local credentials only; leave the grant live at Google. Never touches sibling skills |
| `--force-revoke` | bool | off | Revoke even when another skill still shares the grant — which will break that skill too |
| `--yes` / `-y` | bool | off | Skip the confirmation prompt. **Required when running non-interactively** (there is no TTY under `exec_shell`, so the prompt cannot be answered) |

**Behavior.** In order: the skill's listener is stopped and deregistered; the
Google push channel is closed (this needs a live credential, so it happens before
the revoke); the grant is revoked at Google; the local files are deleted; and the
backend's cached tokens for that profile are dropped. `scripts/.env` is
**preserved** — it holds bring-your-own-client configuration, not link state.

Unlinking something that is not linked succeeds and reports `not linked`, so the
command is safe to repeat.

Exit codes encode *which half* failed:

- **0** — the local credentials are gone. This includes a failed revoke, which is
  reported loudly on stderr but is not a failure of the command.
- **1** — a credential file survived the wipe (usually a Windows file lock from a
  running listener). A usable token is still on disk; close whatever is holding it
  and re-run.

Without `--yes` the command first fetches the inventory so the confirmation can
state the real consequence, the sibling skills at risk, and the automations that
will go idle. Those sentences come from the server, so the CLI and the settings
page cannot describe the same account differently.

**Example.**

```console
$ cremind google unlink gcalendar
Unlinking Google Calendar (gcalendar) for profile 'alice', linked as u@example.com:
  - deletes this machine's copy of the Google credentials
  - revokes Cremind's access at Google
  - Cremind stops reading and writing this Google Calendar. The Calendar & Schedule
    page falls back to the Google account connected on that page, or to the built-in
    system calendar if there is none — your scheduled events keep firing either way,
    and events already mirrored into Google stay in Google.
  - the gcalendar listener stops and its autostart registration is removed —
    register it again after re-linking (Settings -> Tools & Skills)
  - the same account is still linked in gdrive, so Google will NOT be told unless
    you also unlink those (or use --all)
  - 2 event automation(s) on this skill stop firing until you re-link and register
    its listener again
Re-linking needs another Google consent, which runs from chat.
Unlink gcalendar now? [y/N]: y
unlinked gcalendar (u@example.com)
revoked at Google: no
listener stopped; autostart registration removed
removed: scripts/.google_token.json, scripts/.listener_state.json
```

## Worked examples

**The user linked the wrong Google account to Gmail.**

```bash
cremind google status                  # confirm which address is linked
cremind google unlink gmail --yes      # wipe it and revoke
# then ask the agent to re-link the gmail skill in chat, as the right account
```

**Clean every Google link off this machine.**

```bash
cremind google unlink --all --yes
```

**Remove the local credential but keep the grant** (e.g. moving the install, and
the same account will be re-linked shortly):

```bash
cremind google unlink gdrive --no-revoke --yes
```

## Troubleshooting

**`unsupported_skill`** — the name is not one of the five Google Suite skills. Run
`cremind google status` to see the installed ones.

**`skill_not_installed`** — that skill is not installed for this profile, so it
holds no Google link. Nothing to do.

**`wipe_failed`** (exit 1) — the credential file could not be deleted, usually
because a running listener holds it open on Windows. The grant may already be
revoked, so the skill will now fail with `invalid_grant` until the file goes.
Stop the listener (Settings → Tools & Skills, or `cremind proc`) and re-run.

**`Google was NOT told: …`** (exit 0) — the local credentials are gone but the
revoke failed. Re-running will not help: the token that could revoke it has been
deleted. Remove Cremind at <https://myaccount.google.com/connections>.

**The other Google skills stopped working after unlinking one.** Expected when
they shared the account: Google revokes per app, not per skill. Re-link the
affected skills in chat.

**Events stopped firing after re-linking.** Unlinking removed the listener's
autostart registration. Register it again (Settings → Tools & Skills → the skill →
Register Process); the `skill_event_subscriptions` themselves were never deleted.

**`cremind google status` shows an account the user says they removed at Google.**
The local token file still exists — Google's own connections page does not reach
into this machine. Run `cremind google unlink <skill>` to clear it; the revoke will
report `already_revoked`, which counts as success.

**A restored backup brought a linked account back.** Backups include the skills'
token files, so a restore can resurrect a credential whose grant is already dead.
Unlink it again.

## Related

- `app/api/google.py` — the API these commands wrap.
- `app/google/unlink.py` — the teardown itself, and why its order is what it is.
- `cremind drive` — per-file Drive grants on an already-linked account.
- `cremind calendar google disconnect` — clears the credential connected on the
  Calendar & Schedule **page**, which is a *different* credential from the
  gcalendar skill's. Unlinking the skill hands the calendar back to it.
- `cremind tools` — where a skill's listener is registered again after re-linking.
