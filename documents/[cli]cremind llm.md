---
description: "Complete reference for the `cremind llm` CLI command — the terminal-side counterpart to the **Settings → LLM Providers** page in the Cremind web UI — covering how to list and configure LLM providers, browse the models each provider exposes, assign the high/low/default model groups the agent picks from, and run the GitHub Copilot device-code authentication flow. Documents every subcommand under `providers`, `model-groups`, and `device-code`, the JSON shape sent to `providers configure`, the exact polling behavior of `device-code poll`, and the fields rendered in each table."
---

# `cremind llm` — LLM Providers, Model Groups, and Device-Code Auth

`cremind llm` is the CLI for managing the LLM side of Cremind: which
providers are configured, what models each one exposes, which models
the agent should reach for at "high" and "low" reasoning effort, and
the GitHub Copilot device-code OAuth dance.

The group splits into three subcommand sets:

- **`providers`** — list, configure, and delete LLM provider configs.
  Supports any provider that the server knows about (Anthropic, OpenAI,
  GitHub Copilot, etc.).
- **`model-groups`** — read/write the `high` / `low` model assignments
  and the default provider. The agent picks from these groups when it
  needs a heavyweight or lightweight model.
- **`device-code`** — start and poll the device-code OAuth flow.
  Currently used only for GitHub Copilot; the same machinery is
  reusable for any future device-code provider.

Provider configuration values like API keys are stored server-side and
never exposed back to the CLI in subsequent reads — they show up only
as a `configured: yes` flag.

## Finding this in the web UI

Every operation in this group has a control on the **LLM Providers**
page of the Cremind web UI:

> **Sidebar → Settings → LLM Providers**

The page lists each provider as a card showing whether it is
configured, the active auth method, and the model count (mirroring
`cremind llm providers list`). The card's "Configure" panel matches
`cremind llm providers configure`, and the **Model groups** section at the
top of the page matches `cremind llm model-groups get/set`. The
**Sign in with GitHub Copilot** button kicks off the same device-code
flow that `cremind llm device-code start` runs.

## Global flags

All `cremind llm` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand in this group.

## Subcommands

### `cremind llm providers list`

**Purpose.** Show every provider the server knows about, with
configuration status.

**Syntax.**

```bash
cremind llm providers list
```

**Behavior.** Prints a five-column table:

| Column        | Source field         | Meaning                                                    |
|---------------|----------------------|------------------------------------------------------------|
| `NAME`        | `name`               | Internal provider id (e.g. `anthropic`, `openai`).         |
| `DISPLAY`     | `display_name`       | Human-friendly name shown in the UI.                       |
| `CONFIGURED`  | `configured`         | `yes` if any config has been written for this provider.    |
| `MODELS`      | `model_count`        | Number of models discovered for this provider.             |
| `ACTIVE_AUTH` | `active_auth_method` | The auth method currently selected (e.g. `anthropic`, `oauth_personal`). |

With `--json`, returns the underlying array unchanged.

**Example.**

```bash
$ cremind llm providers list
NAME       DISPLAY              CONFIGURED  MODELS  ACTIVE_AUTH
anthropic  Anthropic            yes         8       anthropic
openai     OpenAI               no          0
copilot    GitHub Copilot       yes         12      oauth_personal
```

### `cremind llm providers models`

**Purpose.** List the models a single provider exposes.

**Syntax.**

```bash
cremind llm providers models <provider>
```

**Arguments** (required):

- `<provider>` — Provider id (matches `NAME` from `providers list`).

**Behavior.** Prints a two-column table of model id and display name.
With `--json`, returns the full provider response (which may include
extra metadata such as context windows and capability flags).

**Example.**

```bash
$ cremind llm providers models anthropic
ID                            NAME
claude-opus-4-7               Claude Opus 4.7
claude-sonnet-4-6             Claude Sonnet 4.6
claude-haiku-4-5-20251001     Claude Haiku 4.5
```

### `cremind llm providers configure`

**Purpose.** Set provider configuration — typically the API key and
the active auth method, plus any extra fields the provider supports.

**Syntax.**

```bash
cremind llm providers configure <provider> [--api-key K] [--auth-method M] [--json '{...}']
```

**Arguments** (required):

- `<provider>` — Provider id to configure.

**Flags.**

| Flag             | Type   | Default | Meaning                                                        |
|------------------|--------|---------|----------------------------------------------------------------|
| `--api-key`      | string | `""`    | API key value. Stored server-side; never surfaced back.        |
| `--auth-method`  | string | `""`    | Active auth method id (e.g. `anthropic`, `oauth_personal`).    |
| `--json`         | string | `""`    | Additional fields as a JSON object, merged into the body.      |

At least one of the three flags must be supplied. When `--json` is
combined with `--api-key` or `--auth-method`, the typed flags overwrite
their counterparts in the JSON.

**Behavior.** Silent on success. Validation is server-side: if the
provider does not accept a flag, the command returns a 4xx error and
nothing is changed.

**Examples.**

```bash
# Standard API key
$ cremind llm providers configure anthropic --api-key sk-ant-...

# Switch the active auth method without touching the key
$ cremind llm providers configure copilot --auth-method oauth_business

# Extra fields (e.g. base URL or organization id) via JSON
$ cremind llm providers configure openai --json '{"organization":"org-...","base_url":"https://api.openai.com/v1"}'
```

### `cremind llm providers delete-config`

**Purpose.** Remove every stored config field for a provider — API
keys, OAuth tokens, base URLs, the lot.

**Syntax.**

```bash
cremind llm providers delete-config <provider>
```

