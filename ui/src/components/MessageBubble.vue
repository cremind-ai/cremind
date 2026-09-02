<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import type { ChatMessage, FileAttachment, TerminalAttachment } from '../stores/chat';
import { OpenTerminalKey } from '../composables/terminalTarget';
import { createChatMarked } from '../utils/markdown';
import vLinkBlank from '../directives/v-link-blank';
import { Icon } from '@iconify/vue';
import { copyTextToClipboard } from '../utils/clipboard';
import { chatModeMeta } from '../constants/chatModes';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import { getProcess } from '../services/processApi';
import { ElMessage } from 'element-plus';
import MessageUsageChip from './MessageUsageChip.vue';
import ThinkingProcess from './ThinkingProcess.vue';
import TodoChip from './plan/TodoChip.vue';

const props = defineProps<{
  message: ChatMessage;
  // When embedded (e.g. the event-run drawer), the id of the conversation this
  // bubble belongs to — forwarded to MessageUsageChip so it resolves usage for
  // the right conversation instead of the globally-active one.
  conversationId?: string | null;
}>();

const settingsStore = useSettingsStore();

// Resolve file paths and /api/ URLs to the backend origin
const resolveApiUrl = (href: string): string => {
  if (!href) return href;
  const base = settingsStore.agentUrl.replace(/\/$/, '');
  if (href.startsWith('/api/')) {
    return base + href;
  }
  // Absolute filesystem path: use the /api/files/open endpoint
  if (!href.startsWith('http://') && !href.startsWith('https://')) {
    return `${base}/api/files/open?path=${encodeURIComponent(href)}`;
  }
  return href;
};

// Configure marked with syntax highlighting + URL rewriting (shared factory).
const marked = createChatMarked(resolveApiUrl);

const route = useRoute();
const router = useRouter();

const isUser = computed(() => props.message.role === 'user');
const isRejectedTrigger = computed(() => props.message.isRejectedTrigger === true);
// An event run reporting back into the conversation that registered its rule.
// Only the trigger bubble carries the flag — the agent's own answer to it is an
// ordinary reply and must not be labelled as machine output.
const isEventResult = computed(() => props.message.isEventResult === true);
const eventResultTitle = computed(() => {
  const label = props.message.eventResultLabel;
  return label ? `Automation result — ${label}` : 'Automation result';
});
const eventResultNote = computed(() =>
  props.message.eventResultOnce
    ? 'One-shot task: reported once, then ended.'
    : 'Reported by a standing rule; it stays active and will report again.',
);
// The Events page hosts the run drawer and deep-links a run via ?run=<id>.
const eventRunProfile = computed(() => {
  const profile = route.params.profile;
  return typeof profile === 'string' && profile ? profile : null;
});
function openEventRun() {
  const profile = eventRunProfile.value;
  const runId = props.message.eventRunId;
  if (!profile || !runId) return;
  router.push({ name: 'skill-events', params: { profile }, query: { run: runId } });
}
const modeMeta = computed(() =>
  props.message.mode ? chatModeMeta(props.message.mode) : null,
);

const copied = ref(false);
const copyToClipboard = async () => {
  if (!props.message.content) return;
  if (!(await copyTextToClipboard(props.message.content))) return;
  copied.value = true;
  setTimeout(() => { copied.value = false; }, 2000);
};
const parsedContent = computed(() => {
  if (!props.message.content) return '';
  return marked.parse(props.message.content) as string;
});

// Format latency information for display
const latencyDisplay = computed(() => {
  if (!props.message.latency || !props.message.latency.requestSentAt) return null;
  
  const latency = props.message.latency;
  const parts: string[] = [];
  
  if (latency.firstEventAt) {
    const ms = latency.firstEventAt - latency.requestSentAt;
    parts.push(`First event: ${formatLatencyMs(ms)}`);
  }
  
  if (latency.firstStepAt) {
    const ms = latency.firstStepAt - latency.requestSentAt;
    parts.push(`First step: ${formatLatencyMs(ms)}`);
  }
  
  if (latency.firstTokenAt) {
    const ms = latency.firstTokenAt - latency.requestSentAt;
    parts.push(`First token: ${formatLatencyMs(ms)}`);
  }
  
  if (latency.completedAt) {
    const ms = latency.completedAt - latency.requestSentAt;
    parts.push(`Total: ${formatLatencyMs(ms)}`);
  }
  
  return parts.length > 0 ? parts.join(' | ') : null;
});

