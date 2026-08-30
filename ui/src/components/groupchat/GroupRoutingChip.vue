<script setup lang="ts">
// Who a post was routed to.
//
// Routing only ever takes a turn away — every member is delivered every message
// either way — so this says which agents were asked to answer, and must never
// read as "the others did not hear it". It sits beside the bubble rather than
// inside it because the bubble renders any post and only a routed one carries
// the stamp.
//
// Three outcomes, not two: named agents, everyone, and — on an agent's own
// reply that asked nothing of the others — no one.
//
// Every sentence below assumes the named agents actually started a turn, so a
// capped post must never reach this component: on a hop-limited or flooding row
// nobody started one, and "only Mia started a turn" would be the exact opposite
// of the truth on the rows a reader opens to find out why nobody answered.
// `readRouting` is where that is enforced — it refuses a `quiet` row — because
// the guard needs the row's metadata and only a decision arrives here.
import { computed } from 'vue';
import { ElTooltip } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useGroupChatStore } from '../../stores/groupChat';
import type { GroupRouting } from '../../services/groupChatApi';

const props = defineProps<{ routing: GroupRouting }>();

const store = useGroupChatStore();

// `nobody` is read FIRST and on its own. An empty target set otherwise reads as
// `everyone` — the backend has no other way to route to zero members, and an
// empty arrow says nothing — so a nobody decision (whose target set is also
// empty) would render as the exact inverse of what happened.
const nobody = computed(() => props.routing.nobody && !props.routing.errored);

const everyone = computed(
  () => !nobody.value
    && (props.routing.everyone || props.routing.targets.length === 0),
);

const names = computed(() => props.routing.targets.map((p) => store.nameFor(p)));

const label = computed(() => {
  if (nobody.value) return 'no one';
  return everyone.value ? 'everyone' : names.value.join(', ');
});

const hint = computed(() => {
  const parts: string[] = [];
  if (props.routing.errored) {
    parts.push('The router could not run, so every agent was woken.');
  } else if (nobody.value) {
    parts.push('This reply asked nothing of the other agents, so none of them'
      + ' started a turn. Every member still received it.');
  } else if (everyone.value) {
    parts.push('Routed to every agent in the room.');
  } else {
    parts.push(`Only ${names.value.join(', ')} started a turn; everyone else`
      + ' still received the message.');
  }
  const reason = (props.routing.reason || '').trim();
  if (reason) parts.push(reason);
  if (props.routing.model) parts.push(`Model: ${props.routing.model}`);
  return parts.join(' ');
});
</script>

<template>
  <ElTooltip :content="hint" placement="top" :show-after="300">
    <span class="routing-chip" :class="{ errored: routing.errored }">
      <Icon icon="mdi:arrow-right-thin" />
      <span class="routing-names">{{ label }}</span>
    </span>
  </ElTooltip>
</template>

<style scoped>
.routing-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  max-width: 100%;
  padding: 0 6px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--surface-hover);
  color: var(--text-tertiary);
  font-size: 0.65rem;
  font-weight: 600;
  line-height: 16px;
  cursor: default;
}

.routing-chip.errored {
  border-style: dashed;
}

.routing-names {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
