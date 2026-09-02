<script setup lang="ts">
/**
 * "Did this run's result reach the chat?" — one presenter, shared by the run
 * drawer's meta row and the run-history table's Delivered column so both tell
 * the same story.
 *
 * Every rule registered from a real conversation reports EACH run's result back
 * into it, so this is not a task-only chip: `deliver_to_origin` is false only
 * when there was nowhere to report (a rule bound to a reserved host
 * conversation, or one whose conversation has since been deleted), and those
 * runs render nothing at all.
 */
import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import type { EventRun } from '../../services/eventRunsApi';

const props = defineProps<{
  run: EventRun;
  /** Table-cell variant: smaller, no leading separator, no link. */
  compact?: boolean;
  /** Offer a jump to the conversation the result was reported to. */
  linkOrigin?: boolean;
}>();

const route = useRoute();
const router = useRouter();

const label = computed(() => {
  const r = props.run;
  if (!r.origin_delivered_at) return 'Owed to chat';
  if (r.origin_delivery_mode === 'read') return 'Read by the assistant';
  if (r.origin_delivery_mode === 'skipped') return 'Not delivered';
  return 'Reported to chat';
});

const icon = computed(() => {
  const r = props.run;
  if (!r.origin_delivered_at) return 'mdi:progress-clock';
  if (r.origin_delivery_mode === 'read') return 'mdi:eye-check';
  if (r.origin_delivery_mode === 'skipped') return 'mdi:cancel';
  return 'mdi:reply';
});

// "Read" is the one that needs saying: the result was folded into a turn that
// was already running, so a user hunting for "the turn where it arrived" would
// never find one. "Not delivered" needs saying too — it covers a result that
// was dropped rather than refused.
const hint = computed(() => {
  const r = props.run;
  if (!r.origin_delivered_at) {
    return 'This result is still waiting in its conversation’s inbox.';
  }
  if (r.origin_delivery_mode === 'read') {
    return 'The assistant read this result while it was already working, so it '
      + 'was used inside that turn rather than arriving as a new one.';
  }
  if (r.origin_delivery_mode === 'skipped') {
    return 'This result never reached the chat — the run was cancelled, the '
      + 'conversation waiting for it no longer exists, or the result was '
      + 'dropped as stale: older than the delivery window, or past the limit '
      + 'on how many standing results one conversation is told about at once.';
  }
  if (r.trigger_payload?.once) {
    return 'This one-shot task reported its result back into the conversation '
      + 'that registered it, and then ended.';
  }
  return 'This result was reported back into the conversation that registered '
    + 'the rule — the rule stays active and reports again on its next occurrence.';
});

const originLink = computed(() => {
  if (!props.linkOrigin || props.compact) return null;
  const cid = props.run.origin_conversation_id;
  // The drawer has no `profile` prop; every route that hosts this chip is
  // profile-scoped, so the active route carries it.
  const profile = route.params.profile;
  if (!cid || typeof profile !== 'string' || !profile) return null;
  return { profile, conversationId: cid };
});

function openOrigin() {
  const target = originLink.value;
  if (!target) return;
  router.push({
    name: 'conversation',
    params: { profile: target.profile, conversationId: target.conversationId },
  });
}
</script>

<template>
  <span
    v-if="run.deliver_to_origin"
    class="run-delivery"
    :class="{ owed: !run.origin_delivered_at, compact }"
    :title="hint"
  >
    <!-- The drawer's meta row is dot-separated; the table cell stands alone. -->
    <span v-if="!compact" aria-hidden="true">·</span>
    <Icon :icon="icon" />
    {{ label }}
    <a
      v-if="originLink"
      class="delivery-link"
      @click.stop.prevent="openOrigin"
    >
      Open conversation
    </a>
  </span>
</template>

<style scoped>
.run-delivery {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  white-space: nowrap;
}
.run-delivery.owed { color: var(--warning-color, #e6a23c); }
.run-delivery.compact {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: normal;
}
.run-delivery.compact.owed { color: var(--warning-color, #e6a23c); }
.delivery-link {
  margin-left: 4px;
  color: var(--primary-color);
  cursor: pointer;
  text-decoration: none;
}
.delivery-link:hover { text-decoration: underline; }
</style>
