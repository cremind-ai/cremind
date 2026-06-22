<script setup lang="ts">
import { ref, watch } from 'vue';
import { Icon } from '@iconify/vue';

const props = defineProps<{ title: string; icon?: string; count: number }>();

// Auto-collapsed when empty, auto-open when it has items — until the user
// manually toggles, after which their choice sticks.
const userTouched = ref(false);
const open = ref(props.count > 0);
watch(() => props.count, (c) => { if (!userTouched.value) open.value = c > 0; });
function toggle() { userTouched.value = true; open.value = !open.value; }
</script>

<template>
  <section class="cs">
    <button class="cs-head" :class="{ empty: count === 0 }" @click="toggle">
      <Icon v-if="icon" :icon="icon" class="cs-icon" />
      <span class="cs-title">{{ title }}</span>
      <span class="cs-count" :class="{ zero: count === 0 }">{{ count }}</span>
      <span class="cs-spacer" />
      <Icon :icon="open ? 'mdi:chevron-down' : 'mdi:chevron-right'" class="cs-chev" />
    </button>
    <div v-show="open" class="cs-body"><slot /></div>
  </section>
</template>

<style scoped>
.cs { display: flex; flex-direction: column; }
.cs-head {
  display: flex; align-items: center; gap: 10px; width: 100%;
  background: none; border: none; cursor: pointer;
  padding: 10px 2px; text-align: left;
}
.cs-icon { font-size: 1.2rem; color: var(--primary-color); }
.cs-head.empty .cs-icon { color: var(--text-tertiary); }
.cs-title { font-size: 1.05rem; font-weight: 600; color: var(--text-primary); }
.cs-count {
  font-size: .72rem; font-weight: 600; min-width: 20px; height: 20px; padding: 0 6px;
  display: inline-grid; place-items: center; border-radius: 10px;
  background: color-mix(in srgb, var(--primary-color) 16%, transparent); color: var(--primary-color);
}
.cs-count.zero { background: var(--hover-bg); color: var(--text-tertiary); }
.cs-spacer { flex: 1; }
.cs-chev { font-size: 1.1rem; color: var(--text-tertiary); }
.cs-body { padding-top: 6px; }
</style>