**Behavior.** Silent on success. The provider remains *known* (it will
still appear in `providers list`), but its `configured` flag flips to
`no` and any models that depended on the config become unavailable.

**Example.**

```bash
$ cremind llm providers delete-config openai
```

### `cremind llm model-groups get`

**Purpose.** Show the current `high` and `low` model assignments and
the default provider.

**Syntax.**

```bash
cremind llm model-groups get
```

**Behavior.** Pretty-prints the JSON document the server returns. With
`--json`, the same JSON is emitted unindented.

**Example.**

```bash
$ cremind llm model-groups get
{
  "default_provider": "anthropic",
  "model_groups": {
    "high": "anthropic/claude-opus-4-7",
    "low":  "anthropic/claude-haiku-4-5-20251001"
  }
}
```

### `cremind llm model-groups set`

**Purpose.** Update the high/low/default assignments. Each flag is
optional, but at least one must be supplied.

**Syntax.**

```bash
cremind llm model-groups set [--high <id>] [--low <id>] [--default-provider <name>]
```

**Flags.**

| Flag                  | Type   | Default | Meaning                                                              |
|-----------------------|--------|---------|----------------------------------------------------------------------|
| `--high`              | string | `""`    | Model id for the `high` group (used for heavyweight reasoning).      |
| `--low`               | string | `""`    | Model id for the `low` group (used for the skill classifier, etc.).  |
| `--default-provider`  | string | `""`    | Provider name to use when no explicit provider is requested.         |

**Behavior.** Only the supplied fields are sent; omitted fields keep
their existing value. Silent on success.

**Examples.**

```bash
# Bump the high group to Opus
$ cremind llm model-groups set --high anthropic/claude-opus-4-7

# Switch the default provider away from Anthropic in one call
$ cremind llm model-groups set --default-provider openai --high openai/gpt-5
```

### `cremind llm device-code start`

**Purpose.** Begin a device-code OAuth flow (currently used for GitHub
Copilot). The server returns a verification URL and a short user code
that you enter on that page.

**Syntax.**

```bash
cremind llm device-code start
```

**Behavior.** Prints the response as a key-value table:

| Row                | Meaning                                                      |
|--------------------|--------------------------------------------------------------|
| `verification_uri` | URL the user opens in a browser.                             |
| `user_code`        | Short code the user types into the page above.               |
| `device_code`      | Opaque string passed to `cremind llm device-code poll`.          |
| `expires_in`       | Seconds until the device code becomes invalid.               |
| `interval`         | Recommended seconds between poll attempts.                   |

**Example.**

```bash
$ cremind llm device-code start
verification_uri  https://github.com/login/device
user_code         ABCD-1234
device_code       4fe...e8c
expires_in        900
interval          5
```

### `cremind llm device-code poll`

**Purpose.** Block until the user finishes the OAuth flow in their
browser, then store the resulting access token server-side.

**Syntax.**

```bash
cremind llm device-code poll <device_code>
```

**Arguments** (required):

- `<device_code>` — The opaque code printed by `device-code start`.

**Behavior.** Polls the server every five seconds (the polling interval
is increased by another five seconds whenever the upstream returns
`slow_down`). On `pending`, the loop sleeps and tries again; on
`complete`, the command exits 0 and prints either the access token (if
the server elected to surface it) or the literal message
`complete (token stored server-side)`. On `expired`, exits with an
error suggesting `device-code start` again. On `error`, exits with the
upstream error message. Ctrl-C aborts the loop cleanly.

With `--json`, the final response object is emitted exactly as the
server returned it.

**Example.**

```bash
$ cremind llm device-code poll 4fe...e8c
complete (token stored server-side)
```

## Worked examples

### Bootstrap Anthropic and verify the model list

```bash
$ cremind llm providers configure anthropic --api-key sk-ant-...
$ cremind llm providers list
NAME       DISPLAY    CONFIGURED  MODELS  ACTIVE_AUTH
anthropic  Anthropic  yes         8       anthropic
$ cremind llm providers models anthropic
```

### One-shot GitHub Copilot login

```bash
$ resp=$(cremind llm device-code start --json)
$ echo "$resp" | jq -r .verification_uri
https://github.com/login/device
$ echo "$resp" | jq -r .user_code
ABCD-1234

# Open the URL, enter the code, then:
$ cremind llm device-code poll "$(echo "$resp" | jq -r .device_code)"
```

### Switch the agent to a faster low-tier model

```bash
$ cremind llm model-groups get --json | jq .model_groups
$ cremind llm model-groups set --low anthropic/claude-haiku-4-5-20251001
```

### Rotate an Anthropic API key

```bash
$ cremind llm providers configure anthropic --api-key sk-ant-NEWKEY
```

### Pipe a provider list into `jq` to find unconfigured ones

```bash
$ cremind llm providers list --json | jq -r '.[] | select(.configured==false) | .name'
openai
```

## Troubleshooting

**`at least one of --api-key, --auth-method, or --json is required`** —
`providers configure` rejects empty bodies. Pass at least one flag.

**`at least one of --high, --low, --default-provider is required`** —
Same idea for `model-groups set`. Use `model-groups get` first to see
the current values.

**`device-code poll` hangs forever** — That is the expected behavior
while the user has not yet completed the browser flow. Ctrl-C is safe
and does not invalidate the device code.

**`device code expired`** — Run `cremind llm device-code start` again to
obtain a new code; codes are short-lived (`expires_in` seconds, usually
~15 minutes).

**Provider is configured but `MODELS` is 0** — The server discovered
the provider but could not enumerate its models, usually because the
key is wrong or the provider was unreachable when the worker last ran.
Re-run `providers configure` with a known-good key, then refresh the
list.
