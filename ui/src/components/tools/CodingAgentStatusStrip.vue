<script setup lang="ts">
/**
 * The sign-in state of one coding agent (Claude Code / Codex), shown above the
 * agent's ordinary tool configuration.
 *
 * The login belongs to the agent's own CLI, not to Cremind's LLM provider
 * settings — the Claude CLI keeps its credential entirely independently of the
 * Anthropic provider — so everything here talks about *this server's* CLI: which
 * home the credential lives in, whether it is this profile's own login or the
 * server-wide one every profile inherits, and which command Cremind would run to
 * change it. The buttons only raise intent; the page owns the dialogs and the
 * confirms, so there is one install/sign-in flow on the page rather than two.
 */
import { computed } from 'vue';
import { ElAlert, ElButton, ElTag, ElTooltip } from 'element-plus';
import { Icon } from '@iconify/vue';

import type { CodingAgentStatus } from '../../services/configApi';

const props = defineProps<{
  agent: CodingAgentStatus;
  /** Payload of the last ``/probe`` for this agent, verbatim, or null when no
   *  check has run yet. Left untyped on purpose: the status leaf owns the
   *  shape, and this strip only reads the few fields it can render. */
  probe?: Record<string, unknown> | null;
  probing?: boolean;
  signingOut?: boolean;
  /** Whether the viewer may sign out the shared server login (admin only —
   *  it is the fallback every other profile inherits). */
  isAdmin?: boolean;
}>();

const emit = defineEmits<{
  /** Install the agent's pip extra. The page owns the streaming install
   *  dialog every other built-in tool uses. */
  install: [agent: CodingAgentStatus];
  'sign-in': [agent: CodingAgentStatus];
  check: [agent: CodingAgentStatus];
  'sign-out': [agent: CodingAgentStatus];
}>();

/**
 * Where the credential comes from, in words rather than the internal label.
 * The keys are the runners' own ``credential_source`` strings; anything
 * unrecognised falls through to the raw label, which is still more useful to
 * the user than hiding it.
 */
const CREDENTIAL_LABELS: Record<string, string> = {
  tool_variable_api_key: 'API key (tool variable)',
  // A subscription token pasted in from a machine that has a browser — the way
  // a headless server gets signed in at all, so it must not read as the raw
  // label when it is the credential actually in use.
  tool_variable_oauth_token: 'Subscription token (tool variable)',
  profile_claude_login: 'Signed in (this profile)',
  profile_codex_login: 'Signed in (this profile)',
  host_claude_login: 'Signed in (shared server login)',
  host_codex_login: 'Signed in (shared server login)',
};

/**
 * Which credential this strip is talking about. The listing's
 * ``credential_source`` is Cremind's own ranking of what it can see; a probe
 * asks the CLI which credential it would really use, and the two can disagree
 * (a stale tool-variable key ranked above the OAuth login the CLI actually
 * holds). Once a probe has answered, its verdict and its source must be the
 * same credential, or the chip would name one while the "Signed in" tag was
 * earned by another.
 */
const credentialSource = computed<string | null>(() => {
  const probed = props.probe?.credential_source;
  if (typeof probed === 'string' && probed) return probed;
  return props.agent.credential_source ?? null;
});

const credentialLabel = computed(() => {
  const source = credentialSource.value;
  if (!source) return 'Not signed in';
  // Every env_* source is one story for the user — a key put on the server
  // process, not something this profile configured — so they share a label
  // instead of naming the variable.
  if (source.startsWith('env_')) return 'Server environment';
  return CREDENTIAL_LABELS[source] ?? source;
});

/** First non-empty string among `keys`, so a field the payload spells
 *  differently per agent (plan vs subscription) still reaches the user. */