// Format milliseconds to human-readable string
const formatLatencyMs = (ms: number): string => {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
};

// Build full URL for a file URI (absolute path or legacy /api/files/ path)
const resolveFileUrl = (uri: string): string => {
  if (!uri) return '';
  if (uri.startsWith('http://') || uri.startsWith('https://')) return uri;
  const base = settingsStore.agentUrl.replace(/\/$/, '');
  // Legacy format: already a relative API path
  if (uri.startsWith('/api/')) {
    return `${base}${uri}`;
  }
  // Absolute filesystem path: use the /api/files/open endpoint
  return `${base}/api/files/open?path=${encodeURIComponent(uri)}`;
};

// Authorization header for the active profile (browser tab navigation can't carry it,
// so we fetch the file ourselves and hand the result to the new tab via a blob URL)
const authHeaders = (): Record<string, string> => {
  const token = settingsStore.authToken;
  return token ? { Authorization: `Bearer ${token}` } : {};
};

const openFileInNewTab = async (uri: string) => {
  // Open the blank tab synchronously so popup blockers stay happy
  const tab = window.open('', '_blank');
  if (!tab) return;
  try {
    const resp = await fetch(resolveFileUrl(uri), { headers: authHeaders() });
    if (!resp.ok) {
      tab.close();
      if (resp.status === 404) ElMessage.warning('This file is no longer available.');
      return;
    }
    const blob = await resp.blob();
    const blobUrl = URL.createObjectURL(blob);
    tab.location.href = blobUrl;
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
  } catch {
    tab.close();
  }
};

const downloadFile = async (uri: string, name: string) => {
  try {
    const resp = await fetch(resolveFileUrl(uri), { headers: authHeaders() });
    if (!resp.ok) {
      if (resp.status === 404) ElMessage.warning('This file is no longer available.');
      return;
    }
    const blob = await resp.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(blobUrl);
  } catch {
    /* silent */
  }
};

// Determine if a MIME type is an image
const isImageMime = (mime: string): boolean => {
  return !!mime && mime.startsWith('image/');
};

// Determine if a MIME type is a PDF
const isPdfMime = (mime: string): boolean => {
  return mime === 'application/pdf';
};

// Image-thumbnail URIs that failed to load (deleted/moved files) — render the
// generic file icon instead of a broken <img>.
const failedThumbs = ref<Set<string>>(new Set());

// Copy file text content to clipboard
const fileCopied = ref<string | null>(null);
const copyFileContent = async (url: string, fileName: string) => {
  try {
    const resp = await fetch(url, { headers: authHeaders() });
    if (!resp.ok) {
      if (resp.status === 404) ElMessage.warning('This file is no longer available.');
      return;
    }
    const text = await resp.text();
    if (!(await copyTextToClipboard(text))) return;
    fileCopied.value = fileName;
    setTimeout(() => { fileCopied.value = null; }, 2000);
  } catch {
    // silent fail
  }
};

// Determine file icon based on MIME
const getFileIcon = (mime: string): string => {
  if (!mime) return 'mdi:file-outline';
  if (mime.startsWith('image/')) return 'mdi:file-image-outline';
  if (mime === 'application/pdf') return 'mdi:file-pdf-box';
  if (mime.startsWith('video/')) return 'mdi:file-video-outline';
  if (mime.startsWith('audio/')) return 'mdi:file-music-outline';
  if (mime.startsWith('text/') || mime.includes('json') || mime.includes('xml') || mime.includes('javascript'))
    return 'mdi:file-code-outline';
  if (mime.includes('spreadsheet') || mime.includes('excel') || mime === 'text/csv')
    return 'mdi:file-table-outline';
  if (mime.includes('word') || mime.includes('document'))
    return 'mdi:file-word-outline';
  if (mime.includes('presentation') || mime.includes('powerpoint'))
    return 'mdi:file-powerpoint-outline';
  return 'mdi:file-outline';
};

