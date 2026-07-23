---
description: "Configure **LLM providers and models**: list and `configure` providers (add an API key), add your own **custom OpenAI-compatible providers** (name + base URL + model list) with `create-custom`, browse each provider's available models, assign the high / low / plan / vision / audio / default **model groups** the agent picks from (including a dedicated **plan model** for plan mode, a **vision model** for image_understanding, and an **audio model** for audio_understanding, each with a feature toggle), run the **GitHub Copilot** device-code login, and **Sign in with ChatGPT** (Codex OAuth) for the OpenAI provider via `codex-oauth login` — a browser sign-in that routes requests through your ChatGPT plan's Codex backend instead of an API key. Use this to add a provider (built-in or custom), choose which model the agent uses, enable the Specialized Vision/Audio Model, or authenticate a provider — distinct from `cremind config` (agent behavior) and `cremind agents` (MCP/A2A servers)."
---

# `cremind llm` — LLM Providers, Model Groups, and Device-Code Auth

`cremind llm` is the CLI for managing the LLM side of Cremind: which
providers are configured, what models each one exposes, which models
the agent should reach for at "high" and "low" reasoning effort, and
the GitHub Copilot device-code OAuth dance.

The group splits into three subcommand sets:

- **`providers`** — list, configure, and delete LLM provider configs,
  and `create-custom` your own OpenAI-compatible providers. Supports any
  provider that the server knows about (Anthropic, OpenAI, GitHub
  Copilot, etc.) plus user-defined **custom providers** (internal name
  `custom:<slug>`) for any OpenAI-API-compatible endpoint not in the
  built-in list.
- **`model-groups`** — read/write the `high` / `low` / `plan` model
  assignments and the default provider. The agent picks from these groups
  when it needs a heavyweight or lightweight model, or a dedicated model
  for plan mode's planning phase. (`vision` and `audio` are also
  assignable — used by the `image_understanding` and `audio_understanding`
  tools respectively — each with its own `--vision-enabled` / `--audio-enabled`
  feature toggle, also managed from the web UI's Specialized Vision/Audio Model
  toggles.)
- **`device-code`** — start and poll the device-code OAuth flow.
  Currently used only for GitHub Copilot; the same machinery is
  reusable for any future device-code provider.
- **`codex-oauth`** — "Sign in with ChatGPT" (Codex OAuth) for the OpenAI
  provider. `login` opens a browser sign-in and captures the tokens
  (locally, via a loopback listener on port 1455) or accepts a pasted
  redirect URL (`complete`) for remote installs. When this is the OpenAI
  provider's active auth method, requests run against ChatGPT's Codex
  backend under your plan — a different model list from the API-key models.

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
flow that `cremind llm device-code start` runs. On the **OpenAI** card,
selecting the **Sign in with ChatGPT** auth method shows a button that
runs the same Codex OAuth flow as `cremind llm codex-oauth login` (with a
"paste the redirect URL" fallback matching `codex-oauth complete`).

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

### `cremind llm providers create-custom`

**Purpose.** Add a **custom provider** — any OpenAI-API-compatible
endpoint that isn't in the built-in list — with your own name, API base
URL, API key, and a manually specified model list. Create as many as you
like; each gets a distinct internal name `custom:<slug>` (the slug is
derived from the display name and de-duplicated per profile). Custom
providers then behave like built-in ones: they show up in
`providers list`, `providers models`, and are assignable via
`model-groups set`.

**Syntax.**

```bash
cremind llm providers create-custom --name <name> --base-url <url> \
    [--api-key K] [--model <id> ...] [--models-json '[...]']
```

**Flags.**