function firstText(source: Record<string, unknown> | null, keys: string[]): string {
  if (!source) return '';
  for (const key of keys) {
    const value = source[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

/** The probe's account beats the listing's hint: a probe ran the CLI just now,
 *  while the hint was read off disk when the page last loaded. */
const account = computed<Record<string, unknown> | null>(() => {
  const probed = props.probe?.account;
  if (probed && typeof probed === 'object') return probed as Record<string, unknown>;
  return props.agent.account_hint ?? null;
});

const accountLine = computed(() => {
  const email = firstText(account.value, ['email', 'account_email']);
  const qualifier = firstText(account.value, [
    'plan_type', 'subscription_type', 'plan', 'org_name', 'organization_name', 'org',
  ]);
  if (email && qualifier) return `${email} · ${qualifier}`;
  return email || qualifier;
});

/** Signing out is offered for a login this viewer actually owns. A profile
 *  falling back to the shared server login must not be able to sign every
 *  other profile out of it — only an admin can, and only deliberately. */
const canSignOut = computed(() => {
  const scope = props.agent.credential_scope;
  if (scope === 'profile') return true;
  if (scope === 'shared') return Boolean(props.isAdmin);
  return false;
});

const signOutLabel = computed(() =>
  props.agent.credential_scope === 'shared' ? 'Sign out (shared login)' : 'Sign out',
);

/**
 * Set when this server cannot execute the agent's CLI at all — the binary is
 * present and correct but built for a CPU level this host does not provide, so
 * every invocation of it hangs instead of answering. It outranks every other
 * state on this strip: nothing that goes through the CLI can work, and no
 * setting here changes that. Null on a backend that predates the check, which
 * is why the strip degrades to exactly its old behaviour.
 */
const hostBlock = computed(() => props.agent.cli_blocked ?? null);

/** Why Sign in is unavailable, or '' when it is available. Sign-in runs the
 *  CLI binary, which can be missing even when the SDK is installed — and, on a
 *  host that cannot run the binary, can be present and still useless. */
const signInBlockedReason = computed(() => {
  // The host block comes first because it is the only reason the user cannot
  // act on: installing the feature or pointing Cremind at another copy of the
  // binary would just produce a binary that hangs in the same way, so naming
  // either of those instead would send them down a road with no end.
  if (hostBlock.value) return hostBlock.value.message;
  if (props.agent.cli_available) return '';
  const name = props.agent.display_name;
  if (!props.agent.sdk_installed) {
    return `Install ${name} first — its command-line tool ships with the feature.`;
  }
  return (
    `Cremind cannot find the ${name} command-line tool on this server, so it has `
    + 'no login command to run. Set its CLI path variable below, or install the '
    + 'CLI on the server.'
  );
});

function probeField(key: string): string {
  const value = props.probe?.[key];
  return typeof value === 'string' ? value : '';
}

const probeLoggedIn = computed<boolean | null>(() => {
  const value = props.probe?.logged_in;
  return typeof value === 'boolean' ? value : null;
});

const probeTag = computed<{ label: string; type: 'success' | 'danger' | 'info' }>(() => {
  if (probeField('error')) return { label: 'Check failed', type: 'danger' };
  if (probeLoggedIn.value === true) return { label: 'Signed in', type: 'success' };
  if (probeLoggedIn.value === false) return { label: 'Not signed in', type: 'danger' };
  return { label: 'Unknown', type: 'info' };
});

const probeDetail = computed(() =>
  probeField('error') || probeField('message') || probeField('probe_detail'),
);
</script>

<template>
  <div class="agent-strip">
    <div class="agent-chips">
      <ElTag :type="agent.sdk_installed ? 'success' : 'info'" size="small" effect="plain">
        {{ agent.sdk_installed ? 'Installed' : 'Not installed' }}
      </ElTag>
      <ElTag :type="agent.enabled ? 'success' : 'info'" size="small" effect="plain">
        {{ agent.enabled ? 'Enabled' : 'Off' }}
      </ElTag>
      <ElTooltip
        :disabled="!agent.cli_home"
        :content="`Login home: ${agent.cli_home}`"
        placement="top"
      >
        <ElTag
          :type="credentialSource ? 'info' : 'warning'"
          size="small"
          effect="plain"
        >
          Credential: {{ credentialLabel }}
        </ElTag>
      </ElTooltip>
    </div>

    <p v-if="accountLine" class="agent-account">
      <Icon icon="mdi:account-circle-outline" class="agent-account-icon" />
      <span>{{ accountLine }}</span>
    </p>

    <!-- The backend already writes the same sentence into `agent.message`, so
         showing both would tell the user the bad news twice. -->
    <ElAlert
      v-if="hostBlock"
      type="error"
      :closable="false"
      show-icon
      class="agent-host-block"
    >
      <template #title>{{ agent.display_name }} cannot run on this server</template>
      <p class="agent-host-block-line">{{ hostBlock.message }}</p>
      <p class="agent-host-block-line">{{ hostBlock.remedy }}</p>
    </ElAlert>
    <p v-else class="agent-message">{{ agent.message }}</p>

    <div class="agent-actions">
      <ElButton
        v-if="!agent.sdk_installed"
        type="primary"
        size="small"
        @click="emit('install', agent)"
      >
        <Icon icon="mdi:download" />&nbsp;Install
      </ElButton>

      <ElTooltip
        :disabled="!signInBlockedReason"
        :content="signInBlockedReason"
        placement="top"
      >
        <!-- A disabled button swallows pointer events, so the tooltip needs a
             live wrapper to hang off — otherwise the reason never shows. -->
        <span class="agent-action-wrap">
          <ElButton
            type="primary"
            size="small"
            :disabled="!agent.cli_available || Boolean(hostBlock)"
            @click="emit('sign-in', agent)"
          >
            <Icon icon="mdi:login-variant" />&nbsp;{{ agent.sign_in.label }}
          </ElButton>
        </span>
      </ElTooltip>

      <!-- These two stay live on a blocked host, unlike Sign in. The probe no
           longer runs the CLI when the host cannot execute it — it answers at
           once with the reason — and signing out only deletes a credential
           file, which needs no CLI and is still worth being able to do. -->
      <ElButton size="small" :loading="probing" @click="emit('check', agent)">
        Check sign-in
      </ElButton>

      <ElButton
        v-if="canSignOut"
        size="small"
        :loading="signingOut"
        @click="emit('sign-out', agent)"
      >
        {{ signOutLabel }}
      </ElButton>
    </div>

    <p class="agent-instructions">{{ agent.sign_in.instructions }}</p>

    <div v-if="probe" class="agent-probe">
      <ElTag :type="probeTag.type" size="small" effect="plain">{{ probeTag.label }}</ElTag>
      <span v-if="probeDetail" class="agent-probe-detail">{{ probeDetail }}</span>
    </div>
  </div>
</template>

<style scoped>
.agent-strip {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--border-color);
}

.agent-chips { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }

.agent-account {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 8px 0 0 0;
  font-size: 0.82rem;
  color: var(--text-primary);
}
.agent-account-icon { color: var(--text-tertiary); flex-shrink: 0; }

.agent-message {
  margin: 8px 0 0 0;
  font-size: 0.82rem;
  line-height: 1.5;
  color: var(--text-secondary);
}

/* Every colour here is painted from the app's own tokens, including the ones
   Element Plus would otherwise supply: this project never redeclares the
   `--el-color-error-*` family, so an untouched error alert keeps Element
   Plus's light defaults in the dark theme — a pale pink block whose
   description text, which *is* redeclared, turns near-white and vanishes. */
.agent-host-block {
  margin-top: 8px;
  background: color-mix(in srgb, var(--danger-color) 10%, var(--surface-color));
  border: 1px solid color-mix(in srgb, var(--danger-color) 45%, var(--border-color));
}
.agent-host-block :deep(.el-alert__title),
.agent-host-block :deep(.el-alert__icon) {
  color: var(--danger-color);
}
.agent-host-block :deep(.el-alert__title) { font-weight: 600; }
.agent-host-block-line {
  margin: 6px 0 0 0;
  font-size: 0.82rem;
  line-height: 1.5;
  color: var(--text-primary);
}

.agent-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.agent-action-wrap { display: inline-flex; }

.agent-instructions {
  margin: 8px 0 0 0;
  font-size: 0.78rem;
  line-height: 1.5;
  color: var(--text-tertiary);
}

.agent-probe {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 10px;
}
.agent-probe-detail {
  font-size: 0.8rem;
  line-height: 1.5;
  color: var(--text-secondary);
}
</style>
