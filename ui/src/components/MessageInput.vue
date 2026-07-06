<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { ElPopover, ElNotification } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useChatStore } from '../stores/chat';
import { fetchSystemVars, fetchAgentNames } from '../services/configApi';
import { uploadTempFiles } from '../services/filesApi';
import { CHAT_MODES, chatModeMeta, type ChatMode } from '../constants/chatModes';
import MentionMenu, { type MentionItem } from './MentionMenu.vue';

export interface Attachment {
  name: string;
  path: string;
}

const props = withDefaults(defineProps<{
  disabled?: boolean;
  isProcessing?: boolean;
  mode?: ChatMode;
  // Hide the mode selector (e.g. inside the event-run drawer, where plan mode
  // is meaningless).
  showModeSelector?: boolean;
  // Set when embedded (event-run drawer): uploads target this run conversation
  // instead of ensuring/creating the active one.
  conversationId?: string | null;
}>(), {
  mode: 'reasoning',
  showModeSelector: true,
});

const emit = defineEmits<{
  send: [payload: { text: string; attachments: Attachment[] }];
  stop: [];
  'update:mode': [mode: ChatMode];
}>();

const settingsStore = useSettingsStore();
const chatStore = useChatStore();

const inputText = ref('');
const popoverVisible = ref(false);
const taRef = ref<HTMLTextAreaElement | null>(null);
const layerRef = ref<HTMLDivElement | null>(null);

// ── file upload (composer attachments) ──
const fileInputRef = ref<HTMLInputElement | null>(null);
const attachments = ref<Attachment[]>([]);
const uploading = ref(false);

const triggerUpload = () => {
  if (props.disabled || props.isProcessing || uploading.value) return;
  fileInputRef.value?.click();
};

const onFilesPicked = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  const files = input.files ? Array.from(input.files) : [];
  // Reset the input value so picking the same file again re-fires `change`.
  input.value = '';
  if (!files.length) return;

  // Gate send for the WHOLE provision+upload window. Set before awaiting
  // ensureConversation so a quick Enter can't (a) send a message before the
  // attachment is pushed (dropping it), or (b) create a second conversation
  // while the first is still being provisioned for the upload.
  uploading.value = true;
  try {
    // Files must land in a real conversation's temp dir. When embedded, upload
    // to the given run conversation; otherwise ensure the active one exists
    // (created upfront for a brand-new chat) before uploading.
    const cid = props.conversationId ?? await chatStore.ensureConversation();
    if (!cid) {
      ElNotification({ title: 'Upload failed', message: 'No active conversation', type: 'error' });
      return;
    }
    const results = await uploadTempFiles(
      settingsStore.agentUrl, settingsStore.authToken, cid, files,
    );
    for (const r of results) {
      if (r.status === 'error' || !r.path) {
        ElNotification({
          title: 'Upload failed',
          message: r.error ? `${r.name}: ${r.error}` : `Failed to upload ${r.name}`,
          type: 'error',
        });
        continue;
      }
      attachments.value.push({ name: r.saved_as || r.name, path: r.path });
    }
  } catch (e: any) {
    ElNotification({
      title: 'Upload failed',
      message: e?.message || 'Failed to upload files',
      type: 'error',
    });
  } finally {
    uploading.value = false;
  }
};

const removeAttachment = (index: number) => {
  attachments.value.splice(index, 1);
};

const MIN_HEIGHT_PX = 112;  // tall enough for the reasoning/upload/send button column
const MAX_HEIGHT_PX = 192;  // ≈ 8 rows

// Mention/system-var autocomplete state
type Trigger = '$' | '@';
const triggerKind = ref<Trigger | null>(null);
const triggerStart = ref(-1);
// When the menu is opened by clicking an existing token, the entire token
// range (start..end) is replaced on selection. When opened by typing a fresh
// `$`/`@`, this is null and we replace from triggerStart..caret instead.
const replaceRange = ref<{ start: number; end: number } | null>(null);
const items = ref<MentionItem[]>([]);
const activeIndex = ref(0);
const menuPos = ref({ top: 0, left: 0 });

