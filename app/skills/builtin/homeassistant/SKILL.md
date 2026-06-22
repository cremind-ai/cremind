---
name: homeassistant
description: Read entity states, call services (turn lights/switches on/off, set values, run scripts), and receive granular real-time events (motion detected, door opened, light turned on, person arrived home, ...) from a self-hosted or HA Cloud Home Assistant instance. Connects directly — no add-on, no cloud relay. Authenticate with a pasted Long-Lived Access Token, or leave it blank and authorize via the OAuth 2.0 browser flow.
metadata: {
  environment_variables: [
    {"name": "HA_URL", "description": "Home Assistant base URL, e.g. http://homeassistant.local:8123", "required": true, "type": "string"},
    {"name": "HA_TOKEN", "description": "Long-Lived Access Token (leave blank to authorize via the OAuth browser flow)", "required": false, "secret": true, "type": "string"},
    {"name": "HA_ENTITY_FILTER", "description": "Comma-separated entity globs (fnmatch); empty = all entities", "required": false, "type": "string", "default": ""},
    {"name": "HA_VERIFY_SSL", "description": "Verify TLS certificates (turn off for self-signed/local certs)", "required": false, "type": "boolean", "default": "true"},
    {"name": "LOG_LEVEL", "description": "Logging verbosity", "required": false, "type": "enum", "enum": ["DEBUG", "INFO", "WARNING", "ERROR"], "default": "INFO"}
  ],
  events: {"event_type": [
    {"name": "became_unavailable", "description": "An entity went offline / its state became unavailable or unknown"},
    {"name": "became_available", "description": "An entity came back online from an unavailable/unknown state"},
    {"name": "light_turned_on", "description": "A light was turned on"},
    {"name": "light_turned_off", "description": "A light was turned off"},
    {"name": "switch_turned_on", "description": "A switch was turned on"},
    {"name": "switch_turned_off", "description": "A switch was turned off"},
    {"name": "fan_turned_on", "description": "A fan was turned on"},
    {"name": "fan_turned_off", "description": "A fan was turned off"},
    {"name": "input_boolean_turned_on", "description": "An input_boolean helper was turned on"},
    {"name": "input_boolean_turned_off", "description": "An input_boolean helper was turned off"},
    {"name": "lock_locked", "description": "A lock was locked"},
    {"name": "lock_unlocked", "description": "A lock was unlocked"},
    {"name": "cover_opened", "description": "A cover (garage door, blind, shade) finished opening"},
    {"name": "cover_closed", "description": "A cover finished closing"},
    {"name": "cover_opening", "description": "A cover started opening"},
    {"name": "cover_closing", "description": "A cover started closing"},
    {"name": "motion_detected", "description": "A motion sensor detected motion"},
    {"name": "motion_cleared", "description": "A motion sensor cleared (no more motion)"},
    {"name": "occupancy_detected", "description": "An occupancy/presence sensor detected presence"},
    {"name": "occupancy_cleared", "description": "An occupancy/presence sensor cleared"},
    {"name": "door_opened", "description": "A door (or garage door) binary sensor reports open"},
    {"name": "door_closed", "description": "A door (or garage door) binary sensor reports closed"},
    {"name": "window_opened", "description": "A window/opening binary sensor reports open"},
    {"name": "window_closed", "description": "A window/opening binary sensor reports closed"},
    {"name": "moisture_detected", "description": "A leak/moisture sensor detected water"},
    {"name": "moisture_cleared", "description": "A leak/moisture sensor cleared"},
    {"name": "smoke_detected", "description": "A smoke/gas/CO sensor triggered"},
    {"name": "smoke_cleared", "description": "A smoke/gas/CO sensor cleared"},
    {"name": "binary_sensor_on", "description": "A binary_sensor (no specific device_class) turned on"},
    {"name": "binary_sensor_off", "description": "A binary_sensor (no specific device_class) turned off"},
    {"name": "person_arrived_home", "description": "A person arrived home"},
    {"name": "person_left_home", "description": "A person left home"},
    {"name": "person_location_changed", "description": "A person moved to a different (non-home) zone"},
    {"name": "device_arrived_home", "description": "A device tracker arrived home"},
    {"name": "device_left_home", "description": "A device tracker left home"},
    {"name": "climate_changed", "description": "A climate/thermostat entity changed (mode or target)"},
    {"name": "alarm_armed", "description": "An alarm control panel was armed (home/away/night)"},
    {"name": "alarm_disarmed", "description": "An alarm control panel was disarmed"},
    {"name": "alarm_triggered", "description": "An alarm control panel was triggered"},
    {"name": "media_started_playing", "description": "A media player started playing"},
    {"name": "media_paused", "description": "A media player was paused"},
    {"name": "media_stopped", "description": "A media player stopped / went idle / turned off"},
    {"name": "temperature_changed", "description": "A temperature sensor value changed"},
    {"name": "humidity_changed", "description": "A humidity sensor value changed"},
    {"name": "power_changed", "description": "A power/energy sensor value changed"},
    {"name": "battery_level_changed", "description": "A battery sensor value changed"},
    {"name": "sensor_value_changed", "description": "A sensor (no specific device_class) value changed"},
    {"name": "state_changed", "description": "An entity changed state and matched no more specific event type"}
  ]},
  long_running_app: {
    command: "uv run scripts/event_listener.py",
    description: "Persistent Home Assistant WebSocket listener. Authenticates (LLAT or OAuth), subscribes to state_changed, and drops granular, classified state changes as markdown.",
  }
}
---

