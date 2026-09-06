<script setup lang="ts">
// The deployment runbook shown while HTTPS is being switched on — Settings →
// HTTPS & Certificate, both the activation card and the certificate-error card.
//
// The one rule this component exists to enforce: **only a command is
// copyable**. A note is guidance the user reads (what to edit first, what to
// expect afterwards) and renders as an ordinary paragraph with no button; a
// command is one bare shell line in a monospace box whose copy button puts
// exactly that text on the clipboard. Previously every line was a <code> row
// with a copy button, so the icon happily copied prose.
//
// The server (``app/config/tls_steps.py``) owns the content and the order, and
// guarantees commands are bare — no "Run " prefix, no trailing period, no
// backticks. Grouping is derived here from where the commands sit rather than
// sent on the wire, which keeps the payload two fields wide.
import { computed } from 'vue';
import { ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useCopyToClipboard } from '../../composables/useCopyToClipboard';
import type { TlsInstructionStep } from '../../services/configApi';

const props = defineProps<{ steps: TlsInstructionStep[] }>();

// Not navigator.clipboard directly: this page is served over plain HTTP until
// HTTPS is activated, and that API is undefined in an insecure context. The
// composable falls back to execCommand.
const { copy, isCopied } = useCopyToClipboard();
async function copyCommand(text: string, key: string) {
  if (!(await copy(text, key))) ElMessage.error('Could not copy the command');
}

const HEADINGS = ['Before you start', 'Run in order', 'What to expect'] as const;

interface RenderStep extends TlsInstructionStep {
  /** Stable copy key, and the 1-based ordinal among commands only. */
  key: string;
  index: number | null;
}

const rendered = computed<RenderStep[]>(() => {
  let ordinal = 0;
  return props.steps.map((step, position) => ({
    ...step,
    key: String(position),
    index: step.kind === 'command' ? (ordinal += 1) : null,
  }));
});

const groups = computed(() => {
  const items = rendered.value;
  const positions = items
    .map((step, position) => (step.kind === 'command' ? position : -1))
    .filter((position) => position >= 0);
  const slices = positions.length
    ? [
      items.slice(0, positions[0]),
      items.slice(positions[0], positions[positions.length - 1] + 1),
      items.slice(positions[positions.length - 1] + 1),
    ]
    : [items, [], []];
  return slices
    .map((steps, i) => ({ title: HEADINGS[i], steps }))
    .filter((group) => group.steps.length > 0);
});

// A lone command, or a list with no commands at all, reads better unlabelled.
const showHeadings = computed(() => groups.value.length > 1);
</script>

<template>
  <div v-if="steps.length" class="deployment-steps">
    <div v-for="group in groups" :key="group.title" class="step-group">
      <h4 v-if="showHeadings" class="group-title">{{ group.title }}</h4>
      <template v-for="step in group.steps" :key="step.key">
        <p v-if="step.kind === 'note'" class="step-note">{{ step.text }}</p>
        <div v-else class="command-box">
          <span class="command-index">{{ step.index }}</span>
          <code class="command">{{ step.text }}</code>
          <button
            type="button"
            class="copy-icon-btn"
            :class="{ copied: isCopied(step.key) }"
            :title="isCopied(step.key) ? 'Copied!' : 'Copy command'"
            aria-label="Copy command"
            @click="copyCommand(step.text, step.key)"
          ><Icon :icon="isCopied(step.key) ? 'mdi:check' : 'mdi:content-copy'" /></button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.deployment-steps { margin: 4px 0 2px; }
.step-group + .step-group { margin-top: 18px; }
.group-title {
  font-size: 0.925rem; font-weight: 600; color: var(--text-primary);
  margin: 0 0 6px 0;
}
.step-note {
  margin: 8px 0; color: var(--text-secondary);
  font-size: 0.875rem; line-height: 1.55;
}
.command-box {
  display: flex; align-items: flex-start; gap: 8px;
  margin: 8px 0 12px; padding: 10px 12px;
  background: var(--hover-bg); border: 1px solid var(--border-color);
  border-radius: 6px;
}
.command-index {
  font-size: 0.75rem; color: var(--text-secondary);
  min-width: 1.25em; padding-top: 2px;
}
/* pre-wrap + break-word so a long helm line wraps on screen; the copied text
   is still the single line the server sent. */
.command {
  flex: 1; font-family: monospace; font-size: 0.8rem;
  line-height: 1.5; color: var(--text-primary);
  white-space: pre-wrap; word-break: break-word;
  background: none; padding: 0;
}
.copy-icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  background: none; border: none; cursor: pointer;
  padding: 1px; border-radius: 4px; font-size: 0.85rem;
  color: var(--text-secondary); transition: color 0.15s ease;
}
.copy-icon-btn:hover { color: var(--primary-color); }
.copy-icon-btn.copied { color: var(--success-color); }
</style>
