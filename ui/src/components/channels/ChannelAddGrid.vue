<script setup lang="ts">
/**
 * The "Add a channel" catalogue grid.
 *
 * Only the grid of platform cards is shared. Both consumers — the Setup
 * Wizard's channel step (`StepChannelsConfig.vue`) and the Settings
 * "Channels" page (`ChannelsSettings.vue`) — draw exactly this picker, but
 * the list ABOVE it is deliberately different and stays with each host: the
 * wizard renders an unsaved `CreateChannelPayload[]` draft with a "pair after
 * setup" hint and Edit/Remove, while Settings renders live rows from the
 * channels store with status tags, an enable switch and Pair/Edit/Delete.
 *
 * `channels` is the already-filtered catalogue slice (each host works out for
 * itself which types are still free to add); `select` carries the chosen
 * type, which the wizard turns into a draft and Settings into a real channel.
 * A catalogue entry with `implemented === false` is shown greyed out and
 * never emits — the API would reject it with HTTP 400 anyway.
 */
import { ElCard, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';

import type { ChannelCatalogEntry } from '../../services/channelApi';

defineProps<{ channels: ChannelCatalogEntry[] }>();

const emit = defineEmits<{
  (e: 'select', channelType: string): void;
}>();

function pick(entry: ChannelCatalogEntry) {
  if (entry.implemented === false) return;
  emit('select', entry.type);
}
</script>

<template>
  <div class="add-grid">
    <ElCard
      v-for="entry in channels"
      :key="entry.type"
      shadow="hover"
      class="add-card"
      :class="{ disabled: entry.implemented === false }"
      @click="pick(entry)"
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
</template>

<style scoped>
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