# homeassistant

**Purpose:** Python CLI over the Home Assistant REST API (read states, call services) plus a
persistent WebSocket listener that classifies real-time state changes into **granular event
types** (motion detected, door opened, light turned on, person arrived home, ...). Connects
**directly** to your instance — no add-on, no cloud relay. Runs via `uv` (PEP 723 inline metadata).

## Authentication — two ways

`HA_URL` is always required. For the token, pick **one**:

**A. Long-Lived Access Token (simplest, recommended).** In Home Assistant: profile (bottom-left)
→ **Security** → **Long-Lived Access Tokens** → **Create Token**. Copy it (shown once) and set
`HA_TOKEN` in `scripts/.env`. ~10-year validity, no refresh.

**B. OAuth 2.0 browser flow.** Leave `HA_TOKEN` empty and run `uv run scripts/__main__.py link`.
A browser opens to Home Assistant's login/consent page; on approval the skill stores an access
token (30 min) + refresh token locally in `scripts/.ha_token.json` and refreshes automatically.
This uses HA's built-in IndieAuth OAuth — no client secret, loopback redirect, nothing to register.

If `HA_TOKEN` is set it always takes precedence and `link` is unnecessary.

## Setup

`scripts/.env`:

```
HA_URL=http://homeassistant.local:8123
# Option A — paste a Long-Lived Access Token (leave blank to use OAuth `link` instead):
HA_TOKEN=

# Optional: comma-separated entity globs (fnmatch). Empty = ALL entities (noisy!).
HA_ENTITY_FILTER=light.*,switch.*,binary_sensor.*,person.*
# Optional: set to false for self-signed / local TLS certificates.
HA_VERIFY_SSL=true
# Optional: DEBUG / INFO / WARNING
LOG_LEVEL=INFO
```

`HA_URL` is the address this machine uses to reach Home Assistant. On the same LAN this is
typically `http://homeassistant.local:8123` (or the box's IP). For a remote machine, use the
instance's public **HTTPS** URL (Home Assistant Cloud / Nabu Casa, a reverse proxy, or a
tunnel) — `https://` automatically switches the listener to a `wss://` WebSocket. A purely
LAN-only instance is not reachable from a different network.

## CLI Commands

Run `uv run scripts/__main__.py <subcommand>`. Output is JSON (or human-readable on a TTY;
force JSON with `--json`).

| Subcommand | Required | Optional |
|---|---|---|
| `check` | — | — |
| `link` | — | `--no-browser`, `--timeout N` (300) |
| `unlink` | — | — |
| `list-entities` | — | `--domain light`, `--query STR`, `--max-results N` (200) |
| `get-state` | `--entity light.kitchen` | — |
| `states` | — | `--domain`, `--query` |
| `sync-devices` | — | — |
| `call-service` | `--domain`, `--service` | `--entity light.kitchen`, `--data '<json>'` |

- `check` validates auth + connectivity and reports the auth mode (`llat`/`oauth`), HA version,
  location name, and entity count.
- `link` runs the OAuth browser flow (only when `HA_TOKEN` is unset); `unlink` revokes and
  removes the stored OAuth tokens.
