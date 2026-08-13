<script setup lang="ts">
/**
 * Which Google account each Google skill uses, as a section of Settings -> GSuite.
 *
 * Linking happens in chat (each Google skill owns its own OAuth token and runs its
 * own consent flow); this is the other half — see what is linked, and take a link
 * apart. It owns unlinking for *every* Google skill, so the Drive section below
 * does not repeat the control.
 *
 * Partial outcomes are rendered as a persistent panel, never a toast: "your grant
 * is still live at Google" has to survive longer than four seconds.
 */
import { computed, onMounted, ref, watch } from 'vue';
import { ElButton, ElCard, ElMessage, ElMessageBox, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import {
  getGoogleAccounts, unlinkAllGoogle, unlinkGoogleSkill, unlinkConsequence,
  type GoogleAccountsPayload, type GoogleSkillRow, type GoogleUnlinkResult,
} from '../../services/googleApi';

const props = defineProps<{ profile: string }>();
/** Tells the page an unlink landed, so the Drive section can drop its stale status. */
const emit = defineEmits<{ changed: [] }>();
const settings = useSettingsStore();

const loading = ref(true);
// null until the server has actually answered. "Not linked" is only ever rendered
// from a real answer — never from a request we skipped or that failed, which would
// tell the user their account is unlinked when it isn't.
const payload = ref<GoogleAccountsPayload | null>(null);
const loadError = ref('');
const busy = ref('');
/** Outcomes worth keeping on screen: a failed revoke, or a surviving file. */
const notices = ref<GoogleUnlinkResult[]>([]);

const ICONS: Record<string, string> = {
  gcalendar: 'mdi:calendar',
  gdrive: 'mdi:google-drive',
  gmail: 'mdi:email-outline',
  gsheets: 'mdi:google-spreadsheet',
  gdocs: 'mdi:file-document-outline',
};

const installed = computed(() =>
  (payload.value?.skills || []).filter((row) => row.installed),
);
const linkedRows = computed(() => installed.value.filter((row) => row.linked));
const sharedGroups = computed(() =>
  (payload.value?.accounts || []).filter((group) => group.shared_grant),
);

function icon(skill: string): string {
  return ICONS[skill] || 'mdi:google';
}

function listenerLabel(row: GoogleSkillRow): string {
  if (!row.listener.declared) return '';
  return row.listener.autostart_rows > 0 ? 'listener registered' : 'listener not started';
}

async function refresh() {
  if (!settings.authToken) {
    loading.value = false;
    return;
  }
  loading.value = true;
  loadError.value = '';
  try {
    payload.value = await getGoogleAccounts(settings.agentUrl, settings.authToken);
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

/** Keep the outcome on screen when there is something the user must act on. */
function record(result: GoogleUnlinkResult) {
  if (result.still_linked || (result.revoke_attempted && !result.revoked)) {
    notices.value = [result, ...notices.value.filter((n) => n.skill !== result.skill)];
    return true;
  }
  notices.value = notices.value.filter((n) => n.skill !== result.skill);
  return false;
}

function dismiss(skill: string) {
  notices.value = notices.value.filter((n) => n.skill !== skill);
}

async function onUnlink(row: GoogleSkillRow) {
  try {
    await ElMessageBox.confirm(unlinkConsequence(row), `Unlink ${row.label}?`, {
      confirmButtonText: 'Unlink',
      cancelButtonText: 'Cancel',
      type: 'warning',
    });
  } catch {
    return; // cancelled
  }
  busy.value = row.skill;
  try {
    const result = await unlinkGoogleSkill(settings.agentUrl, settings.authToken, row.skill);
    if (record(result)) {
      ElMessageBox.alert(result.message, `${row.label}: action needed`, {
        confirmButtonText: 'OK',
        type: result.still_linked ? 'error' : 'warning',
      });
    } else if (result.unlinked) {
      ElMessage.success(`Unlinked ${row.label}`);
    } else {
      ElMessage.info(`${row.label} was not linked`);
    }
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = '';
    await refresh();
    emit('changed');
  }
}

async function onUnlinkAll() {
  const who = (payload.value?.accounts || [])
    .map((group) => `${group.email} (${group.skills.join(', ')})`)
    .join('; ');
  const body = [
    `Unlink every Google account for this profile${who ? `: ${who}` : ''}.`,
    'Cremind loses every Google capability. Drive file grants are lost permanently, '
      + 'any listeners stop and are deregistered, and the Calendar & Schedule page falls '
      + 'back to its own credential or the built-in calendar.',
  ].join('\n\n');
  try {
    await ElMessageBox.confirm(body, 'Unlink all Google accounts?', {
      confirmButtonText: 'Unlink all',
      cancelButtonText: 'Cancel',
      type: 'warning',
    });
  } catch {
    return;
  }
  busy.value = '__all__';
  try {
    const out = await unlinkAllGoogle(settings.agentUrl, settings.authToken);
    const flagged = (out.results || []).filter((result) => record(result));
    if (flagged.length || out.failed?.length) {
      ElMessageBox.alert(out.message, 'Some links need attention', {
        confirmButtonText: 'OK',
        type: out.failed?.length ? 'error' : 'warning',
      });
    } else {
      ElMessage.success(out.message || 'Unlinked every Google account');
    }
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = '';
    await refresh();
    emit('changed');
  }
}

onMounted(async () => {
  if (!settings.authToken) await settings.activateProfile(props.profile);
  await refresh();
});

// A token arriving after mount (login flow, session refresh) means the first load
// ran without one — retry once it lands.
watch(() => settings.authToken, (token, previous) => {
  if (token && !previous) void refresh();
});
</script>

<template>
  <div class="ga-section">
    <p class="group-note">
      <strong>Linking</strong> happens in chat — ask the agent to link the skill you
      need. Unlinking here deletes this machine's copy of the credentials and revokes
      Cremind's access at Google.
    </p>

    <ElCard v-for="notice in notices" :key="`notice-${notice.skill}`"
            class="section notice" shadow="never">
      <div class="row">
        <Icon :icon="notice.still_linked ? 'mdi:alert-octagon-outline' : 'mdi:alert-outline'"
              class="row-icon" />
        <div class="grow">
          <strong>{{ notice.label }}</strong>
          <p class="muted">{{ notice.message }}</p>
          <p v-if="!notice.revoked && notice.revoke_attempted" class="muted">
            Remove Cremind manually at
            <a :href="payload?.revoke_url" target="_blank" rel="noopener">
              {{ payload?.revoke_url }}</a>.
          </p>
        </div>
        <ElButton text @click="dismiss(notice.skill)">Dismiss</ElButton>
      </div>
    </ElCard>

    <ElCard class="section" shadow="never">
      <div v-if="loading" class="muted">Loading…</div>
      <template v-else-if="loadError">
        <div class="row">
          <Icon icon="mdi:alert-circle-outline" class="row-icon" />
          <div class="grow">
            <strong>Couldn't check your Google accounts</strong>
            <p class="muted">{{ loadError }}</p>
          </div>
          <ElButton @click="refresh">Retry</ElButton>
        </div>
      </template>
      <template v-else-if="!payload">
        <div class="row">
          <Icon icon="mdi:account-alert-outline" class="row-icon" />
          <div class="grow">
            <strong>Session not ready</strong>
            <p class="muted">
              This page couldn't reach the server with your profile's session.
              Sign in to <code>{{ profile }}</code> again, then reopen it.
            </p>
          </div>
          <ElButton @click="refresh">Retry</ElButton>
        </div>
      </template>
      <template v-else-if="!installed.length">
        <div class="row">
          <Icon icon="mdi:google" class="row-icon" />
          <div>
            <strong>No Google skills installed</strong>
            <p class="muted">
              Install one of the Google Suite skills (Gmail, Calendar, Drive, Sheets,
              Docs) from Settings → Tools &amp; Skills first.
            </p>
          </div>
        </div>
      </template>
      <template v-else>
        <div v-for="row in installed" :key="row.skill" class="row skill-row">
          <Icon :icon="icon(row.skill)" class="row-icon" />
          <div class="grow">
            <strong>{{ row.label }}</strong>
            <ElTag v-if="!row.enabled" size="small" type="info" class="tag">disabled</ElTag>
            <ElTag v-if="row.own_client" size="small" class="tag">own OAuth client</ElTag>
            <p v-if="row.linked" class="muted">
              {{ row.email || 'Linked' }}
              <template v-if="listenerLabel(row)"> · {{ listenerLabel(row) }}</template>
              <template v-if="row.watch.active"> · push channel active</template>
            </p>
            <p v-else class="muted">
              Not linked — ask the agent to link the <code>{{ row.skill }}</code> skill.
            </p>
          </div>
          <ElButton v-if="row.linked" type="danger" plain
                    :loading="busy === row.skill" :disabled="!!busy"
                    @click="onUnlink(row)">
            Unlink
          </ElButton>
        </div>
      </template>
    </ElCard>

    <ElCard v-if="sharedGroups.length" class="section" shadow="never">
      <div class="row">
        <Icon icon="mdi:information-outline" class="row-icon" />
        <div class="grow">
          <strong>One Google app, one grant</strong>
          <p class="muted">
            Google lists Cremind as a single app, so a grant is shared by every skill
            linked to the same address:
            <template v-for="group in sharedGroups" :key="group.email">
              <br /><code>{{ group.email }}</code> — {{ group.skills.join(', ') }}
            </template>
          </p>
          <p class="muted">
            Unlinking one of them removes its local credentials but leaves the grant
            live at Google, so the others keep working. Use <strong>Unlink all</strong>
            (or unlink each of them) to end the grant itself.
          </p>
        </div>
      </div>
    </ElCard>

    <ElCard v-if="linkedRows.length" class="section" shadow="never">
      <div class="row">
        <Icon icon="mdi:link-off" class="row-icon" />
        <div class="grow">
          <strong>Unlink all Google accounts</strong>
          <p class="muted">
            Revokes every Google grant for this profile and deletes all the stored
            credentials. Drive file grants cannot be restored by re-linking.
          </p>
        </div>
        <ElButton type="danger" plain :loading="busy === '__all__'" :disabled="!!busy"
                  @click="onUnlinkAll">
          Unlink all
        </ElButton>
      </div>
    </ElCard>
  </div>
</template>

<style scoped>
.group-note {
  color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 16px;
  max-width: 640px;
}
.section { margin-bottom: 16px; }
.notice { border-left: 3px solid var(--el-color-warning); }
.row { display: flex; align-items: flex-start; gap: 12px; }
.skill-row { padding: 10px 0; border-bottom: 1px solid var(--border-color); }
.skill-row:last-child { border-bottom: none; }
.row-icon { font-size: 22px; color: var(--text-secondary); flex-shrink: 0; margin-top: 2px; }
.grow { flex: 1; min-width: 0; }
.tag { margin-left: 8px; }
.muted {
  color: var(--text-secondary); font-size: 0.8125rem; margin: 4px 0 0;
  overflow-wrap: anywhere;
}
</style>
