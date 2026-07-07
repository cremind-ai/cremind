<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue';
import { ElButton, ElCard, ElMessage, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import {
  fetchChannelCatalogPublic,
  type ChannelCatalogEntry,
  type CreateChannelPayload,
} from '../../services/channelApi';
import ChannelEditDialog from '../channels/ChannelEditDialog.vue';

const props = defineProps<{
  agentUrl: string;
  configs: CreateChannelPayload[];
}>();

const emit = defineEmits<{
  (e: 'update', configs: CreateChannelPayload[]): void;
}>();

const catalog = ref<Record<string, ChannelCatalogEntry>>({});
const loading = ref(false);
const error = ref<string | null>(null);

// Local draft list, kept in sync with the parent. We replicate the
// parent-owned ``configs`` here so the dialog can mutate without
// relying on two-way binding; ``emit('update', drafts)`` after every
// change keeps the wizard's state authoritative for submission.
const drafts = ref<CreateChannelPayload[]>([]);

const dialogOpen = ref(false);
const dialogChannelType = ref<string>('');
const editingIndex = ref<number>(-1);

watch(
  () => props.configs,
  (configs) => {
    // Deep-clone so dialog edits don't mutate the parent's array in place.
    drafts.value = configs.map((c) => ({ ...c, config: { ...(c.config || {}) } }));
  },
  { immediate: true, deep: true },
);

const availableForAdd = computed<ChannelCatalogEntry[]>(() => {
  const taken = new Set(drafts.value.map((d) => d.channel_type));
  return Object.values(catalog.value).filter((e) => !taken.has(e.type));
});

function modeNeedsInteractiveSetup(channelType: string, modeId: string): boolean {
  const entry = catalog.value[channelType];
  if (!entry) return false;
  const mode = entry.modes.find((m) => m.id === modeId);
  return Boolean(mode && mode.setup_kind);
}

function modeLabel(channelType: string, modeId: string): string {
  const entry = catalog.value[channelType];
  return entry?.modes.find((m) => m.id === modeId)?.label || modeId;
}

function displayName(channelType: string): string {
  return catalog.value[channelType]?.display_name || channelType;
}

function iconFor(channelType: string): string {
  return catalog.value[channelType]?.icon || 'mdi:link-variant';
}

const editingDraft = computed<CreateChannelPayload | null>(() => {
  if (editingIndex.value < 0) return null;
  return drafts.value[editingIndex.value] || null;
});

function openCreate(channelType: string) {
  if (!catalog.value[channelType]) return;
  editingIndex.value = -1;
  dialogChannelType.value = channelType;
  dialogOpen.value = true;
}

function openEdit(index: number) {
  const draft = drafts.value[index];
  if (!draft) return;
  editingIndex.value = index;
  dialogChannelType.value = draft.channel_type;
  dialogOpen.value = true;
}

function removeDraft(index: number) {
  drafts.value.splice(index, 1);
  emit('update', drafts.value);
}

function handleDialogSubmit(payload: CreateChannelPayload) {
  if (editingIndex.value >= 0) {
    drafts.value[editingIndex.value] = payload;
  } else {
    drafts.value.push(payload);
  }
  dialogOpen.value = false;
  editingIndex.value = -1;
  emit('update', drafts.value);
}

async function loadCatalog() {
  loading.value = true;
  try {
    catalog.value = await fetchChannelCatalogPublic(props.agentUrl);
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load channel catalog';
    ElMessage.error(error.value);
  } finally {
    loading.value = false;
  }
}

onMounted(loadCatalog);
</script>

<template>
  <div class="step-channels">
    <h3 class="step-title">Channels (optional)</h3>
    <p class="step-description">
      Channels let external messaging platforms (Telegram, WhatsApp, Discord,
      Slack, Messenger, Zalo) talk to your Cremind agent. Add as many as you like
      now or skip and configure them later from <strong>Settings → Channels</strong>.
      Channels that need interactive pairing (WhatsApp / Zalo QR scan, Telegram code)
      will prompt you immediately after Complete Setup.
    </p>

    <div v-if="loading" class="loading">Loading channel catalog…</div>
    <div v-else-if="error" class="error">{{ error }}</div>

    <ElCard
      v-for="(draft, index) in drafts"
      :key="`${draft.channel_type}-${index}`"
      shadow="never"
      class="draft-card"
    >
      <div class="draft-row">
        <div class="draft-icon">
          <Icon :icon="iconFor(draft.channel_type)" />
        </div>
        <div class="draft-meta">
          <div class="draft-name">
            {{ displayName(draft.channel_type) }}
            <ElTag
              v-if="modeNeedsInteractiveSetup(draft.channel_type, draft.mode)"
              type="warning" size="small" effect="plain"
            >pair after setup</ElTag>
          </div>
          <div class="draft-sub">
            <span>Mode: {{ modeLabel(draft.channel_type, draft.mode) }}</span>
            <span>· Auth: {{ draft.auth_mode || 'none' }}</span>
            <span>· Reply: {{ draft.response_mode || 'normal' }}</span>
          </div>
        </div>
        <div class="draft-actions">
          <ElButton size="small" @click="openEdit(index)">Edit</ElButton>
          <ElButton size="small" type="danger" plain @click="removeDraft(index)">
            Remove
          </ElButton>
        </div>
      </div>
    </ElCard>

    <div v-if="!loading && availableForAdd.length > 0" class="add-section">
      <h4 class="add-title">Add a channel</h4>
      <div class="add-grid">
        <ElCard
          v-for="entry in availableForAdd"
          :key="entry.type"
          shadow="hover"
          class="add-card"
          :class="{ disabled: entry.implemented === false }"
          @click="entry.implemented !== false && openCreate(entry.type)"
        >
          <div class="add-card-content">
            <Icon :icon="entry.icon || 'mdi:link-variant'" class="add-icon" />
            <div>
              <div class="add-name">
                {{ entry.display_name }}
                <ElTag
                  v-if="entry.implemented === false"
                  type="info" size="small" effect="plain"
                >coming soon</ElTag>
              </div>
              <div class="add-modes">
                {{ entry.modes.map((m) => m.label).join(' · ') }}
              </div>
            </div>
          </div>
        </ElCard>
      </div>
    </div>

    <ChannelEditDialog
      v-model="dialogOpen"
      :catalog="catalog"
      :channel-type="dialogChannelType"
      :initial="editingDraft"
      :editing="editingIndex >= 0"
      @submit="handleDialogSubmit"
    />
  </div>
</template>

<style scoped>
.step-channels { padding: 8px 0; }
.step-title { font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0; }
.step-description { color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 16px 0; line-height: 1.5; }
.loading, .error { font-size: 0.85rem; color: var(--text-secondary); padding: 16px 0; }
.error { color: var(--el-color-danger); }

.draft-card { margin-bottom: 12px; }
.draft-row { display: flex; align-items: center; gap: 16px; }
.draft-icon {
  width: 36px; height: 36px; display: flex; align-items: center; justify-content: center;
  background: var(--hover-bg); border-radius: 10px; font-size: 20px;
  color: var(--primary-color); flex-shrink: 0;
}
.draft-meta { flex: 1; min-width: 0; }
.draft-name { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.draft-sub { font-size: 0.8rem; color: var(--text-secondary); margin-top: 2px; }
.draft-actions { display: flex; align-items: center; gap: 8px; }

.add-section { margin-top: 20px; }
.add-title { font-size: 0.95rem; font-weight: 600; margin: 0 0 10px 0; }
.add-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.add-card { cursor: pointer; }
.add-card.disabled { cursor: not-allowed; opacity: 0.6; }
.add-card-content { display: flex; align-items: center; gap: 12px; }
.add-icon { font-size: 28px; color: var(--primary-color); flex-shrink: 0; }
.add-name { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.add-modes { font-size: 0.8rem; color: var(--text-secondary); margin-top: 2px; }
</style>
