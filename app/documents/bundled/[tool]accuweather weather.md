---
description: "The AccuWeather Weather (accuweather_weather) built-in tool and its ACCUWEATHER_API_KEY secret variable — current conditions and forecast lookups. Also that the weather tool is disabled by default. How to view and change the accuweather_weather API key per profile."
---

# AccuWeather Weather Tool (accuweather_weather)

The **AccuWeather Weather** tool (`tool_id` `accuweather_weather`) returns current
conditions and forecasts via the AccuWeather API. It is **disabled by default**;
enable it from Settings → Tools & Skills or with
`cremind tools enable accuweather_weather`.

## Tool Variables

| Variable | Type | Meaning |
|----------|------|---------|
| `ACCUWEATHER_API_KEY` | string (secret) | AccuWeather API key. Required for the tool to work. |

`ACCUWEATHER_API_KEY` is a secret: its value is masked on read (shown as set/not
set, never the value). The tool has no Tool Arguments (the `location` and
current/forecast selector are per-call parameters the agent supplies).

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → AccuWeather Weather.
- **CLI** — `cremind tools set-var accuweather_weather ACCUWEATHER_API_KEY=...`;
  `cremind tools get accuweather_weather --json` to read current state.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