// Lists that drive the menu contents. `sysVars` also drives the known-vs-unknown
// styling for `$VAR` tokens in the highlight layer; `agents` (one per profile)
// only feeds the `@` menu — picking one inserts the bare name as plain text, so
// there is no `@` token to highlight.
const sysVars = ref<MentionItem[]>([]);
const agents = ref<MentionItem[]>([]);
const sysVarSet = computed(() => new Set(sysVars.value.map(v => v.name)));
// Until the lists arrive, treat tokens as known so we don't briefly flash
// every chip as "unknown" on first paint.
const listsLoaded = ref(false);

const menuVisible = computed(() => triggerKind.value !== null);

// ── highlight segmentation ──
// Splits inputText into a flat sequence of plain-text and `$VAR` token segments
// using the same regex shape the backend uses to resolve system vars. Tokens
// flagged `known: false` are styled as warnings — covers both never-existed
// names and names that have since been removed from the system. (`@` agent
// names are inserted as plain text, so they are never tokenized here.)
type Segment =
  | { kind: 'text'; value: string }
  | { kind: 'sys'; raw: string; name: string; known: boolean };

const TOKEN_RE = /(\$[A-Z][A-Z0-9_]*)/g;

const segments = computed<Segment[]>(() => {
  const text = inputText.value;
  const out: Segment[] = [];
  let lastIndex = 0;
  TOKEN_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = TOKEN_RE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      out.push({ kind: 'text', value: text.slice(lastIndex, match.index) });
    }
    const name = match[1].slice(1);
    out.push({
      kind: 'sys',
      raw: match[0],
      name,
      known: !listsLoaded.value || sysVarSet.value.has(name),
    });
    lastIndex = TOKEN_RE.lastIndex;
  }
  if (lastIndex < text.length) {
    out.push({ kind: 'text', value: text.slice(lastIndex) });
  }
  return out;
});

const closeMenu = () => {
  triggerKind.value = null;
  triggerStart.value = -1;
  replaceRange.value = null;
  items.value = [];
  activeIndex.value = 0;
};

// ── caret coordinates (mirror trick) ──
const MIRROR_PROPS = [
  'boxSizing', 'width', 'height', 'overflowX', 'overflowY',
  'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
  'borderStyle',
  'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
  'fontStyle', 'fontVariant', 'fontWeight', 'fontStretch', 'fontSize',
  'fontSizeAdjust', 'lineHeight', 'fontFamily',
  'textAlign', 'textTransform', 'textIndent', 'textDecoration',
  'letterSpacing', 'wordSpacing', 'tabSize',
] as const;

const computeCaretCoords = (
  ta: HTMLTextAreaElement, caretIndex: number,
): { top: number; left: number } => {
  const div = document.createElement('div');
  const style = div.style;
  const computed = window.getComputedStyle(ta);

  style.position = 'absolute';
  style.visibility = 'hidden';
  style.whiteSpace = 'pre-wrap';
  style.wordWrap = 'break-word';
  style.top = '0';
  style.left = '-9999px';
  for (const prop of MIRROR_PROPS) {
    style[prop as any] = computed[prop as any];
  }

  div.textContent = ta.value.substring(0, caretIndex);
  const span = document.createElement('span');
  span.textContent = ta.value.substring(caretIndex) || '.';
  div.appendChild(span);

  document.body.appendChild(div);
  const caretTop = span.offsetTop - ta.scrollTop;
  const caretLeft = span.offsetLeft - ta.scrollLeft;
  document.body.removeChild(div);

  const rect = ta.getBoundingClientRect();
  return { top: rect.top + caretTop, left: rect.left + caretLeft };
};

const updateMenuPosition = () => {
  const ta = taRef.value;
  if (!ta || triggerStart.value < 0) return;
  menuPos.value = computeCaretCoords(ta, triggerStart.value);
};

// ── autosize + scroll sync ──
const adjustHeight = () => {
  const ta = taRef.value;
  if (!ta) return;
  ta.style.height = 'auto';
  const next = Math.max(MIN_HEIGHT_PX, Math.min(ta.scrollHeight, MAX_HEIGHT_PX));
  ta.style.height = `${next}px`;
};

const syncScroll = () => {
  const ta = taRef.value;
  const layer = layerRef.value;
  if (!ta || !layer) return;
  layer.scrollTop = ta.scrollTop;
  layer.scrollLeft = ta.scrollLeft;
};

