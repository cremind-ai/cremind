<script setup lang="ts">
import { computed, reactive, watch } from 'vue';
import {
  ElCheckbox, ElCheckboxGroup, ElFormItem, ElInput, ElOption,
  ElRadioButton, ElRadioGroup, ElSelect, ElSwitch, ElTimePicker,
} from 'element-plus';
import {
  NOTIFICATION_KINDS, NOTIFICATION_SOURCE_KINDS,
  defaultNotificationFilter, type NotificationFilter,
} from '../../services/channelApi';

const props = defineProps<{ modelValue?: NotificationFilter | null }>();
const emit = defineEmits<{ (e: 'update:modelValue', v: NotificationFilter): void }>();

// Friendly labels for the raw notification kinds.
const KIND_LABELS: Record<string, string> = {
  event_run_completed: 'Automation completed',
  event_run_failed: 'Automation failed',
  event_run_pending: 'Automation needs input',
  completed: 'Task completed',
  error: 'Task error',
  started: 'Task started',
  skill_register_required: 'Skill needs registering',
};
const SOURCE_LABELS: Record<string, string> = {
  schedule: 'Schedule',
  file_watcher: 'File watcher',
  skill_event: 'Skill event',
};

function clone(f?: NotificationFilter | null): NotificationFilter {
  const base = defaultNotificationFilter();
  if (!f) return base;
  return {
    ...base,
    ...f,
    kinds: [...(f.kinds || [])],
    exclude_kinds: [...(f.exclude_kinds ?? base.exclude_kinds)],
    source_kinds: [...(f.source_kinds || [])],
    subscription_ids: [...(f.subscription_ids || [])],
    conversation_ids: [...(f.conversation_ids || [])],
    keywords: [...(f.keywords || [])],
    quiet_hours: { ...base.quiet_hours, ...(f.quiet_hours || {}) },
  };
}

const state = reactive<NotificationFilter>(clone(props.modelValue));

// Guard against the v-model feedback loop: our own emit changes props.modelValue,
// which would otherwise re-seed `state` (new nested array/object identities), which
// re-triggers the deep watch, which emits again — Vue aborts this as "maximum
// recursive updates". `lastSync` is the serialized snapshot `state` currently
// reflects; we skip re-seeding on an echo and skip emitting on an external reset.
const snap = (o: unknown) => JSON.stringify(o ?? null);
let lastSync = snap(clone(props.modelValue));

// Re-seed only when the parent genuinely swaps in a different filter (dialog reopen).
watch(
  () => props.modelValue,
  (v) => {
    const incoming = snap(clone(v));
    if (incoming === lastSync) return; // echo of our own emit — ignore
    Object.assign(state, clone(v));
    lastSync = snap(state);
  },
);

// Push real edits up as a fresh object so the parent's config round-trips.
watch(
  state,
  () => {
    const cur = snap(state);
    if (cur === lastSync) return; // no real change (e.g. just re-seeded)
    lastSync = cur;
    emit('update:modelValue', JSON.parse(cur));
  },
  { deep: true },
);

// Whether "task activity" (started/completed/error) is delivered. We model
// this as toggling those kinds in exclude_kinds while leaving channel_otp
// (which the backend hard-drops anyway) untouched.
function csvBinding(key: 'subscription_ids' | 'conversation_ids' | 'keywords') {
  return computed<string>({
    get: () => (state[key] as string[]).join(', '),
    set: (v: string) => {
      state[key] = v.split(',').map((s) => s.trim()).filter(Boolean);
    },
  });
}
const subscriptionIdsText = csvBinding('subscription_ids');
const conversationIdsText = csvBinding('conversation_ids');
const keywordsText = csvBinding('keywords');

// "Included kinds" checkbox group: bound to state.kinds (allowlist). Empty =
// all kinds allowed (then exclude_kinds applies). We expose it as an explicit
// allowlist toggle for power users.
const useAllowlist = computed<boolean>({
  get: () => state.kinds.length > 0,
  set: (on: boolean) => { if (!on) state.kinds = []; },
});
</script>

