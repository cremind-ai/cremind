---
description: "The Browser (browser) built-in tool and its Tool Variables: CDP URL for attaching to an existing browser, headless mode, the browser channel (chrome, msedge, chromium), the persistent user-data dir, executable path, and max snapshot content characters. Also that the browser tool is gated by the browser feature. How to view and change each browser variable per profile."
---

# Browser Tool (browser)

The **Browser** tool (`tool_id` `browser`) drives a real Chromium/Chrome/Edge
browser via Playwright for navigation, scraping, and interaction. It requires
the `browser` feature (Playwright + a browser binary); enabling it before the
feature is installed is rejected with HTTP 409 `FeatureNotInstalled` — install
with `cremind features install browser`, then `cremind tools enable browser`.

## Tool Variables

| Variable | Type | Allowed / default | Meaning |
|----------|------|-------------------|---------|
| `BROWSER_CDP_URL` | string | (empty) | Optional CDP URL to attach to an existing browser (e.g. `http://localhost:9222`). Empty = auto-launch Playwright's bundled Chromium. |
| `BROWSER_HEADLESS` | boolean | `false` | Run the auto-launched browser headless. Default is a visible window. |
| `BROWSER_CHANNEL` | enum | `chrome`, `msedge`, `chromium` (default `chrome`) | Playwright channel for auto-launch. `chrome` uses system Chrome, `msedge` system Edge, `chromium` Playwright's bundled build. |
| `BROWSER_USER_DATA_DIR` | string | (empty) | Directory for the persistent browser profile (cookies, logins). Default: `<CREMIND_SYSTEM_DIR>/browser-profile`. Do not point at your real Chrome profile while Chrome is running with it. |
| `BROWSER_EXECUTABLE_PATH` | string | (empty) | Optional absolute path to the browser executable. Empty = Playwright auto-discovers via the channel. |
| `BROWSER_MAX_CONTENT_CHARS` | number | `30000` | Maximum characters of page-snapshot content returned to the agent before truncation. |

`browser` has no Tool Arguments.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → Browser.
- **CLI** — `cremind tools set-var browser BROWSER_HEADLESS=true BROWSER_CHANNEL=chromium`;
  `cremind tools get browser --json` to read the current values and schema.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next launch — no restart. See `cremind tools`
for the full configuration CLI.