watch(inputText, () => {
  nextTick(() => {
    adjustHeight();
    syncScroll();
  });
});

onMounted(async () => {
  adjustHeight();
  // Pre-fetch so the highlight layer can start marking unknown tokens
  // immediately. Failures are silent; tokens stay flagged as "known"
  // (no warning style) until a successful fetch.
  try {
    const [sv, ag] = await Promise.all([
      fetchSystemVars(settingsStore.agentUrl, settingsStore.authToken),
      fetchAgentNames(settingsStore.agentUrl, settingsStore.authToken),
    ]);
    sysVars.value = sv.map(v => ({
      name: v.name,
      description: v.description,
      value: v.value,
      secret: v.secret,
    }));
    agents.value = ag.agents.map(a => ({ name: a.name, profile: a.profile }));
  } catch {
    // leave lists empty; listsLoaded stays false so no chip flashes red.
    return;
  }
  listsLoaded.value = true;
});

const itemsForKind = (kind: Trigger): MentionItem[] =>
  kind === '$' ? sysVars.value : agents.value;

// Glyph the menu shows before each name: `$` keeps its prefix; agent names
// (`@` trigger) show bare, since the `@` is dropped on selection.
const menuPrefix = computed(() => (triggerKind.value === '@' ? '' : '$'));

// ── menu open/insert ──
const openMenu = (
  kind: Trigger,
  triggerCharIndex: number,
  range: { start: number; end: number } | null,
) => {
  triggerKind.value = kind;
  triggerStart.value = triggerCharIndex;
  replaceRange.value = range;
  activeIndex.value = 0;
  items.value = itemsForKind(kind);
  updateMenuPosition();
};

const insertSelection = (item: MentionItem) => {
  const ta = taRef.value;
  if (!ta || triggerStart.value < 0 || !triggerKind.value) return;
  const value = inputText.value;
  const caret = ta.selectionStart ?? value.length;
  const start = replaceRange.value ? replaceRange.value.start : triggerStart.value;
  const end = replaceRange.value ? replaceRange.value.end : caret;
  // `$` vars keep their sigil; agent names (`@`) are inserted bare — the `@`
  // trigger is consumed so the composer shows just the name as plain text.
  const insert = triggerKind.value === '@' ? item.name : `${triggerKind.value}${item.name}`;
  inputText.value = value.slice(0, start) + insert + value.slice(end);
  closeMenu();
  nextTick(() => {
    const t = taRef.value;
    if (!t) return;
    const pos = start + insert.length;
    t.focus();
    t.setSelectionRange(pos, pos);
  });
};

// ── input / typing detection ──
const handleInput = () => {
  const ta = taRef.value;
  if (!ta) return;
  const value = ta.value;
  inputText.value = value;
  const caret = ta.selectionStart ?? value.length;

  // Click-to-edit menu: any input event collapses it, since the user is
  // now typing instead of picking from the list.
  if (replaceRange.value !== null) {
    closeMenu();
    return;
  }

  // Cancel an open typing-mode menu if the user backspaced past the trigger.
  if (triggerKind.value !== null && caret <= triggerStart.value) {
    closeMenu();
    return;
  }

  // Detect a freshly typed trigger character.
  if (caret > 0) {
    const ch = value[caret - 1];
    if (ch === '$' || ch === '@') {
      const prev = caret >= 2 ? value[caret - 2] : '';
      if (caret === 1 || /\s/.test(prev)) {
        openMenu(ch as Trigger, caret - 1, null);
        return;
      }
    }
  }

  if (triggerKind.value !== null) updateMenuPosition();
};

// ── click-to-edit on existing tokens ──
const findTokenAt = (
  text: string, caret: number,
): { kind: Trigger; start: number; end: number } | null => {
  const re = new RegExp(TOKEN_RE.source, 'g');
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const start = m.index;
    const end = start + m[0].length;
    // Caret just after the trigger char up to and including the token end
    // counts as "inside the token" — clicking the very first character
    // (before `$`/`@`) does not open the menu.
    if (caret > start && caret <= end) {
      return { kind: m[0][0] as Trigger, start, end };
    }
  }
  return null;
};

