<script setup lang="ts">
/**
 * The composer's "Search tools" control: which of the four search sources the
 * agent may use in this conversation (or this group room), in their fixed
 * priority order.
 *
 * A menu button with a checkbox menu — the WAI-ARIA menu-button pattern:
 * Enter/Space/click opens it and focuses the first row, Arrow keys / Home / End
 * move between rows, Space/Enter toggles, Escape (or Tab) closes it and returns
 * focus to the button. Every toggle saves at once through the search-tools
 * store; there is no Apply.
 *
 * Rows come only from a server answer (`store.viewFor`), so Documentation
 * search is never shown on an assumption — it appears when the server says it
 * is offered here. An unavailable source stays visible, disabled, with its
 * short reason; an AVAILABLE row may carry an informational note instead (a
 * group room: "Availability varies by agent in this room."), which is shown as
 * a note, not as a disabled row.
 */
import { computed, nextTick, ref, useId, watch } from 'vue';
import { ElPopover } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  PENDING_NOTICE,
  searchToolsKey,
  useSearchToolsStore,
  type SearchToolRowView,
  type SearchToolsTarget,
} from '../stores/searchTools';

const props = withDefaults(defineProps<{
  /** `null` renders nothing (an embedded composer with no conversation). */
  target: SearchToolsTarget | null;
  /** A response is in progress: a save applies from the next one. */
  running?: boolean;
  disabled?: boolean;
}>(), {
  running: false,
  disabled: false,
});

// Unique per instance: the main chat and the event-run drawer can both show one.
const menuId = `search-tools-menu-${useId()}`;

const store = useSearchToolsStore();
const settings = useSettingsStore();

const open = ref(false);
const buttonRef = ref<HTMLButtonElement | null>(null);
const panelRef = ref<HTMLElement | null>(null);

const view = computed(() => store.viewFor(props.target));
const targetKey = computed(() => (props.target ? searchToolsKey(props.target) : ''));

// Re-read whenever the control starts showing a different conversation (or
// profile): availability moves with tool settings, and another tab or the CLI
// may have saved since this one last looked. The held state stays on screen
// while the answer is on its way, so nothing flickers.
watch(
  [targetKey, () => settings.profileId, () => settings.authToken],
  () => {
    if (props.target) void store.load(props.target, { force: true });
  },
  { immediate: true },
);

// Close the menu when the control is pointed somewhere else under it.
watch(targetKey, () => { open.value = false; });

const countLabel = computed(() =>
  view.value.loaded ? `${view.value.enabledCount}/${view.value.availableCount}` : '');

const buttonLabel = computed(() => {
  const v = view.value;
  if (!v.loaded) return 'Search tools';
  let text = `Search tools: ${v.enabledCount} of ${v.availableCount} available sources on`;
  if (v.pendingNextResponse) text += '; the saved change applies from the next response';
  return text;
});

const inlineMessage = computed(() => view.value.error || view.value.conflict || '');

function refresh() {
  if (props.target) void store.load(props.target, { force: true });
}

function onToggle(row: SearchToolRowView) {
  if (!props.target || props.disabled || !row.available) return;
  store.toggle(props.target, row.id, { running: props.running });
}

function onReset() {
  if (!props.target || props.disabled || view.value.isDefault) return;
  store.resetToDefaults(props.target, { running: props.running });
}

function dismiss() {
  if (props.target) store.dismissMessages(props.target);
}

function items(): HTMLElement[] {
  return Array.from(panelRef.value?.querySelectorAll<HTMLElement>('[data-st-item]') ?? []);
}

function focusAt(index: number) {
  const list = items();
  if (!list.length) return;
  list[(index + list.length) % list.length].focus();
}

function focusFirst() {
  nextTick(() => {
    const list = items();
    const firstUsable = list.findIndex((el) => el.getAttribute('aria-disabled') !== 'true');
    focusAt(firstUsable >= 0 ? firstUsable : 0);
  });
}

function close(returnFocus: boolean) {
  open.value = false;
  if (returnFocus) nextTick(() => buttonRef.value?.focus());
}

// On the button itself: Element Plus already toggles on Enter/Space; add the
// rest of the menu-button keys — Arrow Down/Up open the menu (the first row
// gets focus once it has rendered), Escape closes it without leaving the
// composer.
function onButtonKeydown(event: KeyboardEvent) {
  if (props.disabled) return;
  if (event.key === 'Escape' && open.value) {
    event.preventDefault();
    event.stopPropagation();
    close(true);
    return;
  }
  if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && !open.value) {
    event.preventDefault();
    open.value = true;
  }
}

function onPanelKeydown(event: KeyboardEvent) {
  const list = items();
  const at = list.indexOf(document.activeElement as HTMLElement);
  switch (event.key) {
    case 'ArrowDown':
      event.preventDefault();
      focusAt(at < 0 ? 0 : at + 1);
      return;
    case 'ArrowUp':
      event.preventDefault();
      focusAt(at < 0 ? list.length - 1 : at - 1);
      return;
    case 'Home':
      event.preventDefault();
      focusAt(0);
      return;
    case 'End':
      event.preventDefault();
      focusAt(list.length - 1);
      return;
    case 'Escape':
      event.preventDefault();
      event.stopPropagation();
      close(true);
      return;
    case 'Tab':
      // The menu is teleported to <body>; letting Tab run on would drop focus
      // at the end of the page instead of back in the composer.
      event.preventDefault();
      close(true);
      return;
    default:
  }
}
</script>

