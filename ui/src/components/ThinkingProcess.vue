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
import type { ThinkingStep } from '../stores/chat';
import { formatTokens } from '../utils/usageFormat';
import { stepElapsedLabel } from '../utils/latencyLabels';
import { groupThinkingSteps } from '../utils/streamFrames';
import { openAfterModeChange, shouldAutoClose, shouldAutoOpen } from '../utils/thinkingDisclosure';
import { useSettingsStore } from '../stores/settings';
import { useAppearanceStore } from '../stores/appearance';
import type { ThinkingProcessMode } from '../appearance/presets';

const props = withDefaults(
  defineProps<{
    steps: ThinkingStep[];
    // With the "live" Thinking Process setting, drives the open-while-working
    // / close-at-the-end pair (utils/thinkingDisclosure).
    isStreaming?: boolean;
    // The conversation these steps belong to. Nothing in the timeline reads it
    // yet; it is in the contract so a caller rendering steps from a
    // conversation other than the one on screen (a room seat) can say which
    // without a signature change.
    conversationId?: string | null;
    title?: string;
    // Start of the turn in this tab's clock. Only a fallback baseline for the
    // first step's elapsed label, used for steps that carry no server-stamped
    // ``elapsedMs``; leaving it out costs that one label and nothing else.
    requestSentAt?: number;
  }>(),
  { title: 'Thinking Process' },
);

const settingsStore = useSettingsStore();
const appearance = useAppearanceStore();

// Per-tool thinking steps grouped by step (see utils/streamFrames): parallel
// calls of one model turn share a row and its reasoning-call token usage, and
// calls the agent made itself (its automatic document reads) get a row of
// their own.
const thinkingGroups = computed(() => groupThinkingSteps(props.steps));

/**
 * How long the turn spent getting to a grouped step — from the step before it,
 * or from the start of the turn for the first one. See utils/latencyLabels.
 *
 * (In a segment of a turn a mid-turn message split, the first label is time
 * since the START of the turn, not since the split — the timeline measures the
 * turn, and a segment is a slice of one.)
 */
const groupLatency = (group: any, gIdx: number): string => stepElapsedLabel(
  group.tools?.[0],
  gIdx > 0 ? thinkingGroups.value[gIdx - 1]?.tools?.[0] : undefined,
  props.requestSentAt,
);

// Who made a group's calls, when it was not the model.
const ORIGIN_LABELS: Record<string, string> = {
  document_review: 'automatic document review',
};
const originLabel = (origin: string | null | undefined): string =>
  origin ? ORIGIN_LABELS[origin] || 'automatic' : '';

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

// Collapse state for thinking process timeline. Closed until clicked, unless
// the profile chose the "live" Thinking Process (utils/thinkingDisclosure).
const activeCollapse = ref<string[]>([]);

// Set once the user opens or closes the section: from then on nothing opens
// or closes it by itself.
const userToggled = ref(false);

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

// "live": open while the agent works, so its steps show as they arrive.
// ``immediate`` because this component only exists once there IS a step: the
// arrival that would have tripped the watcher is the same one that mounts it,
// so waiting for a change would mean never expanding at all.
watch(
  () => props.steps.length,
  (stepCount) => {
    const state = { isStreaming: !!props.isStreaming, stepCount, userToggled: userToggled.value };
    if (shouldAutoOpen(appearance.settings.thinkingProcess, state) && !activeCollapse.value.includes('thinking')) {
      activeCollapse.value = ['thinking'];
    }
  },
  { immediate: true },
);

// "live": close again when the reply is done.
watch(
  () => props.isStreaming,
  (streaming) => {
    if (
      !streaming
      && activeCollapse.value.includes('thinking')
      && shouldAutoClose(appearance.settings.thinkingProcess, { userToggled: userToggled.value })
    ) {
      activeCollapse.value = [];
    }
  }
);

// ── Auto-open: the profile's mode, switched right on this row ──
//
// It sets the mode for every reply (it is the profile's chat.thinking_process,
// the same one `cremind config set` changes), so a reply still being written
// follows a switch made on any row at once.
const autoOpen = computed(() => appearance.settings.thinkingProcess === 'live');
const isOpen = computed(() => activeCollapse.value.includes('thinking'));
const autoOpenHint = computed(() => (autoOpen.value
  ? 'Opens while the agent works, closes when it is done — in every reply. Click to turn off.'
  : 'Stays closed until you click it. Turn on to open it while the agent works — in every reply.'));

watch(
  () => appearance.settings.thinkingProcess,
  (mode) => {
    const next = openAfterModeChange(mode, {
      isStreaming: !!props.isStreaming,
      stepCount: props.steps.length,
      userToggled: userToggled.value,
      open: isOpen.value,
    });
    if (next !== null) activeCollapse.value = next ? ['thinking'] : [];
  },
);

