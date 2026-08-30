---
description: "The Shell Executor (exec_shell) built-in tool and its Tool Variables: large-output handling mode and token threshold, long-running / output-wait timeouts, the output quiet window that batches bursty process output into fewer reads, log silence threshold, terminal default cols/rows, cleanup TTL, and RTK token-compression settings. Also its target-OS argument and the exec_shell_output read parameters (wait_until, quiet_window, max_wait). How to view and change each shell tool variable per profile."
---

# Shell Executor Tool (exec_shell)

The **Shell Executor** (`tool_id` `exec_shell`) runs shell commands and manages
long-running processes for the agent. It is a core tool: always on (locked) and
visible in Settings so its behavior can be tuned per profile.

## Tool Variables

| Variable | Type | Allowed / default | Meaning |
|----------|------|-------------------|---------|
| `LARGE_OUTPUT_MODE` | enum | `manual`, `automatic` (default `automatic`) | How to handle large command output. `manual`: ask the user before returning output exceeding the token threshold. `automatic`: always return full output regardless of size. |
| `LARGE_OUTPUT_TOKEN_THRESHOLD` | number | `10000` | Token count threshold for large-output handling. When `LARGE_OUTPUT_MODE` is `manual`, output above this is withheld pending user confirmation. In both modes it also caps how much a single output call accumulates before returning, so a process flooding stdout still comes back promptly. |
| `LOG_SILENCE_THRESHOLD` | number | `3` | Seconds of silence before closing the current log file and starting a new one for long-running processes. |
| `LONG_RUNNING_TIMEOUT` | number | `10` | Seconds before a process is reclassified as "long-running" and detached from the synchronous response. |
| `OUTPUT_WAIT_TIMEOUT` | number | `120` | Total seconds a single output call may block. While nothing has arrived it long-polls, then returns a "still running" heartbeat; once output starts arriving it keeps collecting until the process goes quiet (see `OUTPUT_QUIET_WINDOW`) or exits. Clamped below `MCP_TOOL_CALL_TIMEOUT`. |
| `OUTPUT_QUIET_WINDOW` | number | `2` | Seconds of silence, once output has started arriving, before an output call returns its accumulated batch. Chatty processes emit in short bursts and each returned batch costs the agent a reasoning step, so a larger window means fewer, bigger reads at the cost of seeing output slightly later. Interactive prompts and process exits still return immediately. `0` returns after the first burst. |
| `TERMINAL_DEFAULT_COLS` | number | `80` | Default terminal width (columns) for the Process Manager terminal view. |
| `TERMINAL_DEFAULT_ROWS` | number | `24` | Default terminal height (rows) for the Process Manager terminal view. |
| `CLEANUP_TTL_HOURS` | number | `24` | Hours before expired process data (logs, registry entries) is automatically cleaned up. |
| `RTK_ENABLED` | boolean | `false` | Route shell commands through RTK (Rust Token Killer) to filter/compress output and save LLM context tokens. Requires the `rtk` binary on PATH. |
| `RTK_BINARY_PATH` | string | `rtk` | Path to the `rtk` binary used when `RTK_ENABLED` is true. Set an absolute path when the spawned shell does not inherit your PATH. |

## Tool Arguments

`exec_shell` also has one **Tool Argument**:

- `os` — enum `Windows`, `Linux`, `Darwin`, `Auto-Detect` (default `Auto-Detect`).
  The operating system to target for shell selection.

## Reading output (`exec_shell_output`)

Reading a long-running process returns a *batch* of output, not a single burst.
The call blocks until output arrives, then keeps collecting until the process
goes quiet for `OUTPUT_QUIET_WINDOW` seconds, exits, hits an interactive prompt,
fills a batch, or runs out of time. This keeps a chatty installer from costing
one reasoning step per line of output.

Three optional per-call parameters override that behavior:

- `wait_until` — a regular expression. Keep collecting until the output matches
  it instead of returning at the next pause, so one call can ride through long
  silent phases. Use it when you know the finish line, e.g.
  `wait_until: "Installation complete"`. Process exit, an interactive prompt, a
  full batch, or `max_wait` still end the call, and the result reports
  `wait_until_matched` so you can tell "found it" from "gave up waiting".
  Matching ignores ANSI colour codes.
- `quiet_window` — seconds of silence before the batch is returned, overriding
  `OUTPUT_QUIET_WINDOW` for this call. Raise it to batch more aggressively; `0`
  returns as soon as the first burst arrives. Ignored while `wait_until` is set.
- `max_wait` — seconds this call may block in total, overriding
  `OUTPUT_WAIT_TIMEOUT`. Still clamped by the server's tool-call timeout.

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