- `list-entities` / `states` filter client-side by `--domain` (the part before the dot in an
  `entity_id`) and `--query` (case-insensitive substring over `entity_id` + friendly name).
- `sync-devices` (re)builds `references/devices.md` — a concise, one-line-per-device inventory
  (see below) — from the current states, filtered by `HA_ENTITY_FILTER`. The listener maintains
  this file automatically; run this verb to populate it before the listener's first run, or to
  force a fresh snapshot.
- `call-service` controls devices. `--entity` is sugar for `--data '{"entity_id": "..."}'`.
  Calling a service with **no** `--entity` and **no** `--data` can affect every matching device
  (e.g. every light) — the CLI warns when both are absent.

## Examples

```bash
# Validate connection (and see which auth mode is active)
uv run scripts/__main__.py check

# OAuth (only if HA_TOKEN is not set)
uv run scripts/__main__.py link

# Browse entities
uv run scripts/__main__.py list-entities --domain light
uv run scripts/__main__.py list-entities --query temperature

# Read one entity (state + attributes)
uv run scripts/__main__.py get-state --entity light.kitchen

# Build the concise device inventory at references/devices.md (one line per device)
uv run scripts/__main__.py sync-devices

# Turn a light on / off
uv run scripts/__main__.py call-service --domain light --service turn_on --entity light.kitchen
uv run scripts/__main__.py call-service --domain light --service turn_off --entity light.kitchen

# Service with extra data
uv run scripts/__main__.py call-service --domain light --service turn_on \
    --data '{"entity_id": "light.kitchen", "brightness": 200, "color_name": "tomato"}'
```

## Device inventory (`references/devices.md`)

`references/devices.md` is a concise, **always-current snapshot of every tracked device** — one
line per entity, so the whole picture loads cheaply without an API round trip:

```
light.kitchen | Kitchen Light | light | on
binary_sensor.front_door | Front Door | binary_sensor/door | off
sensor.living_room | Living Room Temp | sensor/temperature | 21.5
```

The format is `entity_id | name | type | state`, where **type** is the entity's domain, or
`domain/device_class` when it has one (e.g. `binary_sensor/motion`, `sensor/temperature`).

- It mirrors `HA_ENTITY_FILTER` — exactly the entities the listener watches.
- **Auto-maintained — do not hand-edit.** The listener full-syncs the whole file on every
  (re)connect and rewrites only the **single changed line** on each state change; `sync-devices`
  rebuilds it on demand. Manual edits are overwritten.
- It exists only after the listener has run once (or after `sync-devices`).

Read this file when you need the current name / type / state of devices at a glance.

Before interacting with any device, always load the `references/devices.md` file to check all device information and status.

## Entity filtering (important)

A busy Home Assistant instance fires **hundreds** of `state_changed` events per minute (every
sensor tick, every device-tracker update). Set `HA_ENTITY_FILTER` to the entities you actually
care about, or the listener will create a flood of event files. Patterns are `fnmatch` globs
matched against `entity_id`, comma-separated:

```
HA_ENTITY_FILTER=light.*,switch.*,binary_sensor.front_door,sensor.*_temperature
```

An empty filter matches **all** entities; the listener logs a warning when it is unset.

## Event listener

Run persistently to capture state changes in real time:

```bash
uv run scripts/event_listener.py
```

Behavior:
- **Baseline on startup**: snapshots the current state of every (filtered) entity and emits
  **nothing** for pre-existing values. Only changes that happen while the listener is running
  produce events.
- **Real-time**: holds a WebSocket to `/api/websocket`, authenticates (LLAT or a refreshed OAuth
  access token), and subscribes to `state_changed`.
- **Classification**: each change is mapped to a **granular event type** (see below) and written
  to `events/<event_type>/<YYYY-MM-DDTHH-MM-SS> <friendly name>.md`.
