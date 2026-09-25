<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import type { Marked } from 'marked';
import { useChatStore, type ChatMessage, type FileAttachment, type TerminalAttachment } from '../stores/chat';
import { OpenTerminalKey } from '../composables/terminalTarget';
import { useAuthedBlobUrls } from '../composables/useAuthedBlobUrl';
import { createChatMarked } from '../utils/markdown';
import {
  citationMarkedExtension,
  citationStatusInfo,
  mentionsCitations,
  numberTokens,
  toPlainFootnotes,
  type CitationItem,
  type CitationSource,
} from '../utils/citations';
import vLinkBlank from '../directives/v-link-blank';
import { Icon } from '@iconify/vue';
import { copyTextToClipboard } from '../utils/clipboard';
import { latencySummary } from '../utils/latencyLabels';
import { chatModeMeta } from '../constants/chatModes';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import { useCitationsStore } from '../stores/citations';
import { getProcess } from '../services/processApi';
import { ElMessage, ElPopover } from 'element-plus';
import MessageUsageChip from './MessageUsageChip.vue';
import ThinkingProcess from './ThinkingProcess.vue';
import TodoChip from './plan/TodoChip.vue';
import CitationSourcesFooter from './citations/CitationSourcesFooter.vue';
import CitationViewerDrawer from './citations/CitationViewerDrawer.vue';

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

// ── User Documents citations ────────────────────────────────────────────────
// An answer that cites the user's files carries tokens like
// "[ud:k7m2xq9a#3f9c2e1b]". They render as numbered chips (utils/citations.ts);
// hovering one previews the source, clicking opens it in the viewer, and a
// "Sources" list follows the text. The verified items come with the message
// (metadata / the live `citations` frame) or, for answers saved without them,
// from the citations store.
const chatStore = useChatStore();
const citationsStore = useCitationsStore();
// Tokens are verified against the conversation they were issued in.
const citeConversationId = computed(
  () => props.conversationId ?? chatStore.activeConversationId ?? null,
);

// Distinct tokens → their number in THIS bubble's text (first appearance).
const citeNumbers = computed<Map<string, number>>(() =>
  !isUser.value && mentionsCitations(props.message.content)
    ? numberTokens(props.message.content)
    : new Map(),
);
const hasCitations = computed(() => citeNumbers.value.size > 0);
const ownCitationItems = computed(
  () => new Map((props.message.citations?.items ?? []).map(item => [item.token, item])),
);
const citationItemFor = (token: string): CitationItem | undefined =>
  ownCitationItems.value.get(token) ?? citationsStore.itemFor(citeConversationId.value, token);

const citationSources = computed<CitationSource[]>(() =>
  Array.from(citeNumbers.value, ([token, n]) => ({ token, n, item: citationItemFor(token) })),
);

// Resolve what the message did not bring — but only once it has finished: a
// streaming answer's tokens are still being written, and its verified items
// arrive with the `citations` frame just before `complete`.
const unresolvedTokens = computed(() =>
  hasCitations.value && !props.message.isStreaming
    ? Array.from(citeNumbers.value.keys()).filter(token => !ownCitationItems.value.has(token))
    : [],
);
watch(
  unresolvedTokens,
  (tokens) => { if (tokens.length) void citationsStore.ensure(citeConversationId.value, tokens); },
  { immediate: true },
);

const copied = ref(false);
const copyToClipboard = async () => {
  if (!props.message.content) return;
  // Tokens mean nothing outside Cremind: copy numbered footnotes instead.
  const text = hasCitations.value
    ? toPlainFootnotes(
        props.message.content,
        citationSources.value.flatMap(source => (source.item ? [source.item] : [])),
      )
    : props.message.content;
  if (!(await copyTextToClipboard(text))) return;
  copied.value = true;
  setTimeout(() => { copied.value = false; }, 2000);
};

// A second renderer with the chip extension, built only for bubbles that
// need it; everything else keeps the plain one.
let citedMarked: Marked | null = null;
const parsedContent = computed(() => {
  const content = props.message.content;
  if (!content) return '';
  if (!isUser.value && mentionsCitations(content)) {
    if (!citedMarked) {
      citedMarked = createChatMarked(resolveApiUrl, [citationMarkedExtension({ lookup: citationItemFor })]);
    }
    return citedMarked.parse(content) as string;
  }
  return marked.parse(content) as string;
});