<template>
  <div class="notif-filter">
    <ElFormItem label="Importance">
      <ElRadioGroup v-model="state.min_priority">
        <ElRadioButton value="all">All notifications</ElRadioButton>
        <ElRadioButton value="high">Important only (high priority)</ElRadioButton>
      </ElRadioGroup>
    </ElFormItem>

    <ElFormItem label="Exclude these kinds">
      <ElCheckboxGroup v-model="state.exclude_kinds">
        <ElCheckbox v-for="k in NOTIFICATION_KINDS" :key="k" :value="k">
          {{ KIND_LABELS[k] || k }}
        </ElCheckbox>
      </ElCheckboxGroup>
      <div class="hint">
        Login codes (OTP) are always excluded for security. Leave the rest
        unchecked to receive them.
      </div>
    </ElFormItem>

    <ElFormItem label="Only these kinds (allowlist)">
      <ElSwitch v-model="useAllowlist" />
      <ElCheckboxGroup v-if="useAllowlist" v-model="state.kinds" style="margin-top: 6px">
        <ElCheckbox v-for="k in NOTIFICATION_KINDS" :key="k" :value="k">
          {{ KIND_LABELS[k] || k }}
        </ElCheckbox>
      </ElCheckboxGroup>
      <div class="hint">When on, ONLY the checked kinds are delivered (overrides the exclude list).</div>
    </ElFormItem>

    <ElFormItem label="Only from these sources">
      <ElCheckboxGroup v-model="state.source_kinds">
        <ElCheckbox v-for="s in NOTIFICATION_SOURCE_KINDS" :key="s" :value="s">
          {{ SOURCE_LABELS[s] || s }}
        </ElCheckbox>
      </ElCheckboxGroup>
      <div class="hint">Leave empty for any source. Only applies to automation/event runs.</div>
    </ElFormItem>

    <ElFormItem label="Only these automations (subscription ids)">
      <ElInput v-model="subscriptionIdsText" placeholder="comma-separated ids; empty = any" />
    </ElFormItem>

    <ElFormItem label="Only these conversations (ids)">
      <ElInput v-model="conversationIdsText" placeholder="comma-separated ids; empty = any" />
    </ElFormItem>

    <ElFormItem label="Keyword match">
      <ElInput v-model="keywordsText" placeholder="comma-separated words; empty = any" />
      <div class="keywords-mode">
        Match
        <ElSelect v-model="state.keywords_mode" size="small" style="width: 90px">
          <ElOption value="any" label="any" />
          <ElOption value="all" label="all" />
        </ElSelect>
        of the words (in title or preview).
      </div>
    </ElFormItem>

    <ElFormItem label="Quiet hours">
      <ElSwitch v-model="state.quiet_hours.enabled" />
      <div v-if="state.quiet_hours.enabled" class="quiet-hours">
        <div class="qh-row">
          <span>From</span>
          <ElTimePicker
            v-model="state.quiet_hours.start"
            format="HH:mm" value-format="HH:mm" :clearable="false"
          />
          <span>to</span>
          <ElTimePicker
            v-model="state.quiet_hours.end"
            format="HH:mm" value-format="HH:mm" :clearable="false"
          />
        </div>
        <ElInput
          v-model="state.quiet_hours.tz"
          placeholder="IANA timezone, e.g. Asia/Ho_Chi_Minh (empty = server local)"
        />
        <ElCheckbox v-model="state.quiet_hours.allow_high">
          Still deliver important (high-priority) notifications during quiet hours
        </ElCheckbox>
      </div>
    </ElFormItem>
  </div>
</template>

<style scoped>
.notif-filter :deep(.el-checkbox) { margin-right: 16px; }
.hint { font-size: 0.78rem; color: var(--text-secondary); margin-top: 4px; line-height: 1.4; }
.keywords-mode { display: flex; align-items: center; gap: 6px; margin-top: 6px; font-size: 0.85rem; color: var(--text-secondary); }
.quiet-hours { margin-top: 8px; display: flex; flex-direction: column; gap: 8px; width: 100%; }
.qh-row { display: flex; align-items: center; gap: 8px; }
</style>
