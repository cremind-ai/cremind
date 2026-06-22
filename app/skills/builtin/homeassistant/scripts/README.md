# homeassistant skill — scripts

Python CLI + WebSocket listener for Home Assistant. Connects directly to an instance (no add-on,
no cloud relay). Run with `uv` (PEP 723 inline dependencies — no separate install step).

## Authenticate (pick one)

- **Long-Lived Access Token** (simplest): set `HA_TOKEN` in `scripts/.env` (HA Profile →
  Security → Long-Lived Access Tokens). ~10-year validity, no refresh.
- **OAuth 2.0**: leave `HA_TOKEN` empty and run `uv run scripts/__main__.py link`. A browser
  opens for consent; tokens are stored in `scripts/.ha_token.json` and refreshed automatically.

## Configure

`scripts/.env` (gitignored):

```
HA_URL=http://homeassistant.local:8123
HA_TOKEN=            # optional; leave blank to use `link` (OAuth)
# optional
HA_ENTITY_FILTER=light.*,switch.*,binary_sensor.*
HA_VERIFY_SSL=true
LOG_LEVEL=INFO
```

## CLI

```bash
uv run scripts/__main__.py check
uv run scripts/__main__.py link          # OAuth (only if HA_TOKEN unset)
uv run scripts/__main__.py unlink
uv run scripts/__main__.py list-entities --domain light
uv run scripts/__main__.py get-state --entity light.kitchen
uv run scripts/__main__.py sync-devices    # (re)build references/devices.md inventory
uv run scripts/__main__.py call-service --domain light --service turn_on --entity light.kitchen
```

## Event listener

```bash
uv run scripts/event_listener.py
```

Baselines on startup (emits nothing for existing state), then classifies each live state change
into a granular event type and drops it to `events/<event_type>/`. Reconnects automatically and
refreshes the OAuth token as needed. Stop with Ctrl-C.

It also maintains `references/devices.md` — a concise one-line-per-device inventory
(`entity_id | name | type | state`): a full snapshot on each (re)connect, and a single-line
in-place update per change. Use `sync-devices` to (re)build it without the listener.

## Tests

```bash
uv run scripts/tests/test_config.py
uv run scripts/tests/test_classify.py
uv run scripts/tests/test_listener.py
uv run scripts/tests/test_formatter.py
uv run scripts/tests/test_devices.py
# or: pytest scripts/tests/
```

See `../SKILL.md` for full documentation.