| Flag             | Type       | Default | Meaning                                                                                 |
|------------------|------------|---------|-----------------------------------------------------------------------------------------|
| `--name`         | string     | —       | **Required.** Display name for the provider (e.g. `My Company LLM`).                     |
| `--base-url`     | string     | —       | **Required.** OpenAI-compatible base URL (e.g. `https://api.example.com/v1`).            |
| `--api-key`      | string     | `""`    | API key value. Stored server-side; never surfaced back.                                 |
| `--model`        | string (repeatable) | — | A model id to expose. Defaults to no vision/reasoning and unknown (untracked) cost. |
| `--models-json`  | string     | `""`    | Model list as a JSON array for per-model control (see below).                           |

At least one model is required (via `--model` and/or `--models-json`).
Custom models assume the default **128k context window**; a model with no
prices set has its cost left **untracked** (rather than shown as $0).

Each object in `--models-json` accepts:
`{"id": "...", "display_name": "...", "vision": bool, "supports_reasoning": bool, "input_price_per_1m": num, "output_price_per_1m": num, "cache_read_price_per_1m": num, "cache_write_price_per_1m": num}`.
Only `id` is required. `supports_reasoning: true` marks the model as a native
reasoner (the agent skips its own think-tool) and enables the Reasoning Effort
selector when the model is assigned; omit it (or pass `false`) for an ordinary
chat model. Omit any price field to leave that cost component untracked.

**Behavior.** Prints the new provider's internal `name` on success
(`custom:<slug>`), or the same object under `--json`.

**Examples.**

```bash
# Quick: two plain chat models
$ cremind llm providers create-custom \
    --name "My LLM" --base-url https://api.example.com/v1 \
    --api-key sk-... --model my-large --model my-small
name  custom:my-llm

# Rich: per-model vision / reasoning effort / pricing
$ cremind llm providers create-custom \
    --name "Acme" --base-url https://api.acme.ai/v1 --api-key sk-... \
    --models-json '[
      {"id":"acme-large","display_name":"Acme Large","vision":true,"input_price_per_1m":2.5,"output_price_per_1m":10,"cache_read_price_per_1m":0.25},
      {"id":"acme-mini","supports_reasoning":true}
    ]'

# Then assign it to the agent's main model
$ cremind llm model-groups set --high custom:my-llm/my-large
```

**Editing / deleting a custom provider.** Reuse the generic commands with
the `custom:<slug>` name. `configure` accepts `display_name`, `base_url`,
and a `models` array (as a JSON array) via `--json`, plus `--api-key`:

```bash
# Rename + swap base URL (key preserved if omitted)
$ cremind llm providers configure custom:my-llm \
    --json '{"display_name":"My LLM v2","base_url":"https://api.example.com/v2"}'

# Replace the model list
$ cremind llm providers configure custom:my-llm \
    --json '{"models":[{"id":"my-large","supports_reasoning":true,"input_price_per_1m":1.5,"output_price_per_1m":6}]}'

# Delete the whole custom provider (definition + key)
$ cremind llm providers delete-config custom:my-llm
```

### `cremind llm providers delete-config`

**Purpose.** Remove every stored config field for a provider — API
keys, OAuth tokens, base URLs, the lot. For a **custom provider**
(`custom:<slug>`) this also removes its definition (name, base URL, model
list) entirely and clears any model-group assignment that pointed at it.

**Syntax.**

```bash
cremind llm providers delete-config <provider>
```

**Behavior.** Silent on success. A built-in provider remains *known* (it
still appears in `providers list`) but its `configured` flag flips to
`no`. A custom provider is removed completely — it disappears from
`providers list`.

**Example.**

```bash
$ cremind llm providers delete-config openai
$ cremind llm providers delete-config custom:my-llm
```

### `cremind llm model-groups get`

**Purpose.** Show the current `high`, `low`, and `plan` model
assignments and the default provider.

**Syntax.**

```bash
cremind llm model-groups get
```

**Behavior.** Pretty-prints the JSON document the server returns. With
`--json`, the same JSON is emitted unindented. Optional groups (`low`,
`plan`, `vision`) that the user hasn't set come back as empty strings and
fall back to the `high` model at run time.

**Example.**