- **Device inventory**: maintains `references/devices.md` (one line per tracked device,
  `entity_id | name | type | state`) — a full snapshot on each (re)connect, and a single-line
  in-place update on each change. See [Device inventory](#device-inventory-referencesdevicesmd).
- **State**: persisted to `scripts/.listener_state.json` (gitignored). Delete it to force a
  re-baseline. Auto-wipes if `HA_URL` changes.
- **Reconnect**: on dropped sockets or HA restarts, the listener reconnects, re-authenticates
  (refreshing the OAuth token if needed), and re-subscribes with exponential backoff. State
  changes that occur **while disconnected are not replayed** by Home Assistant.
- **Shutdown**: SIGINT / SIGTERM stops cleanly.

### Event types

Each raw `state_changed` is classified by the entity's domain, `device_class`, and transition.
Subscribe to exactly the ones you care about. Categories:

- **Availability:** `became_unavailable`, `became_available`
- **On/off devices:** `light_turned_on/off`, `switch_turned_on/off`, `fan_turned_on/off`,
  `input_boolean_turned_on/off`
- **Locks:** `lock_locked`, `lock_unlocked`
- **Covers:** `cover_opened`, `cover_closed`, `cover_opening`, `cover_closing`
- **Binary sensors (by device_class):** `motion_detected/cleared`, `occupancy_detected/cleared`,
  `door_opened/closed`, `window_opened/closed`, `moisture_detected/cleared`,
  `smoke_detected/cleared`, and `binary_sensor_on/off` (fallback)
- **Presence:** `person_arrived_home`, `person_left_home`, `person_location_changed`,
  `device_arrived_home`, `device_left_home`
- **Climate:** `climate_changed`
- **Alarm:** `alarm_armed`, `alarm_disarmed`, `alarm_triggered`
- **Media:** `media_started_playing`, `media_paused`, `media_stopped`
- **Sensors (by device_class):** `temperature_changed`, `humidity_changed`, `power_changed`,
  `battery_level_changed`, and `sensor_value_changed` (fallback)
- **Fallback:** `state_changed` (any change not matching a more specific type)

### Event markdown schema

```markdown
---
entity_id: "binary_sensor.front_door"
friendly_name: "Front Door"
domain: "binary_sensor"
state: "on"
previous_state: "off"
last_changed: "2026-06-14T18:20:01.123456+00:00"
last_updated: "2026-06-14T18:20:01.123456+00:00"
attributes: "{\"device_class\": \"door\", \"friendly_name\": \"Front Door\"}"
event_type: "door_opened"
received_at: "2026-06-14T18:20:02+07:00"
---

Front Door changed from off to on.
```

`attributes` is a JSON string (truncated for very large attribute blobs such as cameras/weather).

## Troubleshooting

- `Authentication failed` / `auth rejected` → the `HA_TOKEN` is wrong/revoked, or the OAuth
  session expired. Create a new Long-Lived Access Token (Profile → Security), or re-run `link`.
- `Home Assistant is not linked` → no `HA_TOKEN` and no stored OAuth token. Set `HA_TOKEN` or run
  `link`.
- `Failed to reach Home Assistant` / connection refused → check `HA_URL`, the port (default
  8123), and that this machine can reach the instance. A LAN-only instance is unreachable from a
  different network.
- `SSL error` → for self-signed/local certificates, set `HA_VERIFY_SSL=false` in `scripts/.env`.
- Listener emits a flood of files → set `HA_ENTITY_FILTER` to the entities you care about.
- A user restricted to "local network only" login cannot complete the OAuth flow remotely, even
  on a publicly reachable instance.

## Module layout

```
homeassistant/
├── SKILL.md
├── events/
│   └── <event_type>/              # one markdown drop-zone per classified event type
├── references/
│   └── devices.md                 # auto-maintained current device inventory (untracked at runtime)
└── scripts/
    ├── .env                       # HA_URL (+ optional HA_TOKEN and other vars)
    ├── __main__.py                # CLI entry (uv run scripts/__main__.py ...)
    ├── event_listener.py          # listener entry (uv run scripts/event_listener.py)
    └── app/
        ├── config.py              # env loading + paths + ws_url() + logging
        ├── errors.py              # HaError / AuthError
        ├── auth.py                # LLAT + OAuth (IndieAuth loopback) token management
        ├── homeassistant_api.py   # HaRestClient (requests) + HaWebSocketClient (websocket-client)
        ├── classify.py            # state_changed -> granular event type
        ├── operations.py          # verbs: check / list-entities / get-state / states / sync-devices / call-service
        ├── devices.py             # references/devices.md inventory (full_sync + single-line upsert/remove)
        ├── formatter.py           # entity rows + event markdown
        ├── listener.py            # WebSocket loop, classify, atomic event writes, reconnect
        └── cli.py                 # argparse builder + dispatch
```