<template>
  <div v-if="target" class="search-tools-control">
    <ElPopover
      v-model:visible="open"
      trigger="click"
      placement="top-start"
      :width="340"
      :disabled="disabled"
      popper-class="search-tools-popover"
      @before-enter="refresh"
      @after-enter="focusFirst"
    >
      <template #reference>
        <button
          ref="buttonRef"
          type="button"
          class="st-button"
          :class="{ 'st-button--pending': view.pendingNextResponse, 'st-button--open': open }"
          :disabled="disabled"
          aria-haspopup="menu"
          :aria-expanded="open ? 'true' : 'false'"
          :aria-controls="menuId"
          :aria-label="buttonLabel"
          :title="view.pendingNextResponse ? 'Search tools — the saved change applies from the next response' : 'Search tools'"
          @keydown="onButtonKeydown"
        >
          <Icon icon="mdi:text-search" class="st-button-icon" aria-hidden="true" />
          <span class="st-button-label">Search tools</span>
          <span v-if="countLabel" class="st-button-count">{{ countLabel }}</span>
          <Icon
            v-if="view.saving"
            icon="mdi:loading"
            class="st-button-busy spin"
            aria-hidden="true"
          />
          <span v-else-if="view.pendingNextResponse" class="st-button-dot" aria-hidden="true" />
        </button>
      </template>

      <div ref="panelRef" class="st-panel" @keydown="onPanelKeydown">
        <div class="st-head">
          <span class="st-title">Search tools</span>
          <span class="st-sub">The agent tries them in this order.</span>
        </div>

        <div
          :id="menuId"
          class="st-menu"
          role="menu"
          aria-label="Search tools"
        >
          <div v-if="!view.loaded" class="st-empty" role="none">
            <template v-if="view.loadError">{{ view.loadError }}</template>
            <template v-else>Loading…</template>
          </div>
          <button
            v-for="row in view.rows"
            :key="row.id"
            type="button"
            role="menuitemcheckbox"
            data-st-item
            class="st-item"
            :class="{ 'st-item--checked': row.checked, 'st-item--unavailable': !row.available }"
            :aria-checked="row.checked ? 'true' : 'false'"
            :aria-disabled="row.available && !disabled ? undefined : 'true'"
            :aria-describedby="`${menuId}-${row.id}-desc`"
            @click="onToggle(row)"
          >
            <Icon
              :icon="row.checked ? 'mdi:checkbox-marked' : 'mdi:checkbox-blank-outline'"
              class="st-check"
              aria-hidden="true"
            />
            <span class="st-rank" aria-hidden="true">{{ row.rank }}</span>
            <span class="st-text">
              <span class="st-label">{{ row.label }}</span>
              <span :id="`${menuId}-${row.id}-desc`" class="st-desc">
                {{ row.description }}
                <span v-if="row.reason" class="st-reason">{{ row.reason }}</span>
                <span v-else-if="row.note" class="st-note">
                  <Icon icon="mdi:information-outline" aria-hidden="true" />
                  {{ row.note }}
                </span>
              </span>
            </span>
          </button>
          <div v-if="view.loaded" class="st-separator" role="separator" />
          <button
            v-if="view.loaded"
            type="button"
            role="menuitem"
            data-st-item
            class="st-reset"
            :aria-disabled="view.isDefault || disabled ? 'true' : undefined"
            @click="onReset"
          >
            <Icon icon="mdi:restore" aria-hidden="true" />
            Reset to defaults
          </button>
        </div>

        <div class="st-messages" aria-live="polite">
          <p v-if="view.conflict" class="st-msg st-msg--info">
            <Icon icon="mdi:account-sync-outline" aria-hidden="true" />
            <span>{{ view.conflict }}</span>
          </p>
          <p v-if="view.error" class="st-msg st-msg--error">
            <Icon icon="mdi:alert-circle-outline" aria-hidden="true" />
            <span>{{ view.error }}</span>
          </p>
          <p v-if="view.showPendingNotice && running" class="st-msg st-msg--info">
            <Icon icon="mdi:timer-sand" aria-hidden="true" />
            <span>{{ PENDING_NOTICE }}</span>
          </p>
          <p v-if="view.cacheWarning" class="st-msg st-msg--warn">
            <Icon icon="mdi:cash-clock" aria-hidden="true" />
            <span>{{ view.cacheWarning }}</span>
          </p>
        </div>
      </div>
    </ElPopover>

    <!-- A save that failed or conflicted while the menu was closed (the
         composer waited on it before sending) still has to be explained. -->
    <span v-if="!open && inlineMessage" class="st-inline" role="status">
      <Icon
        :icon="view.error ? 'mdi:alert-circle-outline' : 'mdi:account-sync-outline'"
        aria-hidden="true"
      />
      <span class="st-inline-text">{{ inlineMessage }}</span>
      <button type="button" class="st-inline-dismiss" aria-label="Dismiss" @click="dismiss">
        <Icon icon="mdi:close" aria-hidden="true" />
      </button>
    </span>
  </div>
