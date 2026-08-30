<script setup lang="ts">
// Room composer. Simpler than MessageInput: no attachments, no mode selector,
// no `$VAR` highlight layer — a group post is plain text that several agents
// read. The one shared affordance is the `@` menu, which here lists the room's
// members and KEEPS the `@` on insert: `@handle` is how an agent recognises it
// was addressed, so dropping the sigil (as the chat composer does) would make
// every mention read as an ordinary name.
import { computed, nextTick, ref } from 'vue';
import { Icon } from '@iconify/vue';
import MentionMenu, { type MentionItem } from '../MentionMenu.vue';

const props = withDefaults(defineProps<{
  members: { profile: string; name: string }[];
  disabled?: boolean;
  /** Shown instead of the composer when the viewer may not post. */
  disabledHint?: string;
  sending?: boolean;
}>(), {
  disabled: false,
  disabledHint: '',
  sending: false,
});

const emit = defineEmits<{ send: [text: string] }>();

const inputText = ref('');
const taRef = ref<HTMLTextAreaElement | null>(null);

// Matches the two-party chat's composer, so switching between the two does not
// move the send button up and down the screen.
const MIN_HEIGHT_PX = 112;
const MAX_HEIGHT_PX = 180;

// ── mention menu state ──
const triggerStart = ref(-1);
const activeIndex = ref(0);
const menuPos = ref({ top: 0, left: 0 });

const items = computed<MentionItem[]>(() =>
  props.members.map((m) => ({ name: m.name, profile: m.profile, description: m.profile })),
);

const menuVisible = computed(() => triggerStart.value >= 0 && items.value.length > 0);

const closeMenu = () => {
  triggerStart.value = -1;
  activeIndex.value = 0;
};

// ── caret coordinates (mirror trick, as in MessageInput) ──
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
  const computedStyle = window.getComputedStyle(ta);

  style.position = 'absolute';
  style.visibility = 'hidden';
  style.whiteSpace = 'pre-wrap';
  style.wordWrap = 'break-word';
  style.top = '0';
  style.left = '-9999px';
  for (const prop of MIRROR_PROPS) {
    style[prop as any] = computedStyle[prop as any];
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

const adjustHeight = () => {
  const ta = taRef.value;
  if (!ta) return;
  ta.style.height = 'auto';
  ta.style.height = `${Math.max(MIN_HEIGHT_PX, Math.min(ta.scrollHeight, MAX_HEIGHT_PX))}px`;
};

const insertSelection = (item: MentionItem) => {
  const ta = taRef.value;
  if (!ta || triggerStart.value < 0) return;
  const value = inputText.value;
  const caret = ta.selectionStart ?? value.length;
  const start = triggerStart.value;
  const insert = `@${item.name}`;
  inputText.value = value.slice(0, start) + insert + value.slice(caret);
  closeMenu();
  nextTick(() => {
    const t = taRef.value;
    if (!t) return;
    const pos = start + insert.length;
    t.focus();
    t.setSelectionRange(pos, pos);
    adjustHeight();
  });
};

const handleInput = () => {
  const ta = taRef.value;
  if (!ta) return;
  const value = ta.value;
  inputText.value = value;
  nextTick(adjustHeight);
  const caret = ta.selectionStart ?? value.length;

  // Backspaced past the trigger — the menu no longer describes anything.
  if (triggerStart.value >= 0 && caret <= triggerStart.value) {
    closeMenu();
    return;
  }

  if (caret > 0 && value[caret - 1] === '@') {
    const prev = caret >= 2 ? value[caret - 2] : '';
    if (caret === 1 || /\s/.test(prev)) {
      triggerStart.value = caret - 1;
      activeIndex.value = 0;
      updateMenuPosition();
      return;
    }
  }

  if (triggerStart.value >= 0) updateMenuPosition();
};

const submit = () => {
  const text = inputText.value.trim();
  if (!text || props.disabled || props.sending) return;
  emit('send', inputText.value);
  inputText.value = '';
  closeMenu();
  nextTick(adjustHeight);
};

const handleKeydown = (event: KeyboardEvent) => {
  if (menuVisible.value) {
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
    submit();
  }
};

const handleBlur = () => {
  setTimeout(() => {
    if (document.activeElement !== taRef.value) closeMenu();
  }, 0);
};
</script>

<template>
  <div class="group-composer">
    <div v-if="disabled" class="composer-hint">
      <Icon icon="mdi:lock-outline" />
      <span>{{ disabledHint || 'You cannot post in this group.' }}</span>
    </div>
    <div v-else class="composer-wrapper">
      <textarea
        ref="taRef"
        class="composer-input"
        :value="inputText"
        placeholder="Message the group… (Enter to send, Shift+Enter for a new line, @ to mention a member)"
        rows="2"
        spellcheck="true"
        @input="handleInput"
        @keydown="handleKeydown"
        @blur="handleBlur"
      />
      <button
        type="button"
        class="send-button"
        :disabled="sending || !inputText.trim()"
        :title="sending ? 'Sending…' : 'Send'"
        @click="submit"
      >
        <Icon :icon="sending ? 'mdi:loading' : 'mdi:send'" :class="{ spin: sending }" />
      </button>
      <MentionMenu
        :visible="menuVisible"
        :items="items"
        :top="menuPos.top"
        :left="menuPos.left"
        :active-index="activeIndex"
        prefix="@"
        @select="insertSelection"
        @update:active-index="activeIndex = $event"
      />
    </div>
  </div>
</template>

<style scoped>
.group-composer {
  padding: 12px 16px;
  background: var(--surface-color);
  border-top: 1px solid var(--border-color);
  flex-shrink: 0;
}

.composer-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 4px;
  font-size: 0.85rem;
  color: var(--text-secondary);
}

.composer-wrapper {
  position: relative;
}

.composer-input {
  box-sizing: border-box;
  display: block;
  width: 100%;
  min-height: 112px;
  max-height: 180px;
  padding: 10px 44px 10px 14px;
  font-family: inherit;
  font-size: 0.95em;
  line-height: 1.6;
  color: var(--text-primary);
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  resize: none;
  outline: none;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.composer-input::placeholder {
  color: var(--text-tertiary);
}

.composer-input:focus {
  border-color: var(--primary-color);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}

.send-button {
  position: absolute;
  bottom: 10px;
  right: 10px;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  font-size: 16px;
  color: white;
  background: var(--primary-color);
  border: none;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.send-button:hover:not(:disabled) {
  background: var(--primary-light);
}

.send-button:disabled {
  background: var(--text-tertiary);
  opacity: 0.4;
  cursor: not-allowed;
}

.spin {
  animation: composer-spin 0.8s linear infinite;
}

@keyframes composer-spin {
  to { transform: rotate(360deg); }
}
</style>
