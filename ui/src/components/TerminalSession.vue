<script setup lang="ts">
import { onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue';
import { ElMessage, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { useSettingsStore } from '../stores/settings';
import { getToolConfig } from '../services/configApi';
import {
  openProcessSocket,
  stopProcess,
  type ProcessStatus,
} from '../services/processApi';

const props = defineProps<{
  pid: string;
  showHeader?: boolean;
}>();

const settings = useSettingsStore();

const termEl = ref<HTMLDivElement | null>(null);
const command = ref<string>('');
const workingDir = ref<string>('');
const status = ref<ProcessStatus | 'connecting' | 'disconnected'>('connecting');
const exitCode = ref<number | null>(null);
const isPty = ref<boolean>(false);

let term: Terminal | null = null;
let fitAddon: FitAddon | null = null;
let ws: WebSocket | null = null;
let resizeObserver: ResizeObserver | null = null;
// Set when WE close the socket (unmount / pid change) so onclose can stay
// quiet — only a close we didn't initiate is worth alerting the user about.
let intentionalClose = false;

// Client-side cooked-mode line buffer for non-PTY processes (see ProcessTerminal.vue
// for the full rationale — the same logic applies here).
let inputBuffer = '';

async function loadDefaultSize(): Promise<{ cols: number; rows: number }> {
  try {
    const tool = await getToolConfig(settings.agentUrl, settings.authToken, 'exec_shell');
    const vars = tool.config?.variables || {};
    const cols = Number(vars.TERMINAL_DEFAULT_COLS) || 80;
    const rows = Number(vars.TERMINAL_DEFAULT_ROWS) || 24;
    return { cols, rows };
  } catch {
    return { cols: 80, rows: 24 };
  }
}

function handleNonPtyInput(data: string): string {
  if (!term) return '';
  const norm = data.replace(/\r\n?/g, '\n');
  let echo = '';
  let toSend = '';
  for (const ch of norm) {
    const code = ch.charCodeAt(0);
    if (ch === '\n') {
      toSend += inputBuffer + '\n';
      inputBuffer = '';
      echo += '\r\n';
    } else if (ch === '\x7f' || ch === '\b') {
      if (inputBuffer.length > 0) {
        const chars = [...inputBuffer];
        chars.pop();
        inputBuffer = chars.join('');
        echo += '\b \b';
      }
    } else if (code >= 0x20 && code !== 0x7f) {
      inputBuffer += ch;
      echo += ch;
    }
  }
  if (echo) term.write(echo);
  return toSend;
}

function writeChunk(stream: string, data: string) {
  if (!term) return;
  if (stream === 'stderr') {
    term.write(`\x1b[31m${data}\x1b[0m`);
  } else {
    term.write(data);
  }
}

function sendResize() {
  if (!ws || ws.readyState !== WebSocket.OPEN || !term) return;
  ws.send(JSON.stringify({
    type: 'resize',
    cols: term.cols,
    rows: term.rows,
  }));
}

async function waitForToken(maxMs = 5000): Promise<boolean> {
  if (settings.authToken) return true;
  const start = Date.now();
  while (!settings.authToken && Date.now() - start < maxMs) {
    await new Promise((r) => setTimeout(r, 50));
  }
  return Boolean(settings.authToken);
}

async function connect() {
  if (!termEl.value) return;

  const ok = await waitForToken();
  if (!ok) {
    status.value = 'disconnected';
    return;
  }

  const { cols, rows } = await loadDefaultSize();

  term = new Terminal({
    cols,
    rows,
    convertEol: false,
    cursorBlink: true,
    fontFamily: 'Consolas, Monaco, "Courier New", monospace',
    fontSize: 13,
    theme: { background: '#1e1e1e', foreground: '#d4d4d4' },
  });
  fitAddon = new FitAddon();
  term.loadAddon(fitAddon);
  term.open(termEl.value);
  try { fitAddon.fit(); } catch { /* ignore early-fit race */ }

  // Suppress xterm's automatic replies to host-sent queries — same rationale
  // as ProcessTerminal.vue. Without this, visible garbage like "[?1;2c" can
  // appear at the top of a shell session.
  term.parser.registerCsiHandler({ final: 'c' }, () => true);
  term.parser.registerCsiHandler({ final: 'n' }, () => true);

  term.onData((data: string) => {
    let sendData: string;
    if (isPty.value) {
      sendData = data;
    } else {
      sendData = handleNonPtyInput(data);
      if (!sendData) return;
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'stdin', data: sendData }));
    }
  });

  try {
    intentionalClose = false;
    ws = openProcessSocket(settings.agentUrl, settings.authToken, props.pid);
  } catch (err) {
    status.value = 'disconnected';
    ElMessage.error(
      'Failed to open process stream: ' +
      (err instanceof Error ? err.message : String(err)),
    );
    return;
  }
  ws.onopen = () => {
    status.value = 'running';
    sendResize();
  };
  ws.onmessage = (event) => {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    const t = msg.type as string;
    if (t === 'snapshot') {
      const chunks = (msg.chunks as Array<{ type: string; data: string }>) || [];
      for (const c of chunks) writeChunk(c.type, c.data);
    } else if (t === 'stdout' || t === 'stderr') {
      writeChunk(t, (msg.data as string) || '');
    } else if (t === 'status') {
      const data = (msg.data as Record<string, unknown>) || {};
      command.value = (data.command as string) || '';
      workingDir.value = (data.working_dir as string) || '';
      isPty.value = Boolean(data.is_pty);
      status.value = (data.status as ProcessStatus) || 'running';
      exitCode.value = (data.exit_code as number | null) ?? null;
    } else if (t === 'overflow') {
      ElMessage.warning('Output buffer overflowed — some chunks were dropped.');
    }
  };
  ws.onclose = (ev) => {
    // We closed it ourselves (unmount / pid change), or the process already
    // reported 'exited' (normal end-of-stream): stay quiet. Anything else is
    // a real disconnect the user should see — a silent close otherwise looks
    // identical to a terminal that's still "connecting".
    if (intentionalClose || status.value === 'exited') return;
    status.value = 'disconnected';
    // 1000 (normal) / 1005 (no status) accompany clean closes; only alert on
    // abnormal codes (1006 dropped, 1008 rejected, 1011 server error, …).
    if (ev.code !== 1000 && ev.code !== 1005) {
      ElMessage.error(
        `Terminal disconnected (code ${ev.code}${ev.reason ? `: ${ev.reason}` : ''}).`,
      );
    }
  };
  ws.onerror = () => {
    // onerror typically fires just before onclose; don't double-toast.
    if (intentionalClose || status.value === 'exited' || status.value === 'disconnected') return;
    status.value = 'disconnected';
    ElMessage.error('Terminal connection failed — is the backend reachable?');
  };

  resizeObserver = new ResizeObserver(() => {
    try {
      fitAddon?.fit();
      sendResize();
    } catch {
      /* fit() can throw if element isn't measurable yet */
    }
  });
  resizeObserver.observe(termEl.value);
}

