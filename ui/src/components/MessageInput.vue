<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { ElPopover, ElSwitch } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { fetchSystemVars, listProfiles } from '../services/configApi';
import MentionMenu, { type MentionItem } from './MentionMenu.vue';

const props = withDefaults(defineProps<{
  disabled?: boolean;
  isProcessing?: boolean;
  reasoningEnabled?: boolean;
}>(), {
  reasoningEnabled: true,
});

const emit = defineEmits<{
  send: [text: string];
  stop: [];
  'update:reasoningEnabled': [enabled: boolean];
}>();

const settingsStore = useSettingsStore();

const inputText = ref('');
const popoverVisible = ref(false);
const taRef = ref<HTMLTextAreaElement | null>(null);
const layerRef = ref<HTMLDivElement | null>(null);

const MIN_HEIGHT_PX = 72;   // ≈ 3 rows at 1.6 line-height, 0.95em font
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

// Lists of known names — drive both the menu contents and the
// known-vs-unknown styling in the highlight layer.
const sysVars = ref<MentionItem[]>([]);
const profiles = ref<MentionItem[]>([]);
const sysVarSet = computed(() => new Set(sysVars.value.map(v => v.name)));
const profileSet = computed(() => new Set(profiles.value.map(p => p.name)));
// Until the lists arrive, treat tokens as known so we don't briefly flash
// every chip as "unknown" on first paint.
const listsLoaded = ref(false);

const menuVisible = computed(() => triggerKind.value !== null);

// ── highlight segmentation ──
// Splits inputText into a flat sequence of plain-text and token segments
// using the same regex shapes the backend uses to resolve tokens. Tokens
// flagged `known: false` are styled as warnings — covers both never-existed
// names and names that have since been removed from the system.
type Segment =
  | { kind: 'text'; value: string }
  | { kind: 'sys' | 'profile'; raw: string; name: string; known: boolean };

const TOKEN_RE = /(\$[A-Z][A-Z0-9_]*)|(@[a-z0-9_-]+)/g;

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
    if (match[1]) {
      const name = match[1].slice(1);
      out.push({
        kind: 'sys',
        raw: match[0],
        name,
        known: !listsLoaded.value || sysVarSet.value.has(name),
      });
    } else {
      const name = match[2].slice(1);
      out.push({
        kind: 'profile',
        raw: match[0],
        name,
        known: !listsLoaded.value || profileSet.value.has(name),
      });
    }
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
    const [sv, pr] = await Promise.all([
      fetchSystemVars(settingsStore.agentUrl, settingsStore.authToken),
      listProfiles(settingsStore.agentUrl, settingsStore.authToken),
    ]);
    sysVars.value = sv.map(v => ({ name: v.name, description: v.description }));
    profiles.value = pr.profiles.map(name => ({ name }));
  } catch {
    // leave lists empty; listsLoaded stays false so no chip flashes red.
    return;
  }
  listsLoaded.value = true;
});

const itemsForKind = (kind: Trigger): MentionItem[] =>
  kind === '$' ? sysVars.value : profiles.value;

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
  const insert = `${triggerKind.value}${item.name}`;
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
  if (inputText.value.trim() && !props.disabled) {
    emit('send', inputText.value);
    inputText.value = '';
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

const handleReasoningToggle = (value: boolean | string | number) => {
  emit('update:reasoningEnabled', Boolean(value));
  popoverVisible.value = false;
};
</script>

<template>
  <div class="message-input-container">
    <div class="input-wrapper">
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
        :prefix="triggerKind ?? '$'"
        @select="insertSelection"
      />
      <ElPopover
        :visible="popoverVisible"
        placement="top"
        :width="180"
        @update:visible="popoverVisible = $event"
      >
        <template #reference>
          <button
            class="reasoning-toggle-button"
            :class="{ 'active': reasoningEnabled }"
            @click="popoverVisible = !popoverVisible"
            type="button"
          >
            <Icon icon="mdi:chevron-up" />
          </button>
        </template>
        <div class="reasoning-popover-content">
          <span>Reasoning</span>
          <ElSwitch
            :model-value="reasoningEnabled"
            @update:model-value="handleReasoningToggle"
            size="small"
          />
        </div>
      </ElPopover>
      <button
        @click="handleClick"
        :disabled="!isProcessing && (disabled || !inputText.trim())"
        class="send-button"
        :class="{
          'disabled': !isProcessing && (disabled || !inputText.trim()),
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
  min-height: 72px;
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

.hl-token--profile {
  background: rgba(16, 185, 129, 0.14);
  color: #059669;
}

.hl-token--unknown {
  background: rgba(239, 68, 68, 0.14);
  color: #dc2626;
  text-decoration: line-through;
  text-decoration-color: rgba(239, 68, 68, 0.6);
}

.reasoning-toggle-button {
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

.reasoning-toggle-button:hover {
  border-color: var(--primary-color);
  color: var(--primary-color);
}

.reasoning-toggle-button.active {
  color: var(--primary-color);
  border-color: var(--primary-color);
  background: rgba(37, 99, 235, 0.08);
}

.reasoning-popover-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  color: var(--text-primary);
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
</style>
