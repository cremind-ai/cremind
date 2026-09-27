<script setup lang="ts">
/**
 * The file tree's "Indexed content" pane: shown by FileTreePanel in place of
 * the tree when a file's index status button is clicked, and closed with
 * "Back to files" (or Escape), which puts the tree back as it was — the panel
 * keeps the tree mounted underneath and restores its scroll position.
 *
 * For a file in the index it shows `IndexedContent` (summary, passages, file
 * details). For a path that is not — outside the indexed folder, excluded,
 * not indexed yet, removed — it says why, and where that is changed.
 *
 * Focus moves to the pane's title on open; the panel returns it to the status
 * button on Back.
 */
import { computed, nextTick, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import { useIndexStatusStore } from '../../stores/indexStatus';
import { useSettingsStore } from '../../stores/settings';
import { useTerminalPanelStore } from '../../stores/terminalPanel';
import { summarySignature } from '../../utils/indexStatus';
import IndexedContent from './IndexedContent.vue';
import type { IndexedContentTarget } from './indexedContentContext';

const props = defineProps<{ target: IndexedContentTarget }>();
const emit = defineEmits<{ back: [] }>();

const store = useIndexStatusStore();
const settings = useSettingsStore();
const panel = useTerminalPanelStore();
const router = useRouter();

const titleEl = ref<HTMLElement | null>(null);

const live = computed(() => store.statusFor(props.target.path));
// The id the pane opened with, kept while a refresh briefly has no entry —
// but not once the local folder source is off.
const openedFid = live.value?.fid ?? null;
const fid = computed(() => {
  if (live.value) return live.value.fid;
  return store.source.enabled === false ? null : openedFid;
});
const signature = computed(() => summarySignature(live.value?.summary));

function navigate(route: 'llm-settings' | 'documents-settings') {
  if (!settings.profileId) return;
  void router.push({ name: route, params: { profile: settings.profileId } });
}

onMounted(async () => {
  await nextTick();
  titleEl.value?.focus();
});
</script>

<template>
  <section
    class="icv"
    :aria-label="`Indexed content of ${target.name}`"
    @keydown.esc.stop.prevent="emit('back')"
  >
    <header class="icv-head">
      <button type="button" class="icv-back" @click="emit('back')">
        <Icon icon="mdi:arrow-left" aria-hidden="true" />
        Back to files
      </button>
      <h2 ref="titleEl" class="icv-title" tabindex="-1" :title="target.path">
        <Icon icon="mdi:text-box-search-outline" class="icv-title-icon" aria-hidden="true" />
        <span class="icv-title-text">{{ target.name }}</span>
      </h2>
      <div class="icv-sub">Indexed content</div>
    </header>

    <div class="icv-body">
      <IndexedContent
        v-if="fid"
        :fid="fid"
        :path="target.path"
        :conversation-id="panel.scopeConversationId"
        :live-signature="signature"
        @navigate="navigate"
      />

      <div v-else-if="live" class="icv-explain" :class="`tone-${live.tone}`">
        <div class="icv-status">
          <Icon :icon="live.icon" aria-hidden="true" />
          <span>{{ live.label }}</span>
        </div>
        <p>{{ live.detail }}</p>
        <p v-if="live.kind === 'excluded' || live.kind === 'unmatched'">
          <a
            href="#"
            class="icv-link"
            @click.prevent="navigate('documents-settings')"
          >Open My Documents settings</a>
        </p>
      </div>

      <div v-else class="icv-explain">
        <p v-if="store.source.enabled === false">
          "Search my documents" is off, so this file's index status is not shown.
        </p>
        <p v-else>Checking the index…</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.icv {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  background: var(--bg-color);
  color: var(--text-primary);
}
.icv-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px 8px;
  border-bottom: 1px solid var(--border-color);
  background: var(--surface-color);
  flex-shrink: 0;
}
.icv-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  align-self: flex-start;
  padding: 2px 6px 2px 2px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
  font-size: 0.78rem;
  cursor: pointer;
}
.icv-back:hover { color: var(--primary-color); background: var(--hover-bg); }
.icv-back:focus-visible,
.icv-title:focus-visible,
.icv-link:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: 1px;
}
.icv-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  min-width: 0;
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-primary);
  outline: none;
}
.icv-title-icon { flex-shrink: 0; color: var(--text-secondary); }
.icv-title-text {
  min-width: 0;
  overflow-wrap: anywhere;
}
.icv-sub {
  font-size: 0.72rem;
  color: var(--text-tertiary);
}
.icv-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 10px;
}
.icv-explain {
  --tone: var(--text-secondary);
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--surface-color);
  font-size: 0.82rem;
  line-height: 1.45;
  color: var(--text-secondary);
}
.icv-explain p { margin: 0; }
.icv-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 600;
  color: var(--tone);
}
.tone-ok { --tone: var(--success-color); }
.tone-info { --tone: var(--primary-color); }
.tone-warn { --tone: var(--warning-color); }
.tone-danger { --tone: var(--danger-color); }
.tone-muted { --tone: var(--text-secondary); }
.icv-link { color: var(--primary-color); text-decoration: underline; }
</style>
