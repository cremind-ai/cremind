<script setup lang="ts">
/**
 * The collapsible "Thinking Process" timeline: every tool a turn called, what
 * it was given, and what came back.
 *
 * Extracted from MessageBubble so a group room can show a member's steps
 * without borrowing the bubble. A bubble is a two-party thing — it hardcodes a
 * You/Agent role label, an avatar and a left/right side — so nesting one inside
 * a room post would render a second header under the post's own.
 */
import { computed, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { ElMessage } from 'element-plus';
import type { StepTokenUsage, ThinkingStep } from '../stores/chat';
import { formatTokens } from '../utils/usageFormat';
import { useSettingsStore } from '../stores/settings';

const props = withDefaults(
  defineProps<{
    steps: ThinkingStep[];
    // Drives the auto-expand-while-working / auto-collapse-at-the-end pair.
    isStreaming?: boolean;
    // The conversation these steps belong to. Nothing in the timeline reads it
    // yet; it is in the contract so a caller rendering steps from a
    // conversation other than the one on screen (a room seat) can say which
    // without a signature change.
    conversationId?: string | null;
    title?: string;
    // Start of the turn, used as the baseline for the first step's elapsed
    // label. Without it that one step shows no timing (the rest are measured
    // against the step before them).
    requestSentAt?: number;
  }>(),
  { title: 'Thinking Process' },
);

const settingsStore = useSettingsStore();

// Group per-tool thinking steps by ``step`` so parallel tool calls in one model
// turn render together under a single "Step N". Each group also carries the
// reasoning call's token usage (``tokens``) for that step — every tool call in a
// group shares the one reasoning call, so the first tool with usage is
// authoritative; null for steps persisted before per-step tokens shipped.
const thinkingGroups = computed(() => {
  const groups: { step: number | null; tools: any[]; tokens: StepTokenUsage | null }[] = [];
  for (const s of props.steps) {
    const last = groups[groups.length - 1];
    if (last && s.step != null && last.step === s.step) {
      last.tools.push(s);
    } else {
      groups.push({ step: s.step ?? null, tools: [s], tokens: null });
    }
  }
  for (const g of groups) {
    g.tokens = (g.tools.find(t => t.tokenUsage)?.tokenUsage as StepTokenUsage) ?? null;
  }
  return groups;
});

// Format milliseconds to human-readable string
const formatLatencyMs = (ms: number): string => {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
};

// Latency for a grouped step (relative to the previous group / request start).
const groupLatency = (group: any, gIdx: number): string => {
  const tool = group.tools?.[0];
  if (!tool?.receivedAt) return '';
  let ms: number;
  if (gIdx === 0) {
    if (!props.requestSentAt) return '';
    ms = tool.receivedAt - props.requestSentAt;
  } else {
    const prev = thinkingGroups.value[gIdx - 1]?.tools?.[0];
    if (!prev?.receivedAt) return '';
    ms = tool.receivedAt - prev.receivedAt;
  }
  return ms > 0 ? ` · ${formatLatencyMs(ms)}` : '';
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

// Determine if a MIME type is a PDF
const isPdfMime = (mime: string): boolean => {
  return mime === 'application/pdf';
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

// Collapse state for thinking process timeline
const activeCollapse = ref<string[]>([]);

// Track when the collapse was last expanded to prevent expand-then-immediately-collapse
const lastExpandTime = ref<number>(0);

// Track mouse down position to detect text selection drag
const mouseDownPos = ref<{ x: number; y: number } | null>(null);

// Track when the collapse is expanded to prevent immediate re-collapse.
// Registered before the auto-expand below so it still observes the expansion
// that one performs on mount.
watch(activeCollapse, (newVal, oldVal) => {
  // If we just expanded (went from [] to ['thinking'])
  if (newVal.includes('thinking') && !oldVal.includes('thinking')) {
    lastExpandTime.value = Date.now();
  }
});

// Auto-expand thinking section during streaming so user sees real-time steps.
// ``immediate`` because this component only exists once there IS a step: the
// arrival that would have tripped the watcher is the same one that mounts it,
// so waiting for a change would mean never expanding at all.
watch(
  () => props.steps.length,
  (newLen) => {
    if (newLen && newLen > 0 && props.isStreaming) {
      if (!activeCollapse.value.includes('thinking')) {
        activeCollapse.value = ['thinking'];
      }
    }
  },
  { immediate: true },
);

// Collapse automatically when streaming finishes
watch(
  () => props.isStreaming,
  (streaming) => {
    if (!streaming && activeCollapse.value.includes('thinking')) {
      activeCollapse.value = [];
    }
  }
);

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
  <div
    v-if="steps.length"
    class="thinking-section"
    @mousedown="handleMouseDown"
    @click="handleThinkingClick"
  >
    <el-collapse v-model="activeCollapse">
      <el-collapse-item name="thinking">
        <template #title>
          <span class="collapse-title">
            <Icon icon="mdi:brain" class="collapse-icon" />
            {{ title }} ({{ thinkingGroups.length }} steps)
          </span>
        </template>
        <el-timeline>
          <el-timeline-item
            v-for="(group, gIdx) in thinkingGroups"
            :key="gIdx"
            :type="group.tools.some(t => t.result?.length) ? 'success' : 'primary'"
            :hollow="!group.tools.some(t => t.result?.length)"
            :timestamp="`Step ${gIdx + 1}${groupLatency(group, gIdx)}`"
            placement="top"
          >
            <el-card shadow="never" class="timeline-card">
              <div
                v-if="group.tokens"
                class="step-tokens"
                title="Tokens for the reasoning call that produced this step"
              >
                <span class="step-tokens-item">
                  <span class="step-tokens-num">{{ formatTokens(group.tokens.inputTokens) }}</span> new input
                </span>
                <span v-if="group.tokens.cacheReadTokens > 0" class="step-tokens-item">
                  <span class="step-tokens-num">{{ formatTokens(group.tokens.cacheReadTokens) }}</span> cached
                </span>
                <span v-if="group.tokens.cacheCreationTokens > 0" class="step-tokens-item">
                  <span class="step-tokens-num">{{ formatTokens(group.tokens.cacheCreationTokens) }}</span> cache-write
                </span>
                <span class="step-tokens-item">
                  <span class="step-tokens-num">{{ formatTokens(group.tokens.outputTokens) }}</span> output
                </span>
              </div>
              <div v-for="(tool, tIdx) in group.tools" :key="tIdx" class="step-content">
                <span v-if="tool.modelLabel" class="model-badge step-model">
                  {{ tool.modelLabel }}
                </span>
                <div class="step-detail">
                  <span class="step-label">
                    <Icon icon="mdi:flash" class="step-icon" /> Tool
                  </span>
                  <p>{{ tool.tool }}</p>
                </div>
                <div v-if="tool.toolInput" class="step-detail">
                  <span class="step-label">
                    <Icon icon="mdi:code-tags" class="step-icon" /> Input
                  </span>
                  <p class="action-input">{{ tool.toolInput }}</p>
                </div>
                <div v-if="tool.result && tool.result.length" class="step-detail observation">
                  <span class="step-label">
                    <Icon icon="mdi:check-circle" class="step-icon success-icon" /> Result
                  </span>
                  <pre v-if="formatObservationText(tool.result)" class="observation-code"><code>{{ formatObservationText(tool.result) }}</code></pre>
                  <!-- Compact file cards inside the result -->
                  <div v-for="(filePart, fIdx) in getObservationFiles(tool.result)" :key="fIdx" class="file-card">
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
</template>

<style scoped>
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

.step-model {
  float: right;
  font-size: 0.75em;
}

/* Per-step reasoning-call token counts, above the step's tool calls. */
.step-tokens {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px 12px;
  margin-bottom: 8px;
  font-size: 0.75rem;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  cursor: default;
}

.step-tokens-item {
  white-space: nowrap;
}

.step-tokens-num {
  font-weight: 600;
  color: var(--el-text-color-regular, inherit);
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
