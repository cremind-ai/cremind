# Cremind

Open personal assistant — server, desktop app, and CLI you run yourself.

[![PyPI](https://img.shields.io/pypi/v/cremind.svg)](https://pypi.org/project/cremind/)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.13-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](#license)
[![CI](https://github.com/cremind-ai/cremind/actions/workflows/pr.yml/badge.svg)](https://github.com/cremind-ai/cremind/actions/workflows/pr.yml)

## What is Cremind?

Cremind is a personal AI assistant that runs on your own machine or server. It
bundles a multi-profile agent runtime, a tool plane, a web UI, and a CLI into
one package. Profiles isolate personas, LLM keys, skills, and conversations,
so the same install can host a work assistant, a coding assistant, and a
home assistant side by side without bleeding context across them.

Under the hood, Cremind speaks two open protocols: **A2A** for pluggable agents
and **MCP** for pluggable tools. Bring your own LLM (Anthropic, OpenAI, Groq),
plug in any MCP server, and reach the assistant through whichever surface
fits — browser, desktop app, terminal, or a chat channel like Telegram.

## Features

- **Multi-profile agents** with isolated personas, skills, browser profiles, and conversation history.
- **Pluggable LLMs** — Anthropic, OpenAI, and Groq, installed on demand via feature flags.
- **Tool plane** — built-in file browser, terminal, document RAG, and Playwright browser automation; add any MCP server.
- **Document ingestion** — drop files in a watched folder; Cremind indexes them for retrieval.
- **Storage that scales with you** — SQLite by default; switch any service to Postgres, Qdrant, or ChromaDB as a Docker sidecar or external endpoint, from the Setup Wizard.
- **Chat channels** — Telegram bot and userbot adapters included; channel API for adding more.
- **Three surfaces** — Web UI at `:1515`, the **Cremind App** desktop client (Windows / macOS / Linux), and an `cremind` CLI for chat, conversations, tools, and admin.
- **Cross-platform** — runs on Linux, macOS, and Windows; sandboxed Docker mode available for stronger isolation.

## Install

### Cremind App (desktop)

Download the prebuilt installer for your OS from the
[Releases page](https://github.com/cremind-ai/cremind/releases):

| OS      | File |
|---------|------|
| Windows | `Cremind App-Windows-<version>-Setup.exe` (NSIS) |
| macOS   | `Cremind App-macOS-<version>.dmg`               |
| Linux   | `Cremind App-Linux-<version>.AppImage`          |

The desktop app auto-updates from GitHub Releases and bundles the agent — no
separate server install needed.

### One-line install (Linux / macOS)

```bash
curl -fsSL https://cremind.io/install.sh | bash
```

Or fetch the same script straight from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/cremind-ai/cremind/main/install/install.sh | bash
```

### One-line install (Windows)

```powershell
iwr -useb https://cremind.io/install.ps1 | iex
```

Or fetch the same script straight from GitHub:

```powershell
iwr -useb https://raw.githubusercontent.com/cremind-ai/cremind/main/install/install.ps1 | iex
```

The installer detects Docker and asks which mode to use:

- **Docker** *(recommended)* — agent runs in a sandboxed XFCE container with
  VNC at `:6080`. Postgres / Qdrant / ChromaDB activate as sibling
  containers on demand.
- **Native** — Python 3.13+ venv at `~/.cremind/venv`, SQLite, agent shares
  your desktop.

Both modes are idempotent — re-running upgrades in place. See
[`install/README.md`](install/README.md) for the full flag reference and
deployment-type options (`local` / `server` / `custom`).

## First run

1. Wait for the installer to finish (it pulls images or builds a venv, runs migrations, and starts the server).
2. Your browser opens automatically to **`http://<host>:1515/#/setup`** — the Setup Wizard.
3. Pick an LLM provider, paste an API key, create your first profile, and you're chatting.

Prefer the terminal? `cremind chat` gives you a streamed REPL against the same
profile. `cremind --help` lists every command (profiles, conversations, tools,
agents, channels, LLM config, …).

## Requirements

- **Docker mode**: Docker Engine with Compose v2. Any OS.
- **Native mode**: Python **3.13.9+** on Linux, macOS, or Windows.

## Documentation

- [`install/README.md`](install/README.md) — installer reference, flags, deployment types, and file layout.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup and contribution workflow.
- [`RELEASING.md`](RELEASING.md) — release channels (production / test / dev) and the release process.
- In-app docs live under [`documents/`](documents/) and are auto-indexed for retrieval.

## License

MIT.
