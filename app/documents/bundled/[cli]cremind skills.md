---
description: "Install and remove agent skills with `cremind skills`: `import archive` from a local file, `import github` from a public repo, `import hub` from a Cremind Hub link/name, and `delete` an external skill or reset a built-in one to its shipped default. Skills are *listed* and *configured* via `cremind tools` (each skill is a tool) — this command covers only their install/uninstall lifecycle."
---

# `cremind skills` — Skill Install / Uninstall

`cremind skills` manages the *lifecycle* of agent skills: getting them onto a
profile and removing them again. It mirrors the skill import/delete controls on
the web UI's **Tools & Skills** settings page.

Skills surface as tools, so **listing and configuring** them happens through
`cremind tools` (`cremind tools list`, `tools get`, `tools set-var`, …). There
is deliberately no `skills list` here.

## Finding this in the web UI

> **Sidebar → Settings → Tools & Skills**

The "Import skill" menu (archive / GitHub / Hub) maps to `cremind skills
import`, and the per-skill delete / reset control maps to `cremind skills
delete`.

## Global flags

All subcommands accept the root-level `--json` flag. `CREMIND_TOKEN` is
required.

## Subcommands

### `cremind skills import archive`

**Purpose.** Install skills from a local archive (`.zip` / `.tar.gz`).

**Syntax.**

```bash
cremind skills import archive <path>
```

**Behavior.** Uploads the archive; the server extracts and installs every valid
skill directory it finds. Prints the installed skill names; any skipped
directories (name collisions, invalid names) are reported on stderr. With
`--json`, prints the raw `{installed, skipped}` result.

**Example.**

```bash
$ cremind skills import archive ./my-skills.zip
installed: daily-brief, invoice-parser
```

### `cremind skills import github`

**Purpose.** Install skills from a public GitHub repository.

**Syntax.**

```bash
cremind skills import github <repo>
```

`<repo>` is a full URL (`https://github.com/acme/skills`) or `owner/repo`.

**Example.**

```bash
$ cremind skills import github acme/agent-skills
installed: standup-bot
```

### `cremind skills import hub`

**Purpose.** Install a skill from Cremind Hub.

**Syntax.**

```bash
cremind skills import hub <ref>
```

`<ref>` is a Hub skill link (`https://hub.cremind.io/skills/<name>`) or a bare
skill name.

**Example.**

```bash
$ cremind skills import hub daily-brief
installed: daily-brief
```

### `cremind skills delete`

**Purpose.** Delete an external skill, or reset a built-in to its default.

**Syntax.**

```bash
cremind skills delete <tool_id> [--yes/-y]
```

**Flags.**

| Flag           | Type | Default | Meaning                       |
|----------------|------|---------|-------------------------------|
| `--yes`, `-y`  | bool | `false` | Skip the confirmation prompt. |

**Behavior.** For an **external** (imported) skill this is a permanent delete.
For a **built-in** skill the shipped copy is immediately restored — a
"reset to default" (its saved config is cleared too). The command prints
`deleted` or `reset to default` accordingly. Get the `<tool_id>` from
`cremind tools list`.

**Example.**

```bash
$ cremind skills delete daily-brief
Delete skill 'daily-brief'? (a built-in skill resets to its default) [y/N]: y
deleted
```

## Troubleshooting

**`server returned 404: Skill '<id>' not found`** — The id isn't a skill tool.
List skills with `cremind tools list` and copy the exact `tool_id`.

**`import` reports skips** — A discovered directory collided with a built-in or
an existing skill, or had an invalid name. The skip reason is printed on
stderr; rename the offending directory in the source and re-import.

**Windows PowerShell quoting** — When a repo URL or Hub link contains special
characters, wrap it in single quotes.
