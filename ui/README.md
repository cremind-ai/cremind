# Cremind Client UI

Cremind Client UI is a modern chat interface for interacting with AI agents using the Agent-to-Agent (A2A) protocol. It provides both desktop and web application modes for seamless agent communication.

## Features

- **Multi-Agent Support**: Connect to and chat with multiple AI agents
- **Real-time Streaming**: Support for Server-Sent Events (SSE) for real-time responses
- **Dual Mode**: Run as either an Electron desktop app or a web application
- **Rich UI**: Built with Vue 3 and Element Plus for a polished user experience
- **Markdown Support**: Render formatted responses with syntax highlighting
- **Persistent Chat**: Save and restore conversation history. Each conversation row in the sidebar has a pencil icon that opens an edit dialog for both the **id** and the **title** — renaming the id (format: lowercase `a-z`, digits, `-`, `_`, starting with an alphanumeric character) cascades atomically to all messages and skill-event subscriptions; if the title field isn't also edited, it is reset to match the new id.
- **Configurable Settings**: Customize agent URL, model, temperature, and more
- **Cross-Platform**: Works on Windows, macOS, and Linux

## Tech Stack

- **Frontend**: Vue 3, TypeScript, Vite, Element Plus, Pinia
- **Desktop**: Electron with system tray support
- **Agent SDK**: @a2a-js/sdk for A2A protocol implementation
- **Markdown**: Marked with syntax highlighting

## Project Structure

```
├── electron/           # Electron main process and preload scripts
├── src/               # Vue 3 frontend source code
│   ├── api/          # API utilities
│   ├── components/   # Vue components (ChatWindow, MessageBubble, etc.)
│   ├── router/       # Vue Router configuration
│   ├── services/     # A2A client service
│   ├── stores/       # Pinia stores (chat, settings)
│   ├── types/        # TypeScript type definitions
│   └── views/        # Vue views (ChatView, SettingsView)
├── dist-electron/    # Compiled Electron code
├── dist-web/         # Compiled web assets
└── release/          # Packaged desktop installers
```

## Getting Started

### Prerequisites

- **Node.js**: v18+ recommended
- **npm** or **yarn**: Package manager

### Installation

1. **Clone the repository:**
   ```bash
   git clone <your-repository-url>
   cd cremind-client-ui
   ```

2. **Install dependencies:**
   ```bash
   npm install
   # or
   yarn install
   ```

3. **Configure environment:**
   Copy `.env_example` to `.env` and adjust settings if needed:
   ```bash
   cp .env_example .env
   ```

### Development

#### Desktop (Electron) Mode

Run the Electron app with hot-reload:

```bash
npm run dev
# or
yarn dev
```

This starts the Vite dev server and launches the Electron window.

#### Web Mode

Run the web application with development server:

```bash
npm run web:dev
# or
yarn web:dev
```

Access the app at `http://localhost:1515` (or the port shown in console).

### Building

#### Build Desktop App

Build the Electron application (creates installer):

```bash
npm run build
# or
yarn build
```

The installer will be in the `release/` folder.

#### Web Development

For web application development with hot-reload:

```bash
npm run web:dev
# or
yarn web:dev
```

This starts the Vite dev server at `http://localhost:1515` with proxy support for `/a2a` requests.

## Configuration

### Environment Variables

- `PORT`: Port for web server (default: 1515 for web mode, 0/random for Electron mode)
- `VITE_AGENT_URL`: Default A2A agent URL, injected at build time as `__AGENT_URL__`. Used when the user has not stored a custom URL in localStorage (`agent_url`). (default: http://localhost:1112)

### Agent Configuration

In the app settings, you can configure:

- **Agent URL**: The endpoint of your A2A agent
- **Auto-connect**: Automatically connect to agent on startup
- **Theme**: Light or dark mode

## Usage

1. Launch the application (desktop or web)
2. Configure your agent URL in settings (default: `http://localhost:1112`)
3. Connect to the agent to view agent card information
4. Start chatting with the agent
5. View real-time thinking process, token usage, and latency metrics
6. Use the settings panel to customize agent URL, theme, and auto-connect behavior

## Development Tips

- The app detects whether it's running in Electron or web mode automatically
- In web dev mode, `/a2a` requests are proxied to the agent URL
- Chat history is stored in localStorage
- The SDK client is initialized dynamically based on the runtime mode

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

[MIT](LICENSE)

