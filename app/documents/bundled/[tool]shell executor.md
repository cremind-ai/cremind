---
description: "The Shell Executor (exec_shell) built-in tool and its Tool Variables: large-output handling mode and token threshold, long-running / output-wait timeouts, log silence threshold, terminal default cols/rows, cleanup TTL, and RTK token-compression settings. Also its target-OS argument. How to view and change each shell tool variable per profile."
---

# Shell Executor Tool (exec_shell)

The **Shell Executor** (`tool_id` `exec_shell`) runs shell commands and manages
long-running processes for the agent. It is a core tool: always on (locked) and
visible in Settings so its behavior can be tuned per profile.

## Tool Variables

| Variable | Type | Allowed / default | Meaning |
|----------|------|-------------------|---------|
| `LARGE_OUTPUT_MODE` | enum | `manual`, `automatic` (default `automatic`) | How to handle large command output. `manual`: ask the user before returning output exceeding the token threshold. `automatic`: always return full output regardless of size. |
| `LARGE_OUTPUT_TOKEN_THRESHOLD` | number | `10000` | Token count threshold for large-output handling. Only applies when `LARGE_OUTPUT_MODE` is `manual`. |
| `LOG_SILENCE_THRESHOLD` | number | `3` | Seconds of silence before closing the current log file and starting a new one for long-running processes. |
| `LONG_RUNNING_TIMEOUT` | number | `10` | Seconds before a process is reclassified as "long-running" and detached from the synchronous response. |
| `OUTPUT_WAIT_TIMEOUT` | number | `120` | Seconds a single output call long-polls for new output before returning a "still running" heartbeat. Clamped below `MCP_TOOL_CALL_TIMEOUT`. |
| `TERMINAL_DEFAULT_COLS` | number | `80` | Default terminal width (columns) for the Process Manager terminal view. |
| `TERMINAL_DEFAULT_ROWS` | number | `24` | Default terminal height (rows) for the Process Manager terminal view. |
| `CLEANUP_TTL_HOURS` | number | `24` | Hours before expired process data (logs, registry entries) is automatically cleaned up. |
| `RTK_ENABLED` | boolean | `false` | Route shell commands through RTK (Rust Token Killer) to filter/compress output and save LLM context tokens. Requires the `rtk` binary on PATH. |
| `RTK_BINARY_PATH` | string | `rtk` | Path to the `rtk` binary used when `RTK_ENABLED` is true. Set an absolute path when the spawned shell does not inherit your PATH. |

## Tool Arguments

`exec_shell` also has one **Tool Argument**:

- `os` — enum `Windows`, `Linux`, `Darwin`, `Auto-Detect` (default `Auto-Detect`).
  The operating system to target for shell selection.

## Viewing and changing these

All values are per-profile. Three equivalent ways:

- **UI** — Settings → Tools & Skills → Shell Executor.
- **CLI** — `cremind tools set-var exec_shell LARGE_OUTPUT_MODE=manual`;
  `cremind tools set-args exec_shell --json '{"os":"Linux"}'`;
  `cremind tools get exec_shell --json` to read the current values and schema.
- **Agent** — the assistant can run those same commands through its own Shell
  Executor (the shell has `CREMIND_SERVER`/`CREMIND_TOKEN` preset).

Changes take effect on the tool's next call — no server restart. See
`cremind tools` for the full configuration CLI.