// File attachments from the message (populated during streaming or from persisted data)
const fileAttachments = computed<FileAttachment[]>(() => {
  return props.message.fileAttachments || [];
});

// Terminal attachments (long-running processes started by exec_shell)
const terminalAttachments = computed<TerminalAttachment[]>(() => {
  return props.message.terminalAttachments || [];
});

const hasCarouselContent = computed(
  () => fileAttachments.value.length > 0 || terminalAttachments.value.length > 0,
);

// The per-turn plan/todo chip lives in the carousel (always first). Shown on a
// completed agent turn that drove a todo list.
const showTodoChip = computed(
  () => !isUser.value && !props.message.isStreaming && !!props.message.planTodos?.length,
);

const terminalPanel = useTerminalPanelStore();
// A surrounding surface (e.g. the event-run drawer) can provide its own
// terminal-open handler; otherwise fall back to the global Workspace panel.
const injectedOpenTerminal = inject(OpenTerminalKey, null);

const openTerminal = async (term: TerminalAttachment) => {
  // Terminal processes live only in an in-memory registry (they die on server
  // restart / TTL), so a persisted chip often points at a process that's gone.
  // Probe before opening and surface "not found" instead of a dead panel. Skip
  // the probe while streaming — the process is known-live and being watched.
  if (!props.message.isStreaming) {
    try {
      await getProcess(settingsStore.agentUrl, settingsStore.authToken, term.processId);
    } catch {
      ElMessage.warning('This terminal is no longer available.');
      return;
    }
  }
  if (injectedOpenTerminal) {
    injectedOpenTerminal(term);
    return;
  }
  terminalPanel.openTerminal(term);
};

// Auto-open the terminal panel as soon as a new terminal artifact arrives,
// so users see live output without having to click the chip.
watch(
  () => terminalAttachments.value.length,
  (newLen, oldLen) => {
    if (!props.message.isStreaming) return;
    if (newLen <= (oldLen ?? 0)) return;
    const latest = terminalAttachments.value[newLen - 1];
    if (latest) openTerminal(latest);
  }
);
</script>