// Hover preview: one popover per bubble, anchored (virtual ref) to whichever
// chip the pointer or focus is on. Delegated from the rendered HTML, since
// the chips are v-html and cannot carry Vue listeners.
const contentEl = ref<HTMLElement | null>(null);
const preview = ref<{ el: HTMLElement; token: string; n: number } | null>(null);
let hideTimer: ReturnType<typeof setTimeout> | null = null;

const cancelPreviewHide = () => {
  if (hideTimer) clearTimeout(hideTimer);
  hideTimer = null;
};
// Delayed so the pointer can travel from the chip into the popover.
const schedulePreviewHide = () => {
  cancelPreviewHide();
  hideTimer = setTimeout(() => { preview.value = null; }, 180);
};
onBeforeUnmount(cancelPreviewHide);

const chipFrom = (target: EventTarget | null): HTMLElement | null => {
  const el = target instanceof Element ? target.closest('button.ud-cite') : null;
  return el instanceof HTMLElement && contentEl.value?.contains(el) ? el : null;
};
const onContentPointerOver = (e: Event) => {
  const chip = chipFrom(e.target);
  if (!chip) return;
  cancelPreviewHide();
  if (preview.value?.el === chip) return;
  preview.value = { el: chip, token: chip.dataset.token || '', n: Number(chip.dataset.n) || 0 };
};
const onContentPointerOut = (e: Event) => {
  const chip = chipFrom(e.target);
  if (!chip) return;
  const next = (e as MouseEvent | FocusEvent).relatedTarget;
  if (next instanceof Node && chip.contains(next)) return;
  schedulePreviewHide();
};
const onContentClick = (e: MouseEvent) => {
  const chip = chipFrom(e.target);
  if (!chip) return;
  e.preventDefault();
  preview.value = null;
  openCitation(chip.dataset.token || '');
};
// A re-render (a status arriving, more text streaming) replaces the chip
// elements, which would leave the popover pinned to a detached node.
watch(parsedContent, () => { preview.value = null; });

// What the popover shows. It outlives `preview` so the content does not
// collapse while the popover fades out.
const previewShown = ref<{ el: HTMLElement; token: string; n: number } | null>(null);
watch(preview, (p) => { if (p) previewShown.value = p; });
const previewItem = computed(() =>
  (previewShown.value ? citationItemFor(previewShown.value.token) : undefined));
const previewStatus = computed(() => citationStatusInfo(previewItem.value));
const previewSnippet = computed(() => {
  const text = (previewItem.value?.snippet || '').trim();
  return text.length > 280 ? `${text.slice(0, 280).trimEnd()}…` : text;
});

// The viewer drawer, mounted on first use.
const viewerToken = ref<string | null>(null);
const viewerOpen = ref(false);
const viewerItem = computed(() => (viewerToken.value ? citationItemFor(viewerToken.value) ?? null : null));
const viewerN = computed(() => (viewerToken.value ? citeNumbers.value.get(viewerToken.value) : undefined));
function openCitation(token: string) {
  if (!token) return;
  preview.value = null;
  viewerToken.value = token;
  viewerOpen.value = true;
}

// "First step: 5.6s | First token: 12.7s | Total: 13.2s" — see utils/latencyLabels.
const latency = computed(() => latencySummary(props.message.latency));

// Spelled out on hover, because "~" alone doesn't say what is approximate.
const latencyTitle = computed(() =>
  latency.value?.approximate
    ? 'Approximate — this turn ran before Cremind recorded its own timings, '
      + 'so the total is the gap between the two stored messages.'
    : 'Time from sending the message to each milestone of the reply.',
);

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