// Said once it is saved, and only for the last of quick clicks (one save).
let toggleSeq = 0;
const toggleAutoOpen = () => {
  const mode: ThinkingProcessMode = autoOpen.value ? 'collapsed' : 'live';
  const seq = ++toggleSeq;
  appearance.update({ thinkingProcess: mode })
    .then(() => {
      if (seq !== toggleSeq) return;
      ElMessage({
        type: 'success',
        duration: 2500,
        message: mode === 'live'
          ? 'Auto-open on: the Thinking Process opens while the agent works, in every reply.'
          : 'Auto-open off: the Thinking Process stays closed until you open it, in every reply.',
      });
    })
    .catch((e: unknown) => {
      ElMessage.error(e instanceof Error ? e.message : 'Could not save the Auto-open setting');
    });
};

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
  userToggled.value = true;
  activeCollapse.value = [];
};
</script>

<template>
  <div
    v-if="steps.length"
    class="thinking-section"
    :class="{ streaming: isStreaming, open: isOpen }"
    @mousedown="handleMouseDown"
    @click="handleThinkingClick"
  >
    <!-- `change` fires on a click on the header, never for the watchers above. -->
    <el-collapse v-model="activeCollapse" @change="userToggled = true">
      <el-collapse-item name="thinking">
        <template #title>
          <span class="collapse-title">
            <Icon icon="mdi:brain" class="collapse-icon" />
            {{ title }} ({{ thinkingGroups.length }} {{ thinkingGroups.length === 1 ? 'step' : 'steps' }})
          </span>
          <!-- Inside the header, so its clicks and keys must not reach the
               header's own open/close handlers. Shown while the reply is
               written, while the row is open, and on hover or focus.
               `trigger-keys` empty: the tooltip otherwise takes Enter and
               Space (preventDefault) and the switch never toggles from the
               keyboard. -->
          <el-tooltip
            :content="autoOpenHint"
            placement="top"
            :show-after="400"
            :hide-after="0"
            :trigger-keys="[]"
          >
            <button
              type="button"
              class="auto-open"
              :class="{ on: autoOpen }"
              role="switch"
              :aria-checked="autoOpen"
              aria-label="Auto-open the Thinking Process while the agent works"
              @click.stop="toggleAutoOpen"
              @keydown.enter.stop
              @keydown.space.stop
            >
              <span class="auto-open-track" aria-hidden="true"><span class="auto-open-thumb" /></span>
              <span class="auto-open-label">Auto-open</span>
            </button>
          </el-tooltip>
        </template>
        <el-timeline>
          <el-timeline-item
            v-for="(group, gIdx) in thinkingGroups"
            :key="gIdx"
            :type="group.tools.some(t => t.result?.length) ? 'success' : 'primary'"
            :hollow="!group.tools.some(t => t.result?.length)"
            :timestamp="`Step ${gIdx + 1}${groupLatency(group, gIdx)}${group.origin ? ' · ' + originLabel(group.origin) : ''}`"
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
                <span
                  v-if="tool.origin"
                  class="model-badge step-model step-origin"
                  :title="`Made by the agent itself (${originLabel(tool.origin)}), not by the model`"
                >
                  automatic
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

/* The title slot: the label on the left, the Auto-open switch on the right
   (before Element Plus's arrow). */
.thinking-section :deep(.el-collapse-item__title) {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

/* ── Auto-open switch ── */
.auto-open {
  margin-left: auto;
  margin-right: 8px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  padding: 0 8px 0 4px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  color: var(--text-tertiary);
  font: inherit;
  font-size: 0.72rem;
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
  cursor: pointer;
  /* Hidden (and out of the tab order) until the row is in use. */
  visibility: hidden;
  opacity: 0;
  transition: opacity 0.15s ease, color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
}
.thinking-section:hover .auto-open,
.thinking-section:focus-within .auto-open,
.thinking-section.streaming .auto-open,
.thinking-section.open .auto-open {
  visibility: visible;
  opacity: 1;
}
.auto-open:hover {
  color: var(--text-secondary);
  border-color: var(--border-color);
  background: var(--surface-color);
}
.auto-open:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: 1px;
}
.auto-open.on {
  color: var(--primary-color);
}
.auto-open-track {
  position: relative;
  width: 22px;
  height: 12px;
  border-radius: 999px;
  background: var(--border-hover);
  transition: background 0.15s ease;
  flex-shrink: 0;
}
.auto-open-thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--surface-color);
  transition: transform 0.15s ease;
}
.auto-open.on .auto-open-track {
  background: var(--primary-color);
}
.auto-open.on .auto-open-thumb {
  background: var(--on-primary);
  transform: translateX(10px);
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

/* Beside the model badge, on a call the agent made itself. */
.step-origin {
  margin-right: 6px;
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