const handleClickInTextarea = () => {
  // Defer one tick so selectionStart reflects the post-click caret.
  nextTick(() => {
    const ta = taRef.value;
    if (!ta) return;
    const caret = ta.selectionStart ?? 0;
    const hit = findTokenAt(inputText.value, caret);
    if (!hit) {
      if (replaceRange.value !== null) closeMenu();
      return;
    }
    openMenu(hit.kind, hit.start, { start: hit.start, end: hit.end });
  });
};

// ── send / keyboard ──
const handleClick = () => {
  if (props.isProcessing) {
    emit('stop');
    return;
  }
  // Don't send while an attachment is still provisioning/uploading — its path
  // isn't in `attachments` yet, so the message would go without it.
  if (uploading.value) return;
  if (inputText.value.trim() && !props.disabled) {
    emit('send', { text: inputText.value, attachments: [...attachments.value] });
    inputText.value = '';
    attachments.value = [];
    closeMenu();
    nextTick(adjustHeight);
  }
};

const handleKeydown = (event: KeyboardEvent) => {
  if (menuVisible.value && items.value.length > 0) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      activeIndex.value = (activeIndex.value + 1) % items.value.length;
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      activeIndex.value = (activeIndex.value - 1 + items.value.length) % items.value.length;
      return;
    }
    if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault();
      insertSelection(items.value[activeIndex.value]);
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      closeMenu();
      return;
    }
  }

  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    if (!props.isProcessing) handleClick();
  }
};

const handleBlur = () => {
  setTimeout(() => {
    if (document.activeElement !== taRef.value) closeMenu();
  }, 0);
};

const activeModeMeta = computed(() => chatModeMeta(props.mode));

const selectMode = (mode: ChatMode) => {
  emit('update:mode', mode);
  popoverVisible.value = false;
};
</script>

<template>
  <div class="message-input-container">
    <div class="input-wrapper">
      <input
        ref="fileInputRef"
        type="file"
        multiple
        class="hidden-file-input"
        @change="onFilesPicked"
      />
      <div v-if="attachments.length" class="attachment-chips">
        <span
          v-for="(att, i) in attachments"
          :key="i"
          class="attachment-chip"
          :title="att.name"
        >
          <Icon icon="mdi:paperclip" class="chip-icon" />
          <span class="chip-name">{{ att.name }}</span>
          <button
            type="button"
            class="chip-remove"
            title="Remove"
            @click="removeAttachment(i)"
          >
            <Icon icon="mdi:close" />
          </button>
        </span>
      </div>
      <div class="composer" :class="{ disabled }">
        <div class="hl-layer" ref="layerRef" aria-hidden="true">
          <template v-for="(seg, i) in segments" :key="i">
            <span v-if="seg.kind === 'text'" class="hl-text">{{ seg.value }}</span>
            <span
              v-else
              :class="[
                'hl-token',
                `hl-token--${seg.kind}`,
                { 'hl-token--unknown': !seg.known },
              ]"
            >{{ seg.raw }}</span>
          </template>
          <!-- trailing newline guard so a final '\n' is rendered as one extra line -->
          <span class="hl-text">{{ '​' }}</span>
        </div>
        <textarea
          ref="taRef"
          class="composer-input"
          :value="inputText"
          placeholder="Type your message... (Enter to send, Shift+Enter for new line)"
          :disabled="disabled"
          rows="3"
          spellcheck="true"
          @input="handleInput"
          @keydown="handleKeydown"
          @click="handleClickInTextarea"
          @scroll="syncScroll"
          @blur="handleBlur"
        />
      </div>
      <MentionMenu
        :visible="menuVisible"
        :items="items"
        :top="menuPos.top"
        :left="menuPos.left"
        :active-index="activeIndex"
        :prefix="menuPrefix"
        @select="insertSelection"
        @update:active-index="activeIndex = $event"
      />
      <ElPopover
        v-if="showModeSelector"
        :visible="popoverVisible"
        placement="top"
        :width="280"
        popper-class="mode-popover"
        @update:visible="popoverVisible = $event"
      >
        <template #reference>
          <button
            class="mode-toggle-button"
            :class="activeModeMeta.buttonClass"
            :title="activeModeMeta.label"
            @click="popoverVisible = !popoverVisible"
            type="button"
          >
            <Icon :icon="activeModeMeta.icon" />
          </button>
        </template>
        <div class="mode-menu" role="menu">
          <button
            v-for="m in CHAT_MODES"
            :key="m.id"
            type="button"
            role="menuitemradio"
            :aria-checked="m.id === mode"
            class="mode-item"
            :class="{ active: m.id === mode }"
            @click="selectMode(m.id)"
          >
            <Icon :icon="m.icon" class="mode-item-icon" />
            <span class="mode-item-text">
              <span class="mode-item-label">{{ m.label }}</span>
              <span class="mode-item-desc">{{ m.description }}</span>
            </span>
            <Icon v-if="m.id === mode" icon="mdi:check" class="mode-item-check" />
          </button>
        </div>
      </ElPopover>
      <button
        @click="triggerUpload"
        :disabled="disabled || isProcessing || uploading"
        class="upload-button"
        :class="{ disabled: disabled || isProcessing || uploading }"
        type="button"
        :title="uploading ? 'Uploading…' : 'Attach files'"
      >
        <Icon :icon="uploading ? 'mdi:loading' : 'mdi:paperclip'" :class="{ spin: uploading }" />
      </button>
      <button
        @click="handleClick"
        :disabled="!isProcessing && (disabled || uploading || !inputText.trim())"
        class="send-button"
        :class="{
          'disabled': !isProcessing && (disabled || uploading || !inputText.trim()),
          'stop': isProcessing,
        }"
        :title="isProcessing ? 'Stop' : 'Send'"
      >
        <Icon :icon="isProcessing ? 'mdi:stop' : 'mdi:send'" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.message-input-container {
  padding: 12px 16px;
  background: var(--surface-color);
  border-top: 1px solid var(--border-color);
  position: relative;
  flex-shrink: 0;
  max-height: 40vh;
  overflow-y: auto;
}