// Image chips load through an authenticated fetch: the server accepts only a
// Bearer header, which a bare <img src> cannot send (it was a 401 every time).
const thumbs = useAuthedBlobUrls(() =>
  fileAttachments.value.filter(f => isImageMime(f.mimeType)).map(f => resolveFileUrl(f.uri)),
);
const thumbSrc = (file: FileAttachment): string | null => thumbs.urlFor(resolveFileUrl(file.uri));
const showThumb = (file: FileAttachment): boolean =>
  isImageMime(file.mimeType)
  && !failedThumbs.value.has(file.uri)
  && !thumbs.failed(resolveFileUrl(file.uri));

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

      <!-- Main text content. The listeners serve the citation chips inside
           the v-html (hover preview, click to open); they ignore anything else. -->
      <div
        v-if="message.content"
        ref="contentEl"
        class="text-content marked-content"
        v-html="parsedContent"
        v-link-blank
        @mouseover="onContentPointerOver"
        @mouseout="onContentPointerOut"
        @focusin="onContentPointerOver"
        @focusout="onContentPointerOut"
        @click="onContentClick"
      ></div>

      <!-- Streaming cursor -->
      <span v-if="message.isStreaming" class="streaming-cursor">▊</span>

      <!-- The documents this answer cites, numbered like its chips. -->
      <CitationSourcesFooter
        v-if="hasCitations && !message.isStreaming"
        :sources="citationSources"
        @open="openCitation"
      />

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
      <div v-if="latency && !isUser" class="latency-info" :title="latencyTitle">
        {{ latency.text }}
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
          <div v-for="(file, fIdx) in fileAttachments" :key="fIdx" class="file-chip" :class="{ 'file-chip-image': showThumb(file) }">
            <!-- Image thumbnail (falls back to a file icon if the image is gone) -->
            <template v-if="showThumb(file)">
              <img v-if="thumbSrc(file)" :src="thumbSrc(file)!" :alt="file.name" class="file-chip-thumb" @error="failedThumbs.add(file.uri)" />
              <div v-else class="file-chip-thumb file-chip-thumb-loading" aria-hidden="true"></div>
            </template>
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

      <!-- Citation hover preview: one popover for all of this bubble's chips. -->
      <ElPopover
        v-if="hasCitations"
        :visible="preview !== null"
        :virtual-ref="previewShown?.el"
        virtual-triggering
        placement="top"
        :width="340"
        popper-class="ud-cite-popover"
      >
        <div
          v-if="previewShown"
          class="ud-cite-preview"
          @mouseenter="cancelPreviewHide"
          @mouseleave="schedulePreviewHide"
        >
          <div class="ud-cite-preview-head">
            <span class="ud-cite-preview-n" :class="`tone-${previewStatus.tone}`">{{ previewShown.n }}</span>
            <span class="ud-cite-preview-name">
              {{ previewItem?.file?.name || previewItem?.file?.rel_path || 'Source' }}
            </span>
            <span v-if="previewItem?.locator_label" class="ud-cite-preview-loc">
              {{ previewItem.locator_label }}
            </span>
          </div>
          <p v-if="previewSnippet" class="ud-cite-preview-snippet">{{ previewSnippet }}</p>
          <div class="ud-cite-preview-status" :class="`tone-${previewStatus.tone}`">
            <strong>{{ previewStatus.label }}</strong>
            <span v-if="previewStatus.note"> — {{ previewStatus.note }}</span>
          </div>
          <div v-if="previewStatus.quoteNote" class="ud-cite-preview-status tone-danger">
            <strong>Quote mismatch</strong> — {{ previewStatus.quoteNote }}
          </div>
          <button type="button" class="ud-cite-preview-open" @click="openCitation(previewShown.token)">
            <Icon icon="mdi:open-in-app" /> Open source
          </button>
        </div>
      </ElPopover>

      <CitationViewerDrawer
        v-if="viewerToken"
        v-model="viewerOpen"
        :token="viewerToken"
        :n="viewerN"
        :item="viewerItem"
      />
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

/* Placeholder while an image chip's authenticated fetch is in flight. */
.file-chip-thumb-loading {
  background: linear-gradient(90deg, var(--surface-hover), var(--surface-color), var(--surface-hover));
  background-size: 200% 100%;
  animation: thumbShimmer 1.2s ease-in-out infinite;
}
@keyframes thumbShimmer {
  from { background-position: 100% 0; }
  to { background-position: -100% 0; }
}