<template>
  <div class="message-row" :class="{ 'user-row': isUser, 'agent-row': !isUser }">
    <!-- Agent Avatar (left side) -->
    <div v-if="!isUser" class="message-avatar agent-avatar">
      <img src="/agent-avatar.png" alt="Agent" class="agent-avatar-img" />
    </div>

    <div class="message-bubble" :class="{ 'user-message': isUser, 'agent-message': !isUser, 'rejected-trigger': isRejectedTrigger }">
      <div class="message-header">
        <span class="message-role">{{ isUser ? 'You' : 'Agent' }}</span>
        <span v-if="isUser && modeMeta" class="mode-chip" :title="modeMeta.label">
          <Icon :icon="modeMeta.icon" />
          {{ modeMeta.label }}
        </span>
        <span class="message-time">{{ message.timestamp.toLocaleTimeString() }}</span>
        <button v-if="message.content && !message.isStreaming" class="copy-btn" :class="{ copied }" @click.stop="copyToClipboard" :title="copied ? 'Copied!' : 'Copy to clipboard'">
          <Icon :icon="copied ? 'mdi:check' : 'mdi:content-copy'" />
        </button>
      </div>

    <div class="message-content">
      <!-- Rejected skill-event trigger: a filtered event that did NOT match the
           user's automation rule. Shown for visibility; the agent never ran. -->
      <div v-if="isRejectedTrigger" class="rejected-trigger-banner">
        <Icon icon="mdi:filter-remove-outline" class="rejected-trigger-icon" />
        <div class="rejected-trigger-text">
          <span class="rejected-trigger-title">Trigger skipped — didn't match your rule</span>
          <span v-if="message.rejectedReason" class="rejected-trigger-reason">{{ message.rejectedReason }}</span>
        </div>
      </div>

      <!-- An event run reporting its result back into this conversation. The
           turn is machine-made, so say which rule made it and whether that rule
           is done or will report again. -->
      <div v-if="isEventResult" class="event-result-banner">
        <Icon icon="mdi:lightning-bolt-outline" class="event-result-icon" />
        <div class="event-result-text">
          <span class="event-result-title">{{ eventResultTitle }}</span>
          <span class="event-result-note">{{ eventResultNote }}</span>
        </div>
        <a
          v-if="message.eventRunId && eventRunProfile"
          class="event-result-link"
          @click.stop.prevent="openEventRun"
        >
          Open run
        </a>
      </div>

      <!-- Main text content -->
      <div v-if="message.content" class="text-content marked-content" v-html="parsedContent" v-link-blank></div>

      <!-- Streaming cursor -->
      <span v-if="message.isStreaming" class="streaming-cursor">▊</span>

      <!-- A group message the agent read but chose not to answer. Said plainly,
           because an unanswered question in a transcript otherwise reads as the
           agent having failed rather than having decided. -->
      <div v-if="message.quietReason" class="quiet-note">
        <Icon icon="mdi:volume-off" class="quiet-note-icon" />
        <span>No reply — {{ message.quietReason }}</span>
      </div>

      <!-- Token usage + estimated cost (expandable per sub-agent/tool) -->
      <MessageUsageChip v-if="message.tokenUsage" :message="message" :conversation-id="conversationId" />

      <!-- Latency information -->
      <div v-if="latencyDisplay && !isUser" class="latency-info">
        {{ latencyDisplay }}
      </div>

      <!-- Collapsible Thinking Process Timeline -->
      <ThinkingProcess
        :steps="message.thinkingSteps || []"
        :is-streaming="message.isStreaming"
        :conversation-id="conversationId"
        :request-sent-at="message.latency?.requestSentAt"
      />

      <!-- File attachments carousel (bottom of bubble) -->
      <div v-if="hasCarouselContent || showTodoChip" class="file-carousel-section">
        <div class="file-carousel">
          <!-- Plan/todo snapshot chip: always first, ahead of file/terminal chips. -->
          <TodoChip
            v-if="showTodoChip"
            :message="message"
            :conversation-id="conversationId"
          />
          <div
            v-for="term in terminalAttachments"
            :key="`term-${term.processId}`"
            class="file-chip terminal-chip"
            :title="term.command"
            role="button"
            @click="openTerminal(term)"
          >
            <Icon icon="mdi:console" class="file-chip-icon terminal-icon" />
            <span class="file-chip-name">{{ term.commandShort || term.command }}</span>
          </div>
          <div v-for="(file, fIdx) in fileAttachments" :key="fIdx" class="file-chip" :class="{ 'file-chip-image': isImageMime(file.mimeType) && !failedThumbs.has(file.uri) }">
            <!-- Image thumbnail (falls back to a file icon if the image is gone) -->
            <img v-if="isImageMime(file.mimeType) && !failedThumbs.has(file.uri)" :src="resolveFileUrl(file.uri)" :alt="file.name" class="file-chip-thumb" loading="lazy" @error="failedThumbs.add(file.uri)" />
            <!-- File type icon -->
            <Icon v-else :icon="getFileIcon(file.mimeType)" class="file-chip-icon" :class="{ 'pdf-icon': isPdfMime(file.mimeType), 'text-icon': file.mimeType?.startsWith('text/') }" />
            <span class="file-chip-name" :title="file.name">{{ file.name }}</span>
            <!-- Hover actions -->
            <div class="file-chip-actions">
              <button class="file-action-btn" title="Open in new tab" @click.stop="openFileInNewTab(file.uri)">
                <Icon icon="mdi:open-in-new" />
              </button>
              <button class="file-action-btn" title="Download" @click.stop="downloadFile(file.uri, file.name)">
                <Icon icon="mdi:download" />
              </button>
              <button v-if="file.mimeType?.startsWith('text/') || file.mimeType?.includes('json') || file.mimeType?.includes('xml')" class="file-action-btn" :class="{ copied: fileCopied === file.name }" :title="fileCopied === file.name ? 'Copied!' : 'Copy content'" @click.stop="copyFileContent(resolveFileUrl(file.uri), file.name)">
                <Icon :icon="fileCopied === file.name ? 'mdi:check' : 'mdi:content-copy'" />
              </button>
            </div>
          </div>
        </div>
      </div>
      </div>
    </div>

    <!-- User Avatar (right side) -->
    <div v-if="isUser" class="message-avatar user-avatar">
      <Icon icon="mdi:account-circle" />
    </div>
  </div>
