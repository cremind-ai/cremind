<script setup lang="ts">
/**
 * Sign a coding agent in by running its own ``login`` command in a PTY.
 *
 * Claude's CLI has no non-interactive login: it prints a URL, waits for the
 * user to paste a code back, and writes the credential into its own config
 * home. So Cremind spawns the real binary under a terminal and lets the user
 * answer it — no shell access to the server needed.
 *
 * This is a dialog rather than a tab in the workspace terminal panel because
 * that panel only mounts in the chat views; Settings has nowhere to put a live
 * terminal otherwise.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ElButton, ElDialog } from 'element-plus';

import TerminalSession from '../TerminalSession.vue';
import { useSettingsStore } from '../../stores/settings';
import {
  openCodingAgentLoginTerminal, type CodingAgentStatus,
} from '../../services/configApi';
import { closeTerminal } from '../../services/terminalApi';

const props = defineProps<{
  modelValue: boolean;
  agent: CodingAgentStatus;
  /** Which CLI home the login lands in. Defaults to this profile's own; the
   *  shared server login is admin-only and must be asked for explicitly. */
  scope?: 'profile' | 'shared';
}>();

const emit = defineEmits<{
  'update:modelValue': [value: boolean];
  /** The login command exited. Whether it succeeded is the probe's answer,
   *  not ours — the page re-checks. */
  done: [];
}>();

const settings = useSettingsStore();

const starting = ref(false);
const error = ref('');
const terminalId = ref('');
const command = ref('');
const cliHome = ref('');
const exited = ref(false);

const sessionRef = ref<InstanceType<typeof TerminalSession> | null>(null);

// The exposed members are refs the expose proxy unwraps at runtime; reading
// through the proxy still tracks them, so this computed stays reactive. Same
// pattern (and the same `as any`) as views/ProcessTerminal.vue.
const status = computed<string>(() => (sessionRef.value?.status as any) ?? 'connecting');

watch(status, (value) => {
  if (value === 'exited' && !exited.value) {
    exited.value = true;
    emit('done');
  }
});

async function start() {
  starting.value = true;
  error.value = '';
  terminalId.value = '';
  command.value = '';
  cliHome.value = '';
  exited.value = false;
  try {
    const res = await openCodingAgentLoginTerminal(
      settings.agentUrl, settings.authToken, props.agent.tool_id,
      { scope: props.scope, cols: 100, rows: 24 },
    );
    terminalId.value = res.terminal_id;
    command.value = res.command;
    cliHome.value = res.cli_home;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    starting.value = false;
  }
}

/** A login terminal counts against the profile's terminal budget, so it must
 *  not outlive the dialog. A terminal that already exited answers 404, which
 *  the API client treats as success. */
function release() {
  const tid = terminalId.value;
  terminalId.value = '';
  if (!tid) return;
  void closeTerminal(settings.agentUrl, settings.authToken, tid)
    .catch(() => { /* best effort — the server reaps terminals anyway */ });
}

function close() {
  emit('update:modelValue', false);
}

/** The dialog is controlled: its own close button reports intent, the page
 *  owns the flag that hides it. */
function onVisibilityChange(value: boolean) {
  if (!value) close();
}

watch(() => props.modelValue, (open) => {
  if (open) void start();
  else release();
});

onBeforeUnmount(release);
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="`Sign in to ${agent.display_name}`"
    width="760px"
    :close-on-click-modal="false"
    destroy-on-close
    @update:model-value="onVisibilityChange"
  >
    <p class="login-intro">
      Follow the prompts below. If no browser opens, visit the URL the command
      prints and paste the code back in here.
    </p>

    <p v-if="command" class="login-command">
      <code>{{ command }}</code>
      <span v-if="cliHome" class="login-home">→ {{ cliHome }}</span>
    </p>

    <div v-if="error" class="login-error">{{ error }}</div>
    <div v-else-if="starting || !terminalId" class="login-note">Opening a terminal…</div>

    <div v-if="terminalId" class="login-terminal">
      <TerminalSession
        ref="sessionRef"
        :key="terminalId"
        :pid="terminalId"
        kind="terminal"
        :show-header="false"
      />
    </div>

    <p v-if="exited" class="login-exited">
      The sign-in command has finished. Close this dialog — Cremind re-checks the
      credential straight away.
    </p>

    <template #footer>
      <ElButton v-if="error" type="primary" :loading="starting" @click="start">Try again</ElButton>
      <ElButton :type="exited ? 'primary' : 'default'" @click="close">Close</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.login-intro {
  margin: 0 0 10px 0;
  font-size: 0.85rem;
  line-height: 1.5;
  color: var(--text-secondary);
}

.login-command {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 10px 0;
  font-size: 0.78rem;
  color: var(--text-tertiary);
}
.login-command code {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  background: var(--hover-bg);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  padding: 2px 6px;
  color: var(--text-primary);
  word-break: break-all;
}
.login-home {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  word-break: break-all;
}

.login-note { font-size: 0.85rem; color: var(--text-secondary); padding: 8px 0; }
.login-error {
  font-size: 0.85rem;
  line-height: 1.5;
  color: var(--danger-color, #f56c6c);
  padding: 8px 0;
}

/* xterm sizes itself to its host, so the host has to have a height of its
   own — inside a dialog there is no flex parent to inherit one from. */
.login-terminal {
  height: 380px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  overflow: hidden;
}

.login-exited {
  margin: 10px 0 0 0;
  font-size: 0.82rem;
  color: var(--success-color);
}
</style>
