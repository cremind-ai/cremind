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
import { ElButton, ElDialog, ElInput } from 'element-plus';

import TerminalSession from '../TerminalSession.vue';
import { useSettingsStore } from '../../stores/settings';
import {
  openCodingAgentLoginTerminal, updateToolConfig, type CodingAgentStatus,
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

/**
 * How long a running login command may stay silent before the dialog stops
 * pretending everything is fine.
 *
 * Every CLI here prints its prompt (or its URL) within a second or two of
 * starting, so anything past this is not slowness — it is the headless failure
 * described on ``silentCli`` below.
 */
const SILENT_CLI_MS = 15000;

/** The per-profile secret the Claude CLI reads instead of running a browser
 *  flow. The name is the CLI's own environment variable, so the runner can
 *  hand it straight to the process. */
const OAUTH_TOKEN_VARIABLE = 'CLAUDE_CODE_OAUTH_TOKEN';

const starting = ref(false);
const error = ref('');
const terminalId = ref('');
const command = ref('');
const cliHome = ref('');
const exited = ref(false);
/** The command is up but has printed nothing for ``SILENT_CLI_MS``, which on a
 *  server almost always means the CLI is waiting on a browser that can never
 *  open. Nothing is broken enough to error on, so it becomes a hint plus the
 *  token fallback rather than a failure. */
const silentCli = ref(false);

const sessionRef = ref<InstanceType<typeof TerminalSession> | null>(null);

// The exposed members are refs the expose proxy unwraps at runtime; reading
// through the proxy still tracks them, so this computed stays reactive. Same
// pattern (and the same `as any`) as views/ProcessTerminal.vue.
const status = computed<string>(() => (sessionRef.value?.status as any) ?? 'connecting');
const outputSeen = computed<boolean>(() => Boolean(sessionRef.value?.outputSeen as any));
const exitCode = computed<number | null>(() => (sessionRef.value?.exitCode as any) ?? null);

/** The terminal exists but its stream is gone (the token wait timed out, or the
 *  socket was rejected or dropped). TerminalSession toasts that, and a toast is
 *  gone in four seconds — without saying it here the dialog would go on reading
 *  "Opening a terminal…" over a box that will never fill. */
const streamDead = computed(() => status.value === 'disconnected');

/** A non-zero exit is the CLI telling us it did not sign in; its own output is
 *  still on screen above, which is the only place the reason exists. */
const exitFailed = computed(() => exitCode.value !== null && exitCode.value !== 0);

let silenceTimer: ReturnType<typeof setTimeout> | null = null;

function clearSilenceTimer() {
  if (silenceTimer !== null) {
    clearTimeout(silenceTimer);
    silenceTimer = null;
  }
}

/** Arm the watchdog for a stream that has started but said nothing yet. */
function armSilenceTimer() {
  clearSilenceTimer();
  if (outputSeen.value) return;
  silenceTimer = setTimeout(() => {
    silenceTimer = null;
    if (outputSeen.value) return;
    silentCli.value = true;
    // This is the exact moment the fallback is the answer, so open it for the
    // user instead of leaving it collapsed under a terminal that never spoke.
    showToken.value = true;
  }, SILENT_CLI_MS);
}

watch(status, (value) => {
  if (value === 'running') armSilenceTimer();
  if (value === 'exited' && !exited.value) {
    exited.value = true;
    clearSilenceTimer();
    // A command that has ended is not waiting on a browser any more, so the
    // hint would be describing a state that no longer exists. The fallback it
    // opened stays open — a silent command that then exited did not sign in.
    silentCli.value = false;
    emit('done');
  }
  if (value === 'disconnected') {
    clearSilenceTimer();
    showToken.value = true;
  }
});

// The first byte proves the CLI is talking to us, so the watchdog has nothing
// left to warn about — including the case where output arrives after the hint
// already appeared.
watch(outputSeen, (seen) => {
  if (!seen) return;
  clearSilenceTimer();
  silentCli.value = false;
});

/**
 * Which sign-in attempt is the live one.
 *
 * The POST spawns a real PTY on the server, so its result has to be either
 * adopted or closed — never dropped. Checking ``modelValue`` on the way back is
 * not enough to decide which: the page never lowers the flag when it unmounts
 * this dialog, and a close-then-reopen raises it again while the first request
 * is still in flight. Both leave a login terminal running against the profile's
 * budget of ten, after which every later sign-in answers 409. A counter is what
 * the answer actually depends on: only the newest attempt may adopt a terminal,
 * and every superseded one closes the terminal it opened.
 */
let attemptSeq = 0;

async function start() {
  // A retry after a dead stream still has a live terminal on the server, and a
  // login terminal counts against that same budget.
  release();
  const attempt = ++attemptSeq;
  starting.value = true;
  error.value = '';
  terminalId.value = '';
  command.value = '';
  cliHome.value = '';
  exited.value = false;
  tokenError.value = '';
  tokenSaved.value = false;
  try {
    const res = await openCodingAgentLoginTerminal(
      settings.agentUrl, settings.authToken, props.agent.tool_id,
      { scope: props.scope, cols: 100, rows: 24 },
    );
    if (attempt !== attemptSeq || !props.modelValue) {
      void closeTerminal(settings.agentUrl, settings.authToken, res.terminal_id)
        .catch(() => { /* best effort — the server reaps terminals anyway */ });
      return;
    }
    terminalId.value = res.terminal_id;
    command.value = res.command;
    cliHome.value = res.cli_home;
  } catch (e) {
    // A superseded attempt's failure is not this dialog's news: the attempt the
    // user is looking at has its own outcome to report.
    if (attempt !== attemptSeq) return;
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (attempt === attemptSeq) starting.value = false;
  }
}

/** A login terminal counts against the profile's terminal budget, so it must
 *  not outlive the dialog. A terminal that already exited answers 404, which
 *  the API client treats as success. */
function release() {
  clearSilenceTimer();
  silentCli.value = false;
  const tid = terminalId.value;
  terminalId.value = '';
  if (!tid) return;
  void closeTerminal(settings.agentUrl, settings.authToken, tid)
    .catch(() => { /* best effort — the server reaps terminals anyway */ });
}

// ── The headless door: a token pasted from a machine that does have a browser ──

/** Only Claude's CLI can mint a long-lived token to paste back (``claude
 *  setup-token``); Codex has its own device-code flow and never reaches this
 *  dialog. */
const supportsTokenFallback = computed(() => props.agent.tool_id === 'claude_code');

const showToken = ref(false);
const tokenValue = ref('');
const tokenSaving = ref(false);
const tokenError = ref('');
const tokenSaved = ref(false);

async function saveToken() {
  const value = tokenValue.value.trim();
  tokenError.value = '';
  tokenSaved.value = false;
  // Secrets read back from the server as a literal '***', so pasting what the
  // form showed — or nothing at all — would replace a working token with a
  // placeholder that fails every later run.
  if (!value || value === '***') {
    tokenError.value = 'Paste the token that `claude setup-token` printed. An empty '
      + 'or masked value would overwrite the token already stored.';
    return;
  }
  tokenSaving.value = true;
  try {
    // Only this one key: the endpoint writes exactly the variables it is given,
    // and every other secret would arrive as its '***' mask and clobber the
    // real value.
    await updateToolConfig(
      settings.agentUrl, settings.authToken, props.agent.tool_id,
      { [OAUTH_TOKEN_VARIABLE]: value },
    );
    tokenSaved.value = true;
    tokenValue.value = '';
    emit('done');
  } catch (e) {
    tokenError.value = e instanceof Error ? e.message : String(e);
  } finally {
    tokenSaving.value = false;
  }
}

function close() {
  emit('update:modelValue', false);
}

/** The dialog is controlled: its own close button reports intent, the page
 *  owns the flag that hides it. */
function onVisibilityChange(value: boolean) {
  if (!value) close();
}

// `immediate` is load-bearing, not a nicety: the page mounts this dialog behind
// a `v-if` in the same tick that it sets the flag it binds to `modelValue`, so
// the component is created with `modelValue` already true and a lazy watch never
// sees a change. That is what made the very first click open a dialog that said
// "Opening a terminal…" for ever, having asked the server for nothing at all.
// With `modelValue` false at creation the release branch is a no-op.
watch(() => props.modelValue, (open) => {
  if (open) void start();
  else release();
}, { immediate: true });

// Unmount is the one close the page never announces: it flips the `v-if` while
// leaving `modelValue` true, so an in-flight start() would still read the dialog
// as open and adopt a terminal into a component that no longer exists. Retiring
// the attempt number here is what turns that into the close path above.
onBeforeUnmount(() => {
  attemptSeq += 1;
  release();
});
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

    <div v-if="streamDead" class="login-error">
      The connection to the sign-in terminal was lost, so nothing more will
      appear above. Try again, or use the token below.
    </div>

    <div v-else-if="silentCli" class="login-hint">
      The command has printed nothing for a while. On a server this almost always
      means the CLI is trying to open a browser it cannot reach — it will wait
      like this indefinitely. Use the token fallback below instead.
    </div>

    <p v-if="exited" class="login-exited" :class="{ 'login-exited-failed': exitFailed }">
      The sign-in command has finished{{ exitCode !== null ? ` (exit code ${exitCode})` : '' }}.
      <template v-if="exitFailed">
        It did not sign in — its own output above says why.
      </template>
      <template v-else>
        Close this dialog — Cremind re-checks the credential straight away.
      </template>
    </p>

    <section v-if="supportsTokenFallback" class="login-token">
      <button type="button" class="login-token-toggle" @click="showToken = !showToken">
        <span class="login-token-caret">{{ showToken ? '▾' : '▸' }}</span>
        No browser on this server? Use a token instead
      </button>

      <div v-if="showToken" class="login-token-body">
        <ol class="login-token-steps">
          <li>
            On your own computer — one with a browser, and signed in to a Claude
            subscription — run <code>claude setup-token</code>.
          </li>
          <li>Paste the token it prints here. It is stored as a secret for this profile only.</li>
        </ol>
        <div class="login-token-row">
          <ElInput
            v-model="tokenValue"
            type="password"
            show-password
            placeholder="Paste the token from claude setup-token"
            :disabled="tokenSaving"
          />
          <ElButton type="primary" :loading="tokenSaving" @click="saveToken">Save token</ElButton>
        </div>
        <p v-if="tokenError" class="login-token-error">{{ tokenError }}</p>
        <p v-else-if="tokenSaved" class="login-token-ok">
          Token saved. Cremind is re-checking the credential now.
        </p>
      </div>
    </section>

    <template #footer>
      <ElButton
        v-if="error || streamDead"
        type="primary"
        :loading="starting"
        @click="start"
      >Try again</ElButton>
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
.login-exited-failed { color: var(--danger-color, #f56c6c); }

.login-hint {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--warning-color, #e6a23c);
  border-radius: 6px;
  font-size: 0.82rem;
  line-height: 1.5;
  color: var(--text-secondary);
}

.login-token { margin-top: 12px; }

/* A bare button rather than a collapse component: this is one disclosure, and
   Element Plus's collapse brings its own borders and padding that fight the
   dialog's. */
.login-token-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--primary-color, #409eff);
}
.login-token-toggle:hover { text-decoration: underline; }
.login-token-caret { font-size: 0.7rem; }

.login-token-body {
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  background: var(--hover-bg);
}

.login-token-steps {
  margin: 0 0 10px 0;
  padding-left: 18px;
  font-size: 0.8rem;
  line-height: 1.6;
  color: var(--text-secondary);
}
.login-token-steps code {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  color: var(--text-primary);
  word-break: break-all;
}

.login-token-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.login-token-error {
  margin: 8px 0 0 0;
  font-size: 0.8rem;
  line-height: 1.5;
  color: var(--danger-color, #f56c6c);
}
.login-token-ok {
  margin: 8px 0 0 0;
  font-size: 0.8rem;
  color: var(--success-color, #67c23a);
}
</style>