</template>

<style scoped>
/* Message Row Container */
.message-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin: 4px 0;
  animation: messageSlideIn 0.3s ease;
}

.user-row {
  flex-direction: row-reverse;
  justify-content: flex-start;
}

.agent-row {
  flex-direction: row;
  justify-content: flex-start;
}

/* Avatar Styling */
.message-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 20px;
  margin-top: 2px;
  transition: transform 0.2s ease;
}

.agent-avatar {
  background: var(--primary-color);
  color: white;
  overflow: hidden;
}

.agent-avatar-img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}

.user-avatar {
  background: linear-gradient(135deg, var(--primary-color) 0%, var(--primary-light) 100%);
  color: white;
}

.message-avatar:hover {
  transform: scale(1.05);
}

/* Message Bubble */
.message-bubble {
  padding: 10px 10px; /* Bubble inner padding: vertical | horizontal */
  position: relative;
  display: flex;
  flex-direction: column;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
  width: fit-content;
}

.user-row .message-bubble {
  max-width: 75%;
}

.agent-row .message-bubble {
  max-width: 85%;
}

/* User Message - Modern Blue Bubble */
.user-message {
  background: var(--primary-color);
  color: white;
  border-radius: 18px 18px 4px 18px;
  box-shadow: 0 1px 6px rgba(37, 99, 235, 0.2);
}

.user-message:hover {
  box-shadow: 0 2px 10px rgba(37, 99, 235, 0.3);
}

[data-theme="dark"] .user-message {
  background: var(--primary-color);
  box-shadow: 0 1px 6px rgba(59, 130, 246, 0.3);
}

[data-theme="dark"] .user-message:hover {
  box-shadow: 0 2px 10px rgba(59, 130, 246, 0.4);
}

/* Agent Message - Refined Card Style */
.agent-message {
  background: var(--surface-color);
  color: var(--text-primary);
  border-radius: 18px 18px 18px 4px;
  border-left: 3px solid var(--primary-color);
  box-shadow: 0 1px 8px rgba(0, 0, 0, 0.06);
}

.agent-message:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
}

[data-theme="dark"] .agent-message {
  box-shadow: 0 1px 8px rgba(0, 0, 0, 0.2);
}

[data-theme="dark"] .agent-message:hover {
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.3);
}

