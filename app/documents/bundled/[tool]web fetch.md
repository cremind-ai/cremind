---
description: "The Web Fetch (web_fetch) built-in tool and its Tool Variable WEB_FETCH_MAX_CHARS — the default maximum characters of page content to return when the agent does not specify max_chars. How to view and change the web_fetch content cap per profile."
---

# Web Fetch Tool (web_fetch)

The **Web Fetch** tool (`tool_id` `web_fetch`) downloads a URL and returns its
content to the agent. It is on and visible by default.

## Tool Variables

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `WEB_FETCH_MAX_CHARS` | number | `20000` | Default maximum characters of page content to return when the agent does not specify `max_chars` on the call. |

`web_fetch` has no Tool Arguments.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → Web Fetch.
- **CLI** — `cremind tools set-var web_fetch WEB_FETCH_MAX_CHARS=50000`;
  `cremind tools get web_fetch --json` to read the current value.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
