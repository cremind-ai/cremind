<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { ChatMessage, FileAttachment, TerminalAttachment } from '../stores/chat';
import { Marked } from 'marked';
import { markedHighlight } from 'marked-highlight';
import hljs from 'highlight.js';
import vLinkBlank from '../directives/v-link-blank';
import { Icon } from '@iconify/vue';
import { copyTextToClipboard } from '../utils/clipboard';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';

const props = defineProps<{
  message: ChatMessage;
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

// Configure marked with syntax highlighting + URL rewriting
const marked = new Marked(
  markedHighlight({
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      const language = hljs.getLanguage(lang) ? lang : 'plaintext';
      return hljs.highlight(code, { language }).value;
    }
  })
);
marked.use({
  silent: true,
  breaks: true,
  renderer: {
    image({ href, title, text }) {
      const src = resolveApiUrl(href);
      const alt = text || '';
      const titleAttr = title ? ` title="${title}"` : '';
      return `<img src="${src}" alt="${alt}"${titleAttr} loading="lazy" style="max-width:100%;border-radius:6px;" />`;
    },
    link({ href, title, tokens }) {
      const url = resolveApiUrl(href);
      const titleAttr = title ? ` title="${title}"` : '';
      const text = this.parser.parseInline(tokens);
      if (url.match(/\/api\/files\//)) {
        return `<a href="${url}"${titleAttr} target="_blank" rel="noopener">${text}</a>`;
      }
      return `<a href="${url}"${titleAttr}>${text}</a>`;
    },
  },
});

const isUser = computed(() => props.message.role === 'user');
const hasThinking = computed(() => props.message.thinkingSteps && props.message.thinkingSteps.length > 0);

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

const parsedSummary = computed(() => {
  if (!props.message.summary) return '';
  return marked.parse(props.message.summary) as string;
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

// Calculate per-step latency for timeline display (relative to previous step)
const getStepLatency = (step: any, index: number): string => {
  if (!step.receivedAt || !props.message.latency?.requestSentAt) {
    return '';
  }
  
  let ms: number;
  if (index === 0) {
    // First step: calculate from when the user message was sent
    ms = step.receivedAt - props.message.latency.requestSentAt;
  } else {
    // Subsequent steps: calculate from the previous step's receivedAt time
    const prevStep = props.message.thinkingSteps?.[index - 1];
    if (!prevStep?.receivedAt) {
      return '';
    }
    ms = step.receivedAt - prevStep.receivedAt;
  }
  
  return ` · ${formatLatencyMs(ms)}`;
};

// Extract text-only observation parts for display in code block
const formatObservationText = (parts: any[]): string => {
  if (!parts || !Array.isArray(parts)) return '';
  const segments: string[] = [];
  for (const part of parts) {
    if (part.kind === 'text' && part.text) {
      segments.push(part.text);
    } else if (part.kind === 'data' && part.data) {
      try {
        segments.push(JSON.stringify(part.data, null, 2));
      } catch {
        segments.push(String(part.data));
      }
    }
    // FileParts are rendered separately — not included in text block
  }
  return segments.join('\n');
};

// Extract file parts from observation for inline rendering
const getObservationFiles = (parts: any[]): any[] => {
  if (!parts || !Array.isArray(parts)) return [];
  return parts.filter((p: any) => p.kind === 'file' && p.file);
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
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
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
    if (!resp.ok) return;
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

// Copy file text content to clipboard
const fileCopied = ref<string | null>(null);
const copyFileContent = async (url: string, fileName: string) => {
  try {
    const resp = await fetch(url, { headers: authHeaders() });
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

const terminalPanel = useTerminalPanelStore();

const openTerminal = (term: TerminalAttachment) => {
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

// Collapse state for thinking process timeline
const activeCollapse = ref<string[]>([]);

// Collapse state for the reasoning summary (collapsed by default)
const activeSummaryCollapse = ref<string[]>([]);

// Track when the collapse was last expanded to prevent expand-then-immediately-collapse
const lastExpandTime = ref<number>(0);

// Track mouse down position to detect text selection drag
const mouseDownPos = ref<{ x: number; y: number } | null>(null);

// Auto-expand thinking section during streaming so user sees real-time steps
watch(
  () => props.message.thinkingSteps?.length,
  (newLen) => {
    if (newLen && newLen > 0 && props.message.isStreaming) {
      if (!activeCollapse.value.includes('thinking')) {
        activeCollapse.value = ['thinking'];
      }
    }
  }
);

// Collapse automatically when streaming finishes
watch(
  () => props.message.isStreaming,
  (streaming) => {
    if (!streaming && activeCollapse.value.includes('thinking')) {
      activeCollapse.value = [];
    }
  }
);

// Track when the collapse is expanded to prevent immediate re-collapse
watch(activeCollapse, (newVal, oldVal) => {
  // If we just expanded (went from [] to ['thinking'])
  if (newVal.includes('thinking') && !oldVal.includes('thinking')) {
    lastExpandTime.value = Date.now();
  }
});

// Handle mouse down to track position for drag detection
const handleMouseDown = (event: MouseEvent) => {
  mouseDownPos.value = { x: event.clientX, y: event.clientY };
};

// Handle click on thinking section to collapse (with guards)
const handleThinkingClick = (event: MouseEvent) => {
  // Guard 1: Only collapse if currently expanded
  if (!activeCollapse.value.includes('thinking')) {
    return;
  }

  // Guard 2: Prevent immediate collapse after expand (within 300ms)
  if (Date.now() - lastExpandTime.value < 300) {
    return;
  }

  // Guard 3: Don't collapse if clicking on interactive elements
  let target = event.target as HTMLElement;
  while (target && target !== event.currentTarget) {
    const tagName = target.tagName.toUpperCase();
    if (
      tagName === 'BUTTON' ||
      tagName === 'A' ||
      tagName === 'INPUT' ||
      tagName === 'TEXTAREA' ||
      target.getAttribute('role') === 'button'
    ) {
      return;
    }
    target = target.parentElement as HTMLElement;
  }

  // Guard 4: Don't collapse if user has text selected
  const selection = window.getSelection();
  if (selection && selection.toString().trim().length > 0) {
    return;
  }

  // Guard 5: Don't collapse if mouse was dragged (text selection)
  if (mouseDownPos.value) {
    const dx = Math.abs(event.clientX - mouseDownPos.value.x);
    const dy = Math.abs(event.clientY - mouseDownPos.value.y);
    if (dx > 5 || dy > 5) {
      return;
    }
  }

  // All guards passed - collapse the thinking section
  activeCollapse.value = [];
};
</script>

<template>
  <div class="message-row" :class="{ 'user-row': isUser, 'agent-row': !isUser }">
    <!-- Agent Avatar (left side) -->
    <div v-if="!isUser" class="message-avatar agent-avatar">
      <img src="/agent-avatar.png" alt="Agent" class="agent-avatar-img" />
    </div>

    <div class="message-bubble" :class="{ 'user-message': isUser, 'agent-message': !isUser }">
      <div class="message-header">
        <span class="message-role">{{ isUser ? 'You' : 'Agent' }}</span>
        <span class="message-time">{{ message.timestamp.toLocaleTimeString() }}</span>
        <button v-if="message.content && !message.isStreaming" class="copy-btn" :class="{ copied }" @click.stop="copyToClipboard" :title="copied ? 'Copied!' : 'Copy to clipboard'">
          <Icon :icon="copied ? 'mdi:check' : 'mdi:content-copy'" />
        </button>
      </div>

    <div class="message-content">
      <!-- Main text content -->
      <div v-if="message.content" class="text-content marked-content" v-html="parsedContent" v-link-blank></div>

      <!-- Streaming cursor -->
      <span v-if="message.isStreaming" class="streaming-cursor">▊</span>

      <!-- Token usage footer -->
      <div v-if="message.tokenUsage" class="token-usage">
        {{ message.tokenUsage.totalTokens }} tokens
        ({{ message.tokenUsage.inputTokens }} in + {{ message.tokenUsage.outputTokens }} out)
      </div>

      <!-- Latency information -->
      <div v-if="latencyDisplay && !isUser" class="latency-info">
        {{ latencyDisplay }}
      </div>

      <!-- Collapsible Thinking Process Timeline -->
      <div 
        v-if="hasThinking" 
        class="thinking-section"
        @mousedown="handleMouseDown"
        @click="handleThinkingClick"
      >
        <el-collapse v-model="activeCollapse">
          <el-collapse-item name="thinking">
            <template #title>
              <span class="collapse-title">
                <Icon icon="mdi:brain" class="collapse-icon" />
                Thinking Process ({{ message.thinkingSteps?.length || 0 }} steps)
                <span v-if="message.reasoningModelLabel" class="model-badge reasoning-model">
                  {{ message.reasoningModelLabel }}
                </span>
              </span>
            </template>
            <el-timeline>
              <el-timeline-item
                v-for="(step, index) in message.thinkingSteps"
                :key="index"
                :type="step.observation?.length ? 'success' : 'primary'"
                :hollow="!step.observation?.length"
                :timestamp="`Step ${index + 1}${getStepLatency(step, index)}`"
                placement="top"
              >
                <el-card shadow="never" class="timeline-card">
                  <div class="step-content">
                    <span v-if="step.modelLabel" class="model-badge step-model">
                      {{ step.modelLabel }}
                    </span>
                    <div class="step-detail">
                      <span class="step-label">
                        <Icon icon="mdi:brain" class="step-icon" /> Thought
                      </span>
                      <p>{{ step.thought }}</p>
                    </div>
                    <div class="step-detail">
                      <span class="step-label">
                        <Icon icon="mdi:flash" class="step-icon" /> Action
                      </span>
                      <p>{{ step.action }}</p>
                    </div>
                    <div v-if="step.actionInput" class="step-detail">
                      <span class="step-label">
                        <Icon icon="mdi:code-tags" class="step-icon" /> Input
                      </span>
                      <p class="action-input">{{ step.actionInput }}</p>
                    </div>
                    <div v-if="step.observation && step.observation.length" class="step-detail observation">
                      <span class="step-label">
                        <Icon icon="mdi:check-circle" class="step-icon success-icon" /> Observation
                      </span>
                      <pre v-if="formatObservationText(step.observation)" class="observation-code"><code>{{ formatObservationText(step.observation) }}</code></pre>
                      <!-- Compact file cards inside observation -->
                      <div v-for="(filePart, fIdx) in getObservationFiles(step.observation)" :key="fIdx" class="file-card">
                        <Icon :icon="getFileIcon(filePart.file.mime_type)" class="file-card-icon" :class="{ 'pdf-icon': isPdfMime(filePart.file.mime_type), 'text-icon': filePart.file.mime_type?.startsWith('text/') }" />
                        <div class="file-card-info">
                          <span class="file-name">{{ filePart.file.name || 'file' }}</span>
                          <span class="file-mime">{{ filePart.file.mime_type || 'unknown' }}</span>
                        </div>
                        <button class="file-download-btn" title="Open" @click.stop="openFileInNewTab(filePart.file.uri)">
                          <Icon icon="mdi:open-in-new" />
                        </button>
                      </div>
                    </div>
                  </div>
                </el-card>
              </el-timeline-item>
            </el-timeline>
          </el-collapse-item>
        </el-collapse>
        </div>

      <!-- Reasoning summary (TL;DR of the ReAct trace; collapsible, below Thinking Process) -->
      <div v-if="message.summary" class="reasoning-summary">
        <el-collapse v-model="activeSummaryCollapse">
          <el-collapse-item name="summary">
            <template #title>
              <span class="collapse-title">
                <Icon icon="mdi:text-box-outline" class="collapse-icon" />
                Summary
              </span>
            </template>
            <div class="reasoning-summary-body marked-content" v-html="parsedSummary" v-link-blank></div>
          </el-collapse-item>
        </el-collapse>
      </div>

      <!-- File attachments carousel (bottom of bubble) -->
      <div v-if="hasCarouselContent" class="file-carousel-section">
        <div class="file-carousel">
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
          <div v-for="(file, fIdx) in fileAttachments" :key="fIdx" class="file-chip" :class="{ 'file-chip-image': isImageMime(file.mimeType) }">
            <!-- Image thumbnail -->
            <img v-if="isImageMime(file.mimeType)" :src="resolveFileUrl(file.uri)" :alt="file.name" class="file-chip-thumb" loading="lazy" />
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
.marked-content {
  /* white-space: pre-wrap; */
  word-wrap: break-word;
  line-height: 1.25; /* Markdown content line spacing */
}

:deep(.marked-content p) {
  margin: 0.1em 0; /* Paragraph spacing: top | bottom */
  white-space: pre-wrap;
  word-wrap: break-word;
}

:deep(.marked-content p:first-child) {
  margin-top: 0;
}

:deep(.marked-content p:last-child) {
  margin-bottom: 0;
}

:deep(.marked-content code) {
  background: var(--surface-hover);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 0.9em;
  border: 1px solid var(--border-color);
}

.user-message :deep(.marked-content code) {
  background: rgba(255, 255, 255, 0.2);
  color: white;
  border: 1px solid rgba(255, 255, 255, 0.3);
}

:deep(.marked-content pre) {
  margin: 0.5em 0; /* Code block outer spacing */
  padding: 8px; /* Code block inner padding */
  background: var(--surface-hover);
  border-radius: 6px;
  overflow-x: auto;
  border: 1px solid var(--border-color);
}

.user-message :deep(.marked-content pre) {
  background: rgba(0, 0, 0, 0.15);
  border: 1px solid rgba(255, 255, 255, 0.2);
}

:deep(.marked-content pre code) {
  background: transparent;
  padding: 0;
  border-radius: 0;
  display: block;
  white-space: pre;
  border: none;
}

:deep(.marked-content ul),
:deep(.marked-content ol) {
  margin: 0.3em 0; /* List outer spacing */
  padding-left: 2em; /* List indent */
}

:deep(.marked-content li) {
  margin: 0.15em 0; /* List item spacing */
}

:deep(.marked-content blockquote) {
  margin: 0.5em 0; /* Blockquote outer spacing */
  padding: 0.4em 0.8em; /* Blockquote inner padding: vertical | horizontal */
  border-left: 3px solid var(--primary-color);
  background: var(--surface-hover);
  border-radius: 0 4px 4px 0;
}

:deep(.marked-content table) {
  border-collapse: collapse;
  margin: 0.5em 0; /* Table outer spacing */
  width: 100%;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid var(--border-color);
}

:deep(.marked-content table th),
:deep(.marked-content table td) {
  border: 1px solid var(--border-color);
  padding: 8px 12px;
  text-align: left;
}

:deep(.marked-content table th) {
  background: var(--surface-hover);
  font-weight: 600;
}

:deep(.marked-content a) {
  color: var(--primary-color);
  text-decoration: underline;
}

:deep(.marked-content a:hover) {
  opacity: 0.8;
}

.user-message :deep(.marked-content a) {
  color: #bfdbfe;
  text-decoration: underline;
}

.user-message :deep(.marked-content a:hover) {
  color: white;
}

:deep(.marked-content h1),
:deep(.marked-content h2),
:deep(.marked-content h3),
:deep(.marked-content h4) {
  margin: 0.5em 0 0.25em 0; /* Heading spacing: top | right | bottom | left */
  font-weight: 600;
  line-height: 1.2; /* Heading line spacing */
}

:deep(.marked-content h1:first-child),
:deep(.marked-content h2:first-child),
:deep(.marked-content h3:first-child),
:deep(.marked-content h4:first-child) {
  margin-top: 0;
}

:deep(.marked-content hr) {
  margin: 0.75em 0; /* Horizontal rule spacing */
  border: none;
  height: 1px;
  background: var(--border-color);
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

/* Thinking Process Section */
.thinking-section {
  margin-top: 10px;
  padding: 4px 12px;
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  transition: all 0.2s ease;
}

/* Add cursor pointer when thinking section is expanded */
.thinking-section:has(.el-collapse-item.is-active) :deep(.el-collapse-item__content) {
  cursor: pointer;
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

.thinking-section :deep(.el-collapse) {
  border: none;
}

.thinking-section :deep(.el-collapse-item__header) {
  background: transparent;
  border: none;
  font-size: 0.875rem;
  height: 28px;
  line-height: 28px;
  min-height: 28px;
  font-weight: 500;
}

.thinking-section :deep(.el-collapse-item__wrap) {
  background: transparent;
  border: none;
}

.thinking-section :deep(.el-collapse-item__content) {
  padding-bottom: 8px;
  padding-top: 8px;
}

.collapse-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--primary-color);
}

.collapse-icon {
  font-size: 1.1em;
}

.model-badge {
  display: inline-block;
  font-size: 0.7em;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--el-color-info-light-8, #e6e8eb);
  color: var(--el-color-info, #909399);
  font-weight: 500;
  vertical-align: middle;
}

.reasoning-model {
  margin-left: 8px;
  padding: 1px 6px;
  font-size: 0.72em;
  line-height: 2.2;
}

.step-model {
  float: right;
  font-size: 0.75em;
}

/* Timeline styles */
.thinking-section :deep(.el-timeline) {
  padding-left: 8px;
  margin-top: 8px;
}

.thinking-section :deep(.el-timeline-item__timestamp) {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.timeline-card {
  margin-bottom: 8px;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 6px;
}

.timeline-card :deep(.el-card__body) {
  padding: 12px;
}

.step-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.step-detail {
  font-size: 0.875rem;
}

.step-label {
  display: flex;
  align-items: center;
  gap: 5px;
  font-weight: 600;
  margin-bottom: 4px;
  font-size: 0.875rem;
  color: var(--text-primary);
}

.step-icon {
  font-size: 1.1em;
  color: var(--text-secondary);
}

.success-icon {
  color: var(--success-color);
}

.step-detail p {
  margin: 0;
  padding-left: 4px;
  word-break: break-word;
  white-space: pre-wrap;
  color: var(--text-secondary);
  line-height: 1.6;
}

.step-detail.observation {
  margin-top: 6px;
  padding-top: 8px;
  border-top: 1px dashed var(--border-color);
}

.action-input {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 0.85rem;
  background: var(--surface-hover);
  padding: 6px 10px;
  border-radius: 4px;
  border: 1px solid var(--border-color);
}

.observation-code {
  margin: 4px 0 0 0;
  padding: 8px 10px;
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  overflow-x: auto;
  max-height: 300px;
  overflow-y: auto;
}

.observation-code code {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 0.82rem;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
}

/* ── File Carousel (bottom of agent bubble) ── */
.file-carousel-section {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border-color);
}

.file-carousel {
  display: flex;
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

/* ── Observation file cards (inside thinking timeline) ── */
.file-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  margin-top: 6px;
}

.file-card-icon { font-size: 1.3em; color: var(--text-secondary); flex-shrink: 0; }
.file-card-icon.pdf-icon { color: #e53935; }
.file-card-icon.text-icon { color: var(--primary-color); }

.file-card-info {
  display: flex;
  flex-direction: column;
  gap: 1px;
  flex: 1;
  min-width: 0;
}

.file-name {
  font-size: 0.8rem;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-mime {
  font-size: 0.68rem;
  color: var(--text-tertiary);
}

.file-download-btn,
.file-copy-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: 5px;
  border: 1px solid var(--border-color);
  background: var(--surface-color);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.12s ease;
  font: inherit;
  font-size: 0.9rem;
  text-decoration: none;
  appearance: none;
  flex-shrink: 0;
}

.file-download-btn:hover,
.file-copy-btn:hover {
  color: var(--primary-color);
  border-color: var(--primary-color);
}

.file-copy-btn.copied {
  color: var(--success-color);
  border-color: var(--success-color);
}
</style>
