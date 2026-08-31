<script setup lang="ts">
// Landing pane for ``/#/setup-handoff`` — the HTTPS origin's half of the
// Setup Wizard's cross-origin session hand-off (CREMIND_SSL=after-setup).
//
// All the work happens in ``consumeSetupHandoff`` (router/index.ts), which
// runs as this route's ``beforeEnter`` and always redirects. This component
// therefore renders for a single frame at most; it exists so the navigation
// has a component to resolve, and so a slow store write never shows a blank
// page.
</script>

<template>
  <div class="setup-handoff">
    <div class="handoff-spinner"></div>
    <p>Signing you in…</p>
  </div>
</template>

<style scoped>
.setup-handoff {
  width: 100%;
  height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  background: var(--bg-color);
  color: var(--text-secondary);
  font-size: 0.95rem;
}
.handoff-spinner {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: 3px solid var(--border-color);
  border-top-color: var(--primary-color, var(--el-color-primary));
  animation: handoff-spin 0.9s linear infinite;
}
@keyframes handoff-spin {
  to { transform: rotate(360deg); }
}
</style>