.input-wrapper {
  position: relative;
}

/* Composer: wrapper that stacks a transparent textarea over a syntax-
   highlighted layer. Both share identical font / padding / line-height so
   character positions line up exactly, frame for frame. */
.composer {
  position: relative;
  width: 100%;
}

.composer.disabled {
  opacity: 0.6;
}

/* Shared text-shape rules — every property that affects glyph positioning
   must match between layer and textarea. */
.hl-layer,
.composer-input {
  box-sizing: border-box;
  font-family: inherit;
  font-size: 0.95em;
  line-height: 1.6;
  /* Right room for the reasoning/upload/send button stack. Layer + textarea
     share this rule so the caret never drifts. */
  padding: 10px 44px 10px 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: break-word;
  letter-spacing: normal;
  word-spacing: normal;
  tab-size: 4;
}

.hl-layer {
  position: absolute;
  inset: 0;
  pointer-events: none;
  color: var(--text-primary);
  background: var(--surface-color);
  z-index: 1;
  overflow: hidden;
  /* Preserve newlines and avoid mid-word breaking the same way as the textarea */
}

.composer-input {
  position: relative;
  display: block;
  width: 100%;
  min-height: 112px;
  max-height: 192px;
  background: transparent;
  color: transparent;
  caret-color: var(--text-primary);
  resize: none;
  outline: none;
  z-index: 2;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.composer-input::placeholder {
  color: var(--text-tertiary);
}

.composer-input:focus {
  border-color: var(--primary-color);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}

.composer-input:disabled {
  cursor: not-allowed;
  background: var(--surface-hover);
}

/* Selection highlight in the transparent textarea — without this the
   selection background shows but with a transparent foreground; this keeps
   the selected text faintly visible against the highlighted layer. */
.composer-input::selection {
  background: rgba(37, 99, 235, 0.25);
  color: transparent;
}

.hl-text {
  white-space: pre-wrap;
}

/* Tokens are highlighted with background+color only — no padding, margin,
   border, or font-weight change — so each character occupies exactly the
   same horizontal space as in the textarea. Otherwise the textarea's
   caret drifts away from the overlay's glyph positions, especially after
   a token. */
.hl-token {
  display: inline;
  border-radius: 2px;
  pointer-events: none;
}

.hl-token--sys {
  background: rgba(37, 99, 235, 0.14);
  color: var(--primary-color);
}

.hl-token--unknown {
  background: rgba(239, 68, 68, 0.14);
  color: #dc2626;
  text-decoration: line-through;
  text-decoration-color: rgba(239, 68, 68, 0.6);
}

.mode-toggle-button {
  position: absolute;
  bottom: 74px;
  right: 10px;
  width: 28px;
  height: 28px;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  color: var(--text-tertiary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  transition: all 0.2s ease;
  padding: 0;
  z-index: 3;
}

.mode-toggle-button:hover {
  border-color: var(--primary-color);
  color: var(--primary-color);
}

/* Active-mode appearance (icon + tint) so the mode reads without opening. */
.mode-toggle-button.mode-reasoning {
  color: var(--primary-color);
  border-color: var(--primary-color);
  background: rgba(37, 99, 235, 0.08);
}

.mode-toggle-button.mode-plan {
  color: var(--warning-color);
  border-color: var(--warning-color);
  background: rgba(245, 158, 11, 0.10);
}

.mode-toggle-button.mode-instant {
  color: var(--text-tertiary);
  border-color: var(--border-color);
  background: transparent;
}

.send-button {
  position: absolute;
  bottom: 10px;
  right: 10px;
  width: 28px;
  height: 28px;
  background: var(--primary-color);
  border: none;
  border-radius: 6px;
  color: white;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  transition: all 0.2s ease;
  padding: 0;
  z-index: 3;
}

.send-button:hover:not(:disabled) {
  background: var(--primary-light);
}

.send-button:active:not(:disabled) {
  transform: scale(0.95);
}

.send-button.disabled,
.send-button:disabled {
  background: var(--text-tertiary);
  cursor: not-allowed;
  opacity: 0.4;
}

.send-button.stop {
  background: #ef4444;
  cursor: pointer;
  opacity: 1;
}

.send-button.stop:hover {
  background: #dc2626;
}

/* Upload button — sits in the right-side stack between the reasoning toggle
   (above) and the send button (below). */
.hidden-file-input {
  display: none;
}

.upload-button {
  position: absolute;
  bottom: 42px;
  right: 10px;
  width: 28px;
  height: 28px;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  color: var(--text-tertiary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  transition: all 0.2s ease;
  padding: 0;
  z-index: 3;
}

.upload-button:hover:not(:disabled) {
  border-color: var(--primary-color);
  color: var(--primary-color);
}

.upload-button.disabled,
.upload-button:disabled {
  cursor: not-allowed;
  opacity: 0.4;
}

.spin {
  animation: upload-spin 0.8s linear infinite;
}

@keyframes upload-spin {
  to { transform: rotate(360deg); }
}

/* Attachment chips above the composer. */
.attachment-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.attachment-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 220px;
  padding: 3px 6px 3px 8px;
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-primary);
}

.chip-icon {
  flex-shrink: 0;
  color: var(--text-tertiary);
}

.chip-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chip-remove {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  padding: 0;
  background: transparent;
  border: none;
  border-radius: 3px;
  color: var(--text-tertiary);
  cursor: pointer;
}

.chip-remove:hover {
  color: var(--text-primary);
  background: var(--border-color);
}
</style>

<!-- Non-scoped: the ElPopover content is teleported to <body>, so scoped
     styles would not reach the mode-menu rows. -->
<style>
.mode-popover.el-popover.el-popper {
  padding: 6px;
}

.mode-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.mode-menu .mode-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  background: transparent;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
  color: var(--text-primary);
  transition: background 0.15s ease;
}

.mode-menu .mode-item:hover {
  background: rgba(37, 99, 235, 0.08);
}

.mode-menu .mode-item.active {
  background: rgba(37, 99, 235, 0.10);
}

.mode-menu .mode-item-icon {
  flex-shrink: 0;
  font-size: 18px;
  margin-top: 1px;
  color: var(--text-secondary);
}

.mode-menu .mode-item.active .mode-item-icon {
  color: var(--primary-color);
}

.mode-menu .mode-item-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.mode-menu .mode-item-label {
  font-size: 13px;
  font-weight: 600;
}

.mode-menu .mode-item-desc {
  font-size: 11px;
  line-height: 1.35;
  color: var(--text-tertiary);
}

.mode-menu .mode-item-check {
  flex-shrink: 0;
  font-size: 16px;
  color: var(--primary-color);
  margin-top: 1px;
}
</style>