</template>

<style scoped>
.search-tools-control {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.st-button {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 8px;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  color: var(--text-secondary);
  font: inherit;
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
  cursor: pointer;
  transition: border-color 0.2s ease, color 0.2s ease, background 0.2s ease;
}

.st-button:hover:not(:disabled),
.st-button--open {
  border-color: var(--primary-color);
  color: var(--primary-color);
}

.st-button:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: 1px;
}

.st-button:disabled {
  cursor: not-allowed;
  opacity: 0.4;
}

.st-button-icon {
  font-size: 15px;
  flex-shrink: 0;
}

.st-button-count {
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
  font-weight: 600;
}

.st-button-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--warning-color);
  flex-shrink: 0;
}

.st-button--pending {
  border-color: var(--warning-color);
}

.st-button-busy {
  font-size: 13px;
}

.st-inline {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  font-size: 12px;
  color: var(--text-secondary);
}

.st-inline-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.st-inline-dismiss {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  background: transparent;
  border: none;
  border-radius: 3px;
  color: var(--text-tertiary);
  cursor: pointer;
}

.st-inline-dismiss:hover {
  color: var(--text-primary);
  background: var(--surface-hover);
}

.st-inline-dismiss:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: 1px;
}

.spin {
  animation: st-spin 0.8s linear infinite;
}

@keyframes st-spin {
  to { transform: rotate(360deg); }
}
</style>

<!-- Non-scoped: the ElPopover content is teleported to <body>, so scoped
     styles would not reach it. Only app tokens (style.css) — an `--el-*` var
     that style.css does not redeclare would stay light in the dark theme. -->
<style>
.search-tools-popover.el-popover.el-popper {
  padding: 6px;
  background: var(--surface-color);
  border-color: var(--border-color);
  color: var(--text-primary);
  max-width: calc(100vw - 24px);
}

.search-tools-popover .st-panel {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.search-tools-popover .st-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px 4px;
}

.search-tools-popover .st-title {
  font-size: 13px;
  font-weight: 600;
}

.search-tools-popover .st-sub {
  font-size: 11px;
  color: var(--text-tertiary);
}

.search-tools-popover .st-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.search-tools-popover .st-empty {
  padding: 8px 10px;
  font-size: 12px;
  color: var(--text-secondary);
}

.search-tools-popover .st-item,
.search-tools-popover .st-reset {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  padding: 7px 10px;
  background: transparent;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
  font: inherit;
  color: var(--text-primary);
  transition: background 0.15s ease;
}

.search-tools-popover .st-item:hover:not([aria-disabled='true']),
.search-tools-popover .st-reset:hover:not([aria-disabled='true']) {
  background: var(--surface-hover);
}

.search-tools-popover .st-item:focus-visible,
.search-tools-popover .st-reset:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: -2px;
}

.search-tools-popover .st-item[aria-disabled='true'],
.search-tools-popover .st-reset[aria-disabled='true'] {
  cursor: not-allowed;
}

.search-tools-popover .st-item--unavailable .st-label,
.search-tools-popover .st-item--unavailable .st-check,
.search-tools-popover .st-reset[aria-disabled='true'] {
  opacity: 0.55;
}

.search-tools-popover .st-check {
  flex-shrink: 0;
  font-size: 18px;
  margin-top: 1px;
  color: var(--text-tertiary);
}

.search-tools-popover .st-item--checked .st-check {
  color: var(--primary-color);
}

.search-tools-popover .st-rank {
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  margin-top: 1px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-color);
  border-radius: 50%;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--text-secondary);
}

.search-tools-popover .st-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.search-tools-popover .st-label {
  font-size: 13px;
  font-weight: 600;
}

.search-tools-popover .st-desc {
  font-size: 11px;
  line-height: 1.4;
  color: var(--text-secondary);
}

.search-tools-popover .st-reason,
.search-tools-popover .st-note {
  display: flex;
  align-items: center;
  gap: 3px;
  margin-top: 2px;
  font-size: 11px;
}

.search-tools-popover .st-reason {
  color: var(--warning-color);
}

.search-tools-popover .st-note {
  color: var(--text-tertiary);
}

.search-tools-popover .st-separator {
  height: 1px;
  margin: 2px 6px;
  background: var(--border-color);
}

.search-tools-popover .st-reset {
  align-items: center;
  font-size: 12px;
  color: var(--text-secondary);
}

.search-tools-popover .st-messages {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.search-tools-popover .st-messages:empty {
  display: none;
}

.search-tools-popover .st-msg {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.4;
  background: var(--surface-hover);
  color: var(--text-secondary);
}

.search-tools-popover .st-msg > svg {
  flex-shrink: 0;
  margin-top: 1px;
  font-size: 13px;
}

.search-tools-popover .st-msg--error {
  color: var(--danger-color);
}

.search-tools-popover .st-msg--warn > svg {
  color: var(--warning-color);
}
</style>