/* Rejected skill-event trigger: muted, de-emphasized, with a warning accent. */
.agent-message.rejected-trigger {
  border-left-color: var(--warning-color, #f59e0b);
  opacity: 0.78;
}
.rejected-trigger-banner {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 10px;
  margin-bottom: 8px;
  border-radius: 8px;
  background: var(--warning-bg, rgba(245, 158, 11, 0.1));
  border: 1px solid var(--warning-color, #f59e0b);
}
.rejected-trigger-icon {
  flex-shrink: 0;
  margin-top: 2px;
  font-size: 1.05rem;
  color: var(--warning-color, #f59e0b);
}
.rejected-trigger-text { display: flex; flex-direction: column; gap: 2px; }
.rejected-trigger-title {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--warning-color, #f59e0b);
}
.rejected-trigger-reason {
  font-size: 0.8rem;
  color: var(--text-secondary);
}

/* Automation result: a turn an event run reported back. Informational, not a
   warning — the result is wanted, it just wasn't asked for just now. */
.event-result-banner {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 10px;
  margin-bottom: 8px;
  border-radius: 8px;
  background: var(--primary-bg, rgba(59, 130, 246, 0.1));
  border: 1px solid var(--primary-color, #3b82f6);
}
.event-result-icon {
  flex-shrink: 0;
  margin-top: 2px;
  font-size: 1.05rem;
  color: var(--primary-color, #3b82f6);
}
.event-result-text { display: flex; flex-direction: column; gap: 2px; }
.event-result-title {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--primary-color, #3b82f6);
}
.event-result-note {
  font-size: 0.8rem;
  color: var(--text-secondary);
}
.event-result-link {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 0.8rem;
  color: var(--primary-color, #3b82f6);
  cursor: pointer;
  text-decoration: none;
}
.event-result-link:hover { text-decoration: underline; }

/* "The agent read this and said nothing" — a footnote, not a warning. */
.quiet-note {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 6px;
  font-size: 0.72rem;
  color: var(--text-secondary);
  opacity: 0.8;
}
.quiet-note-icon {
  flex-shrink: 0;
  font-size: 0.85rem;
}

/* Message Header */
.message-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 2px;
  font-size: 0.7rem;
  opacity: 0.9;
}

.message-role {
  font-weight: 600;
  color: var(--text-primary);
}

.agent-message .message-role {
  color: var(--primary-color);
}

.user-message .message-role {
  color: rgba(255, 255, 255, 0.9);
}

.mode-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 6px;
  border-radius: 8px;
  font-size: 0.65rem;
  font-weight: 600;
  background: rgba(255, 255, 255, 0.18);
  color: rgba(255, 255, 255, 0.95);
}

.message-time {
  margin-left: auto;
  opacity: 0.7;
  font-size: 0.7rem;
}

.copy-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px;
  border-radius: 4px;
  font-size: 0.85rem;
  opacity: 0;
  transition: opacity 0.15s ease, color 0.15s ease;
  color: rgba(255, 255, 255, 0.7);
}

.agent-message .copy-btn {
  color: var(--text-tertiary);
}

.copy-btn.copied {
  opacity: 1 !important;
  color: var(--success-color);
}

.agent-message .copy-btn.copied {
  color: var(--success-color);
}

.message-bubble:hover .copy-btn {
  opacity: 1;
}

.copy-btn:hover {
  color: rgba(255, 255, 255, 0.95);
}

.agent-message .copy-btn:hover {
  color: var(--primary-color);
}

.agent-message .message-time {
  color: var(--text-tertiary);
}

.user-message .message-time {
  color: rgba(255, 255, 255, 0.7);
}

/* Message Content */
.message-content {
  line-height: 1.25; /* Message text line spacing */
  width: fit-content;
  max-width: 100%;
}

.text-content {
  margin: 0;
  line-height: 1.25; /* Plain text line spacing */
}

/* User Message - Override text colors for white-on-blue */
.user-message .message-content {
  color: white;
}

/* Markdown content styles */
/* The neutral Markdown rules live in styles/markdown.css, shared with the group
   room's bubble. What stays here is only what this bubble inverts: white text on
   the blue "you" bubble, where the shared surface colours would vanish. */

.user-message :deep(.marked-content code) {
  background: rgba(255, 255, 255, 0.2);
  color: white;
  border: 1px solid rgba(255, 255, 255, 0.3);
}

.user-message :deep(.marked-content pre) {
  background: rgba(0, 0, 0, 0.15);
  border: 1px solid rgba(255, 255, 255, 0.2);
}

.user-message :deep(.marked-content a) {
  color: #bfdbfe;
  text-decoration: underline;
}

.user-message :deep(.marked-content a:hover) {
  color: white;
}

/* Streaming cursor */
.streaming-cursor {
  display: inline-block;
  width: 2px;
  height: 1.2em;
  background: var(--primary-color);
  margin-left: 2px;
  vertical-align: middle;
  animation: blink 1s infinite;
}

/* Token usage */
.token-usage {
  margin-top: 8px;
  padding: 6px 10px;
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  font-size: 0.7rem;
  color: var(--text-secondary);
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  display: inline-block;
}

.user-message .token-usage {
  background: rgba(255, 255, 255, 0.15);
  border: 1px solid rgba(255, 255, 255, 0.25);
  color: rgba(255, 255, 255, 0.85);
}

/* Latency info */
.latency-info {
  margin-top: 8px;
  padding: 6px 10px;
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  font-size: 0.7rem;
  color: var(--text-secondary);
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  display: inline-block;
}

/* Reasoning Summary Section (collapsible; sits below Thinking Process) */
.reasoning-summary {
  margin-top: 10px;
  padding: 4px 12px;
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
  border-left: 3px solid var(--primary-color, #4f8cff);
  border-radius: 8px;
  transition: all 0.2s ease;
}

.reasoning-summary :deep(.el-collapse) {
  border: none;
  background: transparent;
}

.reasoning-summary :deep(.el-collapse-item__header) {
  background: transparent;
  border: none;
  height: 28px;
  line-height: 28px;
  min-height: 28px;
  font-weight: 600;
  color: var(--text-primary);
}

.reasoning-summary :deep(.el-collapse-item__wrap) {
  background: transparent;
  border: none;
}

.reasoning-summary :deep(.el-collapse-item__content) {
  padding: 10px 0 0 0;
  color: var(--text-primary);
}

.reasoning-summary-body :deep(p:first-child) {
  margin-top: 0;
}

.reasoning-summary-body :deep(p:last-child) {
  margin-bottom: 0;
}

/* Animation */
@keyframes messageSlideIn {
  from {
    opacity: 0;
    transform: translateY(15px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* ── File Carousel (bottom of agent bubble) ── */
.file-carousel-section {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border-color);
}

.file-carousel {
  display: flex;
  align-items: center;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 4px;
  scrollbar-width: thin;
  scrollbar-color: var(--border-color) transparent;
}

.file-carousel::-webkit-scrollbar {
  height: 4px;
}
.file-carousel::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 2px;
}

/* Individual chip card */
.file-chip {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  min-width: 140px;
  max-width: 200px;
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  flex-shrink: 0;
  cursor: default;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.file-chip:hover {
  border-color: var(--primary-color);
  box-shadow: 0 1px 6px rgba(0, 0, 0, 0.08);
}

/* Image-type chip: show thumbnail */
.file-chip-image {
  flex-direction: column;
  align-items: stretch;
  padding: 4px;
  gap: 4px;
  min-width: 120px;
  max-width: 160px;
}

.file-chip-thumb {
  width: 100%;
  height: 80px;
  object-fit: cover;
  border-radius: 5px;
  background: var(--surface-color);
}

.file-chip-image .file-chip-name {
  padding: 0 4px;
}

/* Non-image icon */
.file-chip-icon {
  font-size: 1.3em;
  flex-shrink: 0;
  color: var(--text-secondary);
}
.file-chip-icon.pdf-icon { color: #e53935; }
.file-chip-icon.text-icon { color: var(--primary-color); }

/* Terminal chip — console-icon variant, clickable */
.file-chip.terminal-chip {
  background: #0f172a;
  border-color: #1f2937;
  cursor: pointer;
}
.file-chip.terminal-chip:hover {
  border-color: var(--primary-color);
  background: #111a2e;
}
.file-chip.terminal-chip .file-chip-name {
  color: #cbd5f5;
  font-family: Consolas, Monaco, "Courier New", monospace;
}
.file-chip-icon.terminal-icon { color: #22c55e; }

.file-chip-name {
  font-size: 0.78rem;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

/* Hover action buttons — hidden by default */
.file-chip-actions {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  background: rgba(var(--surface-rgb, 255,255,255), 0.85);
  border-radius: 8px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease;
}

[data-theme="dark"] .file-chip-actions {
  background: rgba(30, 30, 30, 0.88);
}

.file-chip:hover .file-chip-actions {
  opacity: 1;
  pointer-events: auto;
}

.file-action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--surface-color);
  color: var(--text-secondary);
  cursor: pointer;
  font: inherit;
  font-size: 0.95rem;
  text-decoration: none;
  appearance: none;
  transition: all 0.12s ease;
}

.file-action-btn:hover {
  color: var(--primary-color);
  border-color: var(--primary-color);
}

.file-action-btn.copied {
  color: var(--success-color);
  border-color: var(--success-color);
}

</style>
