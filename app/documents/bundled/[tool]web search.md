---
description: "The Web Search (web_search) built-in tool and its Tool Variables: the search provider (parallel, duckduckgo, auto), the optional Parallel.ai API key, the DuckDuckGo region, and the DuckDuckGo safe-search level (strict, moderate, off). How to view and change each web_search variable per profile."
---

# Web Search Tool (web_search)

The **Web Search** tool (`tool_id` `web_search`) runs web queries for the agent.
It is on and visible by default; its backend and safe-search behavior are tuned
per profile.

## Tool Variables

| Variable | Type | Allowed / default | Meaning |
|----------|------|-------------------|---------|
| `WEB_SEARCH_PROVIDER` | enum | `parallel`, `duckduckgo`, `auto` (default `parallel`) | Search backend. `parallel` uses Parallel.ai's free keyless API. `duckduckgo` scrapes DuckDuckGo's HTML endpoint (experimental, rate-limited). `auto` tries `parallel` then falls back to `duckduckgo`. |
| `PARALLEL_API_KEY` | string (secret) | (empty) | Optional Parallel.ai API key for the higher-rate-limit authenticated endpoint. Empty = free anonymous tier. |
| `DDG_REGION` | string | (empty) | Optional DuckDuckGo region code (e.g. `us-en`, `uk-en`, `de-de`). Only used by the `duckduckgo` provider. |
| `DDG_SAFE_SEARCH` | enum | `strict`, `moderate`, `off` (default `moderate`) | Safe-search level for the `duckduckgo` provider. |

`PARALLEL_API_KEY` is a secret: its value is masked on read (shown as set/not
set, never the value). `web_search` has no Tool Arguments.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → Web Search.
- **CLI** — `cremind tools set-var web_search WEB_SEARCH_PROVIDER=auto DDG_SAFE_SEARCH=off`;
  `cremind tools get web_search --json` to read current values.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