function disconnect() {
  try { resizeObserver?.disconnect(); } catch { /* noop */ }
  resizeObserver = null;
  intentionalClose = true;
  try { ws?.close(); } catch { /* noop */ }
  ws = null;
  try { term?.dispose(); } catch { /* noop */ }
  term = null;
  fitAddon = null;
}

onMounted(connect);
onBeforeUnmount(disconnect);

// When wrapped in <keep-alive>, the component is deactivated (DOM detached)
// but onBeforeUnmount does NOT fire — so xterm and the WebSocket stay alive
// and keep buffering output. Re-fit when the panel is re-activated so the
// terminal matches the new container size.
onActivated(() => {
  try { fitAddon?.fit(); } catch { /* noop */ }
  sendResize();
});
onDeactivated(() => {
  // Intentionally empty: we want sockets + xterm to stay alive while hidden.
});

// If the parent passes a different pid (rare — usually the parent keys each
// TerminalSession by pid so this component unmounts), rebuild from scratch.
watch(
  () => props.pid,
  (newPid, oldPid) => {
    if (newPid !== oldPid) {
      disconnect();
      connect();
    }
  },
);


async function confirmStop() {
  await stopProcess(settings.agentUrl, settings.authToken, props.pid);
  status.value = 'exited';
}

defineExpose({ confirmStop, status, command, workingDir, isPty, exitCode });
</script>

<template>
  <div class="terminal-session">
    <div v-if="showHeader !== false" class="session-header">
      <ElTag :type="statusColor(status)" size="small" effect="light">
        {{ status }}{{ exitCode !== null ? ` (${exitCode})` : '' }}
      </ElTag>
      <span v-if="isPty" class="pty-pill">pty</span>
      <code class="cmd" :title="command">{{ command || '…' }}</code>
      <span class="cwd" :title="workingDir">
        <Icon icon="mdi:folder-outline" />
        <span class="cwd-text">{{ workingDir || '—' }}</span>
      </span>
    </div>
    <div ref="termEl" class="terminal-host" />
  </div>
</template>

<script lang="ts">
type TagType = 'success' | 'info' | 'warning' | 'danger' | 'primary';
function statusColor(s: string): TagType {
  if (s === 'running') return 'success';
  if (s === 'exited') return 'info';
  if (s === 'disconnected') return 'warning';
  return 'primary';
}
</script>

<style scoped>
.terminal-session {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: #111;
}
.session-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #0f172a;
  border-bottom: 1px solid #1f2937;
  color: #e5e7eb;
  flex-shrink: 0;
  overflow: hidden;
}
.pty-pill {
  background: #1e293b;
  color: #94a3b8;
  border-radius: 4px;
  padding: 0 6px;
  font-size: 0.7rem;
  flex-shrink: 0;
}
.cmd {
  font-family: Consolas, Monaco, monospace;
  font-size: 0.8rem;
  color: #cbd5f5;
  background: transparent;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1 1 auto;
  min-width: 0;
}
.cwd {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: Consolas, Monaco, monospace;
  font-size: 0.8rem;
  color: #94a3b8;
  white-space: nowrap;
  overflow: hidden;
  flex: 0 1 auto;
  min-width: 0;
}
.cwd-text {
  overflow: hidden;
  text-overflow: ellipsis;
}
.terminal-host {
  flex: 1;
  min-height: 0;
  padding: 8px;
  background: #1e1e1e;
}
</style>