```bash
$ cremind llm model-groups get
{
  "default_provider": "anthropic",
  "model_groups": {
    "high": "anthropic/claude-opus-4-7",
    "low":  "anthropic/claude-haiku-4-5-20251001",
    "plan": "anthropic/claude-opus-4-7"
  }
}
```

### `cremind llm model-groups set`

**Purpose.** Update the high/low/plan/default assignments. Each flag is
optional, but at least one must be supplied.

**Syntax.**

```bash
cremind llm model-groups set [--high <id>] [--low <id>] [--plan <id>] \
  [--vision <id>] [--audio <id>] [--vision-enabled/--no-vision-enabled] \
  [--audio-enabled/--no-audio-enabled] [--default-provider <name>]
```

**Flags.**

| Flag                  | Type   | Default | Meaning                                                              |
|-----------------------|--------|---------|----------------------------------------------------------------------|
| `--high` / `--model`  | string | `""`    | Model id for the `high` group — the single model the agent reasons on.|
| `--low`               | string | `""`    | Model id for the `low` group (used for the skill classifier, etc.).  |
| `--plan`              | string | `""`    | Model id for the `plan` group — used in plan mode's planning phase (research, clarifying questions, writing the plan for approval, and after a cancel). Once a plan is accepted, execution switches back to the `high` model. Defaults to the `high` model when unset. |
| `--vision`            | string | `""`    | Model id for the `vision` group — used by the `image_understanding` tool. Defaults to the `high` model when unset. |
| `--audio`             | string | `""`    | Model id for the `audio` group — used by the `audio_understanding` tool. Defaults to the `high` model when unset. |
| `--vision-enabled` / `--no-vision-enabled` | bool | unset | Enable/disable the Specialized Vision Model feature (exposes the `image_understanding` tool through the `vision` group). |
| `--audio-enabled` / `--no-audio-enabled` | bool | unset | Enable/disable the Specialized Audio Model feature (exposes the `audio_understanding` tool through the `audio` group). |
| `--default-provider`  | string | `""`    | Provider name to use when no explicit provider is requested.         |

**Behavior.** Only the supplied fields are sent; omitted fields keep
their existing value. Silent on success.

**Examples.**

```bash
# Bump the high group to Opus
$ cremind llm model-groups set --high anthropic/claude-opus-4-7

# Use a strong model for planning and a cheaper model for everything else
# (planning runs on Opus; once you accept the plan, execution uses the high model)
$ cremind llm model-groups set --high anthropic/claude-haiku-4-5-20251001 --plan anthropic/claude-opus-4-7

# Switch the default provider away from Anthropic in one call
$ cremind llm model-groups set --default-provider openai --high openai/gpt-5

# Turn on the Specialized Audio Model feature and point it at an audio-capable model
$ cremind llm model-groups set --audio-enabled --audio openai/gpt-audio
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

### `cremind llm codex-oauth login`

**Purpose.** Sign in with ChatGPT (Codex OAuth) for the OpenAI provider,
so the agent can use your ChatGPT plan's Codex backend instead of an API
key. This sets the OpenAI provider's active auth method to Codex OAuth
and stores the access/refresh tokens server-side (auto-refreshed).

The Codex backend serves a **different, restricted model set** from the
API-key path (only GPT-5.x-class models). On successful sign-in, any model
group (`high` / `low` / `plan` / …) still pointing at an API-key-only
OpenAI model (e.g. `openai/gpt-4.1-mini`) is **auto-cleared** so it falls
back to the `high` model instead of failing at request time. Pick a
Codex-eligible model with `model-groups set` if you want a dedicated
`low`/`plan` model under Codex.

**Syntax.**

```bash
cremind llm codex-oauth login [--no-browser]
```

**Flags.**

| Flag           | Type | Default | Meaning                                              |
|----------------|------|---------|------------------------------------------------------|
| `--no-browser` | bool | `false` | Don't attempt to open the sign-in URL automatically. |

**Behavior.** Prints the ChatGPT sign-in URL (and tries to open it in
your browser unless `--no-browser`). On a **local** install the backend
captures the redirect automatically on port 1455 and the command polls
every 2 s until sign-in completes, then prints the account email and
plan. If the loopback listener can't run — **port 1455 is busy** (e.g. the
Codex CLI is mid-login) or the server is **remote** (Docker/K8s, where the
browser's `localhost` isn't the server) — the command prints the reason
and prompts you to paste the full redirect URL from your browser's
address bar. Ctrl-C aborts cleanly. With `--json`, the final status
object is printed.

**Example.**

```bash
$ cremind llm codex-oauth login
Open this URL to sign in with ChatGPT:
  https://auth.openai.com/oauth/authorize?...

