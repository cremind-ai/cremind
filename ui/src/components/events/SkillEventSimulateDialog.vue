<script setup lang="ts">
/**
 * Simulate dialog for a skill-event subscription. Extracted from SkillEventsPage
 * so both the Events table and the Tasks board can open it. Open via `open(row)`.
 * Fires a synthetic event file into the watched folder; the watchdog dispatches
 * it like a real event.
 */
import { ref } from 'vue';
import { ElButton, ElDialog, ElInput, ElMessage } from 'element-plus';
import { useSettingsStore } from '../../stores/settings';
import { useChatStore } from '../../stores/chat';
import { simulateEvent, type SkillEventSubscription } from '../../services/skillEventsApi';

const settings = useSettingsStore();
const chatStore = useChatStore();

const simulateOpen = ref(false);
const simulateTarget = ref<SkillEventSubscription | null>(null);
const simulateFilename = ref('');
const simulateContent = ref('');

function open(row: SkillEventSubscription) {
  simulateTarget.value = row;
  simulateFilename.value = '';
  simulateContent.value = '';
  simulateOpen.value = true;
}

async function fire() {
  if (!simulateTarget.value) return;
  if (!simulateContent.value.trim()) {
    ElMessage.warning('Content is required.');
    return;
  }
  // Open an SSE subscription to the target conversation BEFORE we fire
  // so the agent run streams into its bucket in real time even though we
  // are not currently viewing the conversation. The 'streaming' tracker
  // is auto-removed by the chat store when the run emits 'complete' or
  // 'error', so we don't have to clean up here.
  const targetCid = simulateTarget.value.conversation_id;
  if (targetCid) {
    chatStore.trackConversation(targetCid, 'streaming');
  }
  try {
    const result = await simulateEvent(
      settings.agentUrl,
      settings.authToken,
      simulateTarget.value.id,
      simulateContent.value,
      simulateFilename.value,
    );
    ElMessage.success(`Event fired — wrote ${result.path}`);
    simulateOpen.value = false;
  } catch (err) {
    if (targetCid) {
      chatStore.untrackConversation(targetCid, 'streaming');
    }
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

defineExpose({ open });
</script>

<template>
  <ElDialog v-model="simulateOpen" title="Simulate event" width="640px">
    <p class="dialog-info" v-if="simulateTarget">
      Fires <strong>{{ simulateTarget.event_type }}</strong> for
      <strong>{{ simulateTarget.skill_name }}</strong>.
      The file is written into the watched events folder; the watchdog picks
      it up just like a real event and deletes it after dispatch.
    </p>
    <div class="sim-field">
      <label class="sim-label">File name</label>
      <ElInput
        v-model="simulateFilename"
        placeholder="optional — e.g. my-test.md (auto-named if blank)"
      />
      <p class="sim-hint">
        Path components are stripped. <code>.md</code> is appended if missing.
      </p>
    </div>
    <div class="sim-field">
      <label class="sim-label">File content</label>
      <ElInput
        v-model="simulateContent"
        type="textarea"
        :rows="14"
        placeholder="The exact bytes that will be written to the .md file. Format depends on the skill — e.g. an imap-email event uses YAML frontmatter + markdown body."
      />
    </div>
    <template #footer>
      <ElButton @click="simulateOpen = false">Cancel</ElButton>
      <ElButton type="primary" @click="fire">Fire</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.dialog-info {
  margin: 0 0 12px 0;
  color: var(--text-secondary);
  font-size: 0.875rem;
  line-height: 1.5;
}
.sim-field {
  margin-bottom: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.sim-label {
  font-size: 0.8125rem;
  color: var(--text-secondary);
  font-weight: 500;
}
.sim-hint {
  margin: 0;
  font-size: 0.75rem;
  color: var(--text-tertiary);
}
.sim-hint code {
  background: var(--surface-color);
  padding: 1px 4px;
  border-radius: 3px;
}
</style>