/* ── Citation chips (rendered by utils/citations.ts inside the v-html) ──
   Colours come from the app's tokens, mixed down: Element Plus's tag/button
   colour variants would render their light theme in dark mode. */
.message-content :deep(.ud-cite) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 1.45em;
  height: 1.45em;
  padding: 0 0.35em;
  margin: 0 1px;
  vertical-align: 0.35em;
  border-radius: 0.75em;
  border: 1px solid color-mix(in srgb, var(--primary-color) 35%, transparent);
  background: color-mix(in srgb, var(--primary-color) 12%, transparent);
  color: var(--primary-color);
  font-family: inherit;
  font-size: 0.7em;
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
  transition: background 0.12s ease;
}
.message-content :deep(.ud-cite:hover),
.message-content :deep(.ud-cite:focus-visible) {
  outline: none;
  background: color-mix(in srgb, var(--primary-color) 24%, transparent);
}
/* Not verified yet (streaming, or resolving). */
.message-content :deep(.ud-cite[data-status="pending"]) {
  border-color: color-mix(in srgb, var(--text-secondary) 30%, transparent);
  background: color-mix(in srgb, var(--text-secondary) 10%, transparent);
  color: var(--text-secondary);
}
/* Unverified, or the file changed since: readable, but flagged. */
.message-content :deep(.ud-cite[data-status="unissued"]),
.message-content :deep(.ud-cite[data-status="stale"]) {
  border-color: color-mix(in srgb, var(--warning-color) 50%, transparent);
  background: color-mix(in srgb, var(--warning-color) 16%, transparent);
  color: var(--warning-color);
}
/* Points at nothing (any more). */
.message-content :deep(.ud-cite[data-status="removed"]),
.message-content :deep(.ud-cite[data-status="invalid"]) {
  border-color: color-mix(in srgb, var(--text-tertiary) 35%, transparent);
  background: color-mix(in srgb, var(--text-tertiary) 10%, transparent);
  color: var(--text-tertiary);
  text-decoration: line-through;
}
/* The quoted words beside the token are not in the source. */
.message-content :deep(.ud-cite[data-quote="mismatch"]) {
  box-shadow: 0 0 0 1.5px var(--danger-color);
}
</style>

<!-- Non-scoped: the ElPopover's own wrapper (popper-class) is teleported to
     <body> and belongs to Element Plus, not to this component. -->
<style>
.ud-cite-popover.el-popover.el-popper {
  padding: 10px 12px;
}

.ud-cite-preview {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.8rem;
  color: var(--text-primary);
}
.ud-cite-preview-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.ud-cite-preview-n {
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.68rem;
  font-weight: 700;
  color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 14%, transparent);
}
.ud-cite-preview-n.tone-warn {
  color: var(--warning-color);
  background: color-mix(in srgb, var(--warning-color) 16%, transparent);
}
.ud-cite-preview-n.tone-gone,
.ud-cite-preview-n.tone-pending {
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--text-secondary) 12%, transparent);
}
.ud-cite-preview-n.tone-gone { text-decoration: line-through; }
.ud-cite-preview-name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ud-cite-preview-loc {
  flex-shrink: 0;
  margin-left: auto;
  padding: 0 6px;
  border-radius: 8px;
  font-size: 0.72rem;
  background: var(--surface-hover);
  color: var(--text-secondary);
}
.ud-cite-preview-snippet {
  margin: 0;
  padding: 6px 8px;
  border-left: 3px solid var(--border-color);
  background: var(--surface-hover);
  border-radius: 0 6px 6px 0;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
}
.ud-cite-preview-status {
  font-size: 0.75rem;
  line-height: 1.35;
  color: var(--text-secondary);
}
.ud-cite-preview-status.tone-ok strong { color: var(--success-color); }
.ud-cite-preview-status.tone-warn strong { color: var(--warning-color); }
.ud-cite-preview-status.tone-danger strong { color: var(--danger-color); }
.ud-cite-preview-open {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--surface-color);
  color: var(--text-primary);
  font: inherit;
  font-size: 0.75rem;
  cursor: pointer;
}
.ud-cite-preview-open:hover {
  border-color: var(--primary-color);
  color: var(--primary-color);
}
</style>
