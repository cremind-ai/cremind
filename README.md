# Cremind

Open personal assistant — server, desktop app, and CLI you run yourself.

[![PyPI](https://img.shields.io/pypi/v/cremind.svg)](https://pypi.org/project/cremind/)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.13.9-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](#license)
[![CI](https://github.com/cremind-ai/cremind/actions/workflows/pr.yml/badge.svg)](https://github.com/cremind-ai/cremind/actions/workflows/pr.yml)

## What is Cremind?

Cremind is a personal AI assistant that runs on your own machine, server, or
Kubernetes cluster. It bundles a multi-profile agent runtime, tools and skills,
automations, a web UI, a desktop app, and a CLI into one package. Profiles
isolate personas, LLM keys, skills, memory, and conversations, so the same
install can host a work assistant, a coding assistant, and a home assistant side
by side without bleeding context across them.

Bring your own LLM — 21 providers, from Anthropic, OpenAI, and Google to local
models through Ollama or vLLM. Plug in tools from any **MCP** server, add skills,
and reach the assistant through whichever surface fits: browser, desktop app,
terminal, a chat app such as Telegram, WhatsApp, Slack, or Discord, or an
e-paper display on your desk. Cremind is itself an **A2A** agent. It serves an
agent card at `/.well-known/agent-card.json` and accepts A2A requests signed
with a profile token. The web UI talks to it the same way.

## Features

### Profiles

- Each profile has its own persona, standing instructions, and agent name; its
  own LLM providers, keys, and model roles; its own tools, skills, MCP servers,
  long-term memory, chat channels, browser profile, and timezone; and a private
  working directory for its files.
- The `admin` profile, created by the Setup Wizard, runs the server: it manages
  the other profiles, optional features, vector embedding, backups, HTTPS, and
  Tag hardware.
- Sign-in uses a per-profile token rather than a password. `cremind auth`
  rotates or revokes it.

### Models

- **21 LLM providers:** Anthropic, OpenAI, Groq, Google Gemini, Google Vertex
  AI, GitHub Copilot, xAI (Grok), Mistral AI, Moonshot AI (Kimi), Qwen Cloud,
  MiniMax, NVIDIA, Perplexity, Together AI, Fireworks AI, Hugging Face, Chutes,
  Cloudflare AI Gateway, LiteLLM, Ollama, and vLLM. You can also add any
  custom OpenAI-compatible endpoint.
- **Subscription sign-in instead of an API key:** Sign in with ChatGPT
  (Plus / Pro / Team) for OpenAI models, GitHub Copilot device login, or an
  Anthropic setup token.
- **Model roles:** a main model, plus an optional plan model, a cheaper
  low-performance model for background checks, and dedicated vision and audio
  models. Each role has its own reasoning effort.
- Provider SDKs are optional features, installed on demand the first time you
  configure that provider.

### Chat

- **Three modes per message:**
  - **Plan** researches, asks clarifying questions, and writes a plan for you
    to accept. It then executes the plan against a live to-do list.
  - **Reasoning** is the default.
  - **Instant** skips extended thinking and allows at most one round of tool
    calls.
- **Thinking Process** shows a timeline of every tool call and its result. The
  **Agent Activity** panel streams the work of delegated coding agents live.
- **Mid-turn messages:** you can send a follow-up while the agent is working,
  and it folds the message into the running turn.
- **Context and memory:**
  - A meter shows how full the context window is, and Cremind suggests
    compacting a long conversation.
  - Compaction also saves durable facts to long-term memory, which the agent
    can recall in later conversations.
  - Prompt caching is on by default.
- **Usage & Cost:** tokens and estimated cost by model, provider, and tool, over
  7, 30, or 90 days or all time.

### Tools

- **Shell:** PowerShell or bash, with interactive input and long-running
  processes. You can attach to those processes from the Processes page or with
  `cremind proc`.
- **Files:** a file tool scoped to the profile's working directory. It can also
  run the agent when files change.
- **Browser:** Playwright drives a real Chrome window with its own profile per
  Cremind profile. With the Docker desktop image you can watch it work over
  noVNC.
- **Web:** web search (Parallel.ai by default with no key needed, or
  DuckDuckGo) and web fetch.
- **Images and audio:** image and audio understanding, using the vision and
  audio model roles.
- **Off by default, and need API keys:** weather (AccuWeather) and Google
  Places.
- **Claude Code and Codex:** hand a coding task to Anthropic's or OpenAI's
  coding agent. These are optional features, with sign-in per profile.
- **Search sources:** each conversation chooses which sources the agent may
  use: your own documents, Cremind's manual, long-term memory, and the web.
- **MCP servers:** add any MCP server (stdio or HTTP, by URL or VS Code-style
  JSON, with OAuth if the server needs it), and enable or disable its tools per
  profile.

### Skills and blueprints

- **What a skill is:** a folder with a `SKILL.md`, plus optional scripts,
  references, and event listeners.
- **Getting skills:** import them from GitHub, from an archive, or from
  [Cremind Hub](https://hub.cremind.io). Changes reload automatically.
- **Built-in skills.** All of them start disabled.
  - Google: Gmail (send only), Google Calendar, Drive, Docs, and Sheets.
  - Email: IMAP/SMTP email (read, search, send, and new-mail events).
  - Calendars: CalDAV (iCloud, Fastmail, Nextcloud, …).
  - Smart home: Home Assistant.
  - Atlassian: Jira and Confluence.
  - Other: `claude-usage-monitor` (tracks Claude subscription limits across
    accounts) and `skill-creator`.
- **Blueprints** package a profile's design as a `.cremind-blueprint` file:
  persona, tools, LLM choice, settings, skills, and events. Blueprints never
  include secrets or chats. You can share them or publish them on Cremind Hub,
  and a step-by-step wizard imports them.

### Your documents

- **What it indexes:** documentation search indexes a profile's working
  directory, and optionally selected Google Drive files. It keeps the index in
  sync as files change, and `.cremindignore` files exclude what you don't want
  indexed.
- **Turning it on takes three steps:** the admin enables it, Vector Embedding
  must be on, and then each profile opts in. If embedding is later turned off,
  the index stays searchable by keyword.
- **Formats:**
  - PDF, with OCR for scanned pages.
  - Word, Excel, and PowerPoint, including legacy `.doc`/`.ppt`.
  - OpenDocument, RTF, EPUB, and HTML.
  - Email files (EML, MSG, MHT).
  - CSV, source code, and plain text.
  - Photos, indexed by their EXIF metadata and optional vision captions.
- **Answers and research:** answers cite their sources with `[doc:…]`
  citations. Research jobs can analyze or compile a whole folder.

### Automations

- **Calendar & Schedule:** one-off and recurring (RFC 5545) events that run an
  agent action and report back. It syncs two ways with Google Calendar and uses
  each profile's timezone.
- **Triggers:**
  - File watchers run the agent when files are created, changed, moved, or
    deleted.
  - Skill events are raised by skill listeners, such as new mail, calendar
    changes, Home Assistant states, or Jira issues.
- **Run history:** every run records its status, tokens, and reply, on the
  Events page or with `cremind event-runs`. An unattended run can pause to ask
  you a question.

### Chat channels and notifications

| Platform | Modes |
|---|---|
| Telegram | Bot (BotFather token), or your own account (login code) |
| WhatsApp | Your own account, linked by QR scan |
| Zalo | Official bot, or a personal account linked by QR scan |
| Discord | Bot |
| Slack | Bot (Socket Mode, no public URL needed) |
| Messenger | Page bot (needs a public HTTPS URL) |

- **Notification mode:** every platform also has a notification mode that
  pushes the agent's alerts. You can filter them by priority, source, and
  keyword, and set quiet hours.
- **Access control:** each channel admits people in one of five ways: open,
  passcode, one-time code, approval, or allowlist.
- **Group chats:** the agent can join approved group chats on every platform
  except Messenger. It replies when it is mentioned or when the message is
  relevant to it.
- **Outgoing messages and files:** the agent can send messages and files to
  contacts on connected channels. It always asks for confirmation before
  messaging someone for the first time.

### Group chat

You can put several profiles' agents in one room with you. Each agent answers
from its own persona, tools, and memory, and stays silent when it isn't
addressed. Hop and rate limits keep the agents from talking in circles.

### Google Workspace

- **Linking:** link Gmail, Calendar, Drive, Docs, and Sheets with one-click
  OAuth, with no Google Cloud project of your own.
- **Cremind Connect:** the OAuth client and a relay for push webhooks come from
  Cremind Connect (`connect.cremind.io`), which also relays Jira webhooks.
  Tokens stay on your machine, and you can bring your own Google client
  instead.
- **Unlinking:** Settings → GSuite or `cremind google unlink` unlinks an account
  and revokes the token.

### Cremind Tags

Cremind Tags are battery-powered e-paper displays, in black/white or
black/white/red.

- **What they show:** the agent's questions, task outcomes, notifications,
  calendar, and pinned notes.
- **Hardware:** a USB nRF52840 gateway talks to the tags over Bluetooth LE, and
  optional Bluetooth Mesh bridges extend the range.
- **Where the gateway plugs in:** into the server itself, or into a desktop-app
  machine enrolled for a remote server.
- **Platforms:** Windows x64, Linux x64/arm64, and macOS 15+.

See [`docs/tags/`](docs/tags/).

### Workspace UI

- **Pages:** chat; a file tree of the working directory (upload, rename, move);
  in-browser terminals; and the Processes, Calendar & Schedule, Events,
  Channels, Usage, and Tags pages.
- **Appearance:** nine themes, plus Match system and custom colors; seven font
  presets, or your own font; and adjustable text size.

### Storage

- **Database:** SQLite by default. You can pick PostgreSQL instead, but only in
  the Setup Wizard at first setup.
- **Vector Embedding** (off by default):
  - Runs a local `multilingual-e5-base` or `EmbeddingGemma-300m` model.
  - Stores vectors in ChromaDB (in-process by default) or Qdrant.
  - Powers semantic document search and memory recall.
- **Where services run** depends on the install mode: in-process, as Docker
  sidecars, or as external endpoints.

### Security and operations

- **HTTPS:** opt-in. Cremind can mint a local CA and certificate, help you trust
  the CA, and then serve HTTP/2. Set it up in Settings → HTTPS & Certificate or
  with `cremind tls`.
- **Backup and restore:** full-system backups, optionally encrypted with a
  passphrase (AES-256-GCM). The Setup Wizard can restore from a backup.
- **Updates:**
  - Settings → Updates checks for a release, backs up, installs, migrates, and
    rolls back automatically if the health check fails. On a native install,
    `cremind upgrade` does the same from the terminal.
  - There are production and test (release candidate) channels.
  - The desktop app checks for its own updates.
- **Start at login:** `cremind boot enable` registers a systemd user unit, a
  macOS LaunchAgent, or a Windows Scheduled Task, which also restarts Cremind
  if it stops.
- **Reset:** `cremind clean` wipes one profile's data or factory-resets the
  install.
- **Built-in manual:** the agent searches Cremind's own documentation, so it can
  answer "how do I…" questions and run `cremind` commands for you.

## Install

### Cremind App (desktop)

Download the installer for your OS from the
[Releases page](https://github.com/cremind-ai/cremind/releases):

| OS | File |
|---|---|
| Windows (x64) | `Cremind-App-Windows-<version>-Setup.exe` |
| macOS (Apple silicon) | `Cremind-App-Mac-<version>-arm64-Installer.dmg` |
| macOS (Intel) | `Cremind-App-Mac-<version>-x64-Installer.dmg` |
| Linux (x64) | `Cremind-App-Linux-<version>.AppImage` |

On first launch, the app runs the same installer as below, in Docker or native
mode, to set up the Cremind server on this machine. It then takes you to the
Setup Wizard. The app checks GitHub Releases for new versions of itself.

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

The installer offers only the modes this machine can run:

| Mode | What you get |
|---|---|
| **Docker** *(recommended)* | The agent runs in a sandboxed container. The default **desktop** image (`cremind/cremind-desktop`) includes an XFCE desktop at `http://<host>:6080/vnc.html`, where you can watch the agent's browser. `--no-desktop` pulls the smaller headless image (`cremind/cremind`) instead. Postgres, Qdrant, and ChromaDB run as sibling containers when you choose them. |
| **Native** | A Python venv at `~/.cremind/venv` with SQLite. The agent shares your desktop. If you don't have Python 3.13.9+, the installer offers to fetch a private Python 3.13. |
| **Kubernetes** | Installs the [Helm chart](helm/cremind/README.md), with bundled PostgreSQL, into a kubeconfig context you pick. It then opens a port-forward to the Setup Wizard. |

- **HTTP by default:** the installer asks whether to enable HTTPS. You can also
  turn it on later in Settings → HTTPS & Certificate.
- **Re-running:** every mode is idempotent, so re-running the installer
  upgrades in place.
- **Uninstalling:** `--uninstall` removes Cremind. `--keep` keeps your data,
  and `--purge` deletes it except the profiles' working directories.

See [`install/README.md`](install/README.md) for the full flag reference,
deployment types (`local` / `server` / `custom`), and file layout.

### Helm

```bash
helm install cremind oci://registry-1.docker.io/cremind/cremind \
  --version <X.Y.Z> --namespace cremind --create-namespace
```

See [`helm/cremind/README.md`](helm/cremind/README.md) for values,
dependencies, and upgrades.

### From PyPI

```bash
pip install cremind
```

This installs the `cremind` CLI and the server (`cremind serve`). Optional
features — LLM SDKs, embeddings, browser automation, channels, and so on —
install on demand from the app or with `cremind features install`. The
installers above are the recommended route, because they also handle Docker
isolation, start-at-login, and upgrades.

## First run

1. Wait for the installer to finish. It pulls images or builds a venv, runs
   migrations, and starts the server.
2. Your browser opens to **`http://<host>:1515/#/setup`**, the Setup Wizard.
   The address uses `https://` if you enabled HTTPS.
3. The wizard walks through these steps: **Server** (database), **Vector
   Embedding**, **LLM Providers**, **Tools**, **Memory**, and **Channels**.
   It finishes by creating the `admin` profile and showing its sign-in token.
   You can also restore from a backup instead.
4. Start chatting. To add more profiles, use Settings → Profiles,
   `cremind profile create`, or the guided `cremind profile wizard`.

Port **1515** serves the UI, the API, and A2A. The Docker desktop image adds
noVNC on **6080**.

Prefer the terminal? `cremind chat` gives you a streamed chat REPL with
Plan / Reasoning / Instant modes against the same profile. On the server
machine the CLI picks up the profile's token automatically. From elsewhere,
set `CREMIND_SERVER` and `CREMIND_TOKEN`.

## CLI

Every command supports `--help`. The command groups:

| Area | Commands |
|---|---|
| Chat | `chat`, `conv`, `group` |
| Profiles & access | `profile`, `me`, `auth`, `config` |
| Models, tools & skills | `llm`, `tools`, `agents` (MCP servers), `skills`, `blueprint`, `features` |
| Files & documents | `files`, `docs`, `drive`, `embedding` |
| Automations | `calendar`, `file-watchers`, `skill-events`, `event-runs`, `proc` |
| Channels & devices | `channels`, `google`, `tags` |
| Server & operations | `serve`, `server`, `setup`, `boot`, `tls`, `backup`, `upgrade`, `db`, `logs`, `clean`, `usage`, `system-vars`, `version` |

## Requirements

- **Docker mode:** Docker Engine with Compose v2.
- **Native mode:** nothing to preinstall. Linux, macOS, and Windows are all
  supported, and the installer provides Python 3.13 if you don't have
  3.13.9+.
- **Kubernetes mode:** `kubectl` with a kubeconfig context, and `helm` 3.8+.
- **Optional:** Node.js 20+, for the WhatsApp and Zalo personal-account
  channels.
- **Platform notes:**
  - Vector Embedding isn't available natively on Intel Macs or Windows on ARM;
    use Docker mode there.
  - Cremind Tags need macOS 15+ on a Mac.

## Documentation

- [`install/README.md`](install/README.md): installer reference, flags,
  deployment types, and file layout.
- [`helm/cremind/README.md`](helm/cremind/README.md): the Kubernetes Helm chart.
- [`docs/tags/`](docs/tags/): Cremind Tag hardware and protocol.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): development setup and contribution
  workflow.
- [`RELEASING.md`](RELEASING.md): release channels (production / test / dev)
  and the release process.
- **In-app manual:**
  [`app/cremind_documents/bundled/`](app/cremind_documents/bundled/) has one
  page per CLI command and built-in tool. It is synced on every boot, and the
  agent searches it to answer questions about Cremind.

## License

MIT — see [`LICENSE`](LICENSE).