Waiting for authorization (Ctrl-C to cancel)...
status  complete
email   you@example.com
plan    plus
```

### `cremind llm codex-oauth complete`

**Purpose.** Finish a Codex sign-in from a redirect URL you copied out of
the browser — for remote installs or scripted setups where the automatic
loopback capture isn't available.

**Syntax.**

```bash
cremind llm codex-oauth complete <redirect_url> [--state <state>]
```

**Arguments** (required):

- `<redirect_url>` — The full URL your browser landed on after approving
  access (starts with `http://localhost:1455/auth/callback?...`). It may
  fail to load in the browser — that's fine; only the URL matters.

**Flags.**

| Flag      | Type   | Default | Meaning                                                           |
|-----------|--------|---------|-------------------------------------------------------------------|
| `--state` | string | `""`    | The `state` value from `codex-oauth login` (cross-checked if set).|

**Behavior.** Exchanges the code server-side and stores the tokens. Prints
the account email + plan on success; exits non-zero with the error message
otherwise. A given sign-in request is only valid for ~10 minutes.

**Example (remote install).**

```bash
# On the (remote) server:
$ cremind llm codex-oauth login --no-browser
Open this URL to sign in with ChatGPT:
  https://auth.openai.com/oauth/authorize?...
Port 1455 is already in use ...  # or: automatic capture unavailable
# Open the URL in YOUR browser, approve, copy the address bar, then:
$ cremind llm codex-oauth complete 'http://localhost:1455/auth/callback?code=...&state=...'
status  complete
email   you@example.com
plan    pro
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

**`at least one of --model, --vision, --audio, --plan, --vision-enabled, --audio-enabled, --default-provider is required`** —
Same idea for `model-groups set` (the `high` group is set via `--model`,
also aliased `--high`). Use `model-groups get` first to see the current
values.

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

**`codex-oauth login` says port 1455 is in use** — Another process holds
the loopback port (often the Codex CLI mid-login). Close it and retry, or
paste the redirect URL when prompted / via `codex-oauth complete`.

**`codex-oauth` — "automatic capture unavailable"** — The server is remote
(Docker/K8s), so the browser's `localhost:1455` can't reach it. Open the
printed URL in your browser, approve, then run `codex-oauth complete` with
the URL from the address bar.

**`Unknown or expired sign-in request`** — A sign-in request lives ~10
minutes and is dropped if the server restarts. Run `codex-oauth login`
again to start a fresh one.

**Codex sign-in worked but requests fail with "sign-in has expired"** —
The refresh token was revoked or expired (e.g. after a long offline
period), or you previously pasted a raw access token (which has no refresh
token). Run `cremind llm codex-oauth login` again. Signing out is
`cremind llm providers delete-config openai`.

**`documentation_search` (or another auxiliary tool) returns nothing after
signing in with ChatGPT** — A model group was still pointing at an
API-key-only OpenAI model (e.g. `low = openai/gpt-4.1-mini`) that the Codex
backend rejects (`the '<model>' model is not supported when using Codex
with a ChatGPT account`). Sign-in now auto-clears such groups, and the
resolver self-heals a stale value by falling back to the `high` model, so
this should no longer happen. If you want a dedicated cheap model under
Codex, run e.g. `cremind llm model-groups set --low openai/gpt-5.4-mini`.
