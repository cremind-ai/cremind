---
description: "The Google Places (google_places) built-in tool and its configuration: the GOOGLE_MAPS_API_KEY secret variable and its latitude/longitude Tool Arguments (the user's default coordinates). Also that Google Places is disabled by default and needs the google feature. How to view and change the google_places settings per profile."
---

# Google Places Tool (google_places)

The **Google Places** tool (`tool_id` `google_places`) looks up nearby places
via the Google Maps Places API. It is **disabled by default** and requires the
`google` feature — install with `cremind features install google`, then
`cremind tools enable google_places`.

## Tool Variables

| Variable | Type | Meaning |
|----------|------|---------|
| `GOOGLE_MAPS_API_KEY` | string (secret) | Google Maps API key. Required for the tool to work. |

`GOOGLE_MAPS_API_KEY` is a secret: its value is masked on read (shown as set/not
set, never the value).

## Tool Arguments

Default coordinates used when the agent does not supply a location:

- `latitude` — number (required). The user's current latitude coordinate.
- `longitude` — number (required). The user's current longitude coordinate.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → Google Places.
- **CLI** — `cremind tools set-var google_places GOOGLE_MAPS_API_KEY=AIza...`;
  `cremind tools set-args google_places --json '{"latitude":10.77,"longitude":106.7}'`;
  `cremind tools get google_places --json` to read current values.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
