<script setup lang="ts">
/**
 * Per-file Google Drive access, as a section of Settings -> GSuite.
 *
 * Cremind holds the `drive.file` scope, so the "granted files" list is whatever
 * Google says the token can reach — there is no local grant registry to keep in
 * sync. Unlinking the account is **not** here: the Accounts section above owns that
 * for every Google skill uniformly, and two buttons doing the same thing on one
 * page is worse than one. What stays here is the Drive-specific reason it matters —
 * that Google offers no per-file revoke.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  ElButton, ElCard, ElInput, ElMessage, ElTable, ElTableColumn, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import {
  cancelDriveGrant, completeDriveGrant, getDriveGrant, getDriveStatus,
  listDriveFiles, startDriveGrant,
  type DriveFile, type DriveStatus,
} from '../../services/googleDriveApi';

const props = defineProps<{ profile: string }>();
const settings = useSettingsStore();

const loading = ref(true);
// null until the server has actually answered. "Not linked" is only ever
// rendered from a real answer — never from a request we skipped or that failed,
// which would tell the user their account is unlinked when it isn't.
const status = ref<DriveStatus | null>(null);
const statusError = ref('');
const files = ref<DriveFile[]>([]);
const filesMessage = ref('');
const nextPageToken = ref<string | null>(null);

const granting = ref(false);
const grantState = ref('');
const grantUrl = ref('');
const captureHint = ref('');
const fileRef = ref('');
const pastedRedirect = ref('');
// Set when polling gives up (timeout, denial, or a state the server forgot).
// The banner has to survive that — it carries the picker link and the paste box,
// which is exactly what the user needs at the moment we stop waiting for them.
const manualHint = ref('');

// Polling a grant round: the grant lands with Google on approval, so the server
// discovers it by re-listing reachable files even when the redirect never
// arrives. That is why this polls instead of waiting on a callback.
const POLL_MS = 2500;
const MAX_POLLS = 120;
let pollTimer: number | undefined;
let popup: Window | null = null;

const linked = computed(() => status.value?.linked === true);
const stale = computed(() => status.value?.scopes_stale === true);

function shortType(mime: string): string {
  if (!mime) return '';
  if (mime === 'application/vnd.google-apps.folder') return 'Folder';
  if (mime === 'application/vnd.google-apps.document') return 'Google Doc';
  if (mime === 'application/vnd.google-apps.spreadsheet') return 'Google Sheet';
  if (mime === 'application/vnd.google-apps.presentation') return 'Google Slides';
  return mime.split('/').pop() || mime;
}

async function loadStatus(): Promise<boolean> {
  // The token lives only in the Pinia store, and on a reload (or a pasted URL)
  // this view mounts before App.vue's onMounted — Vue runs children first. The
  // router guard normally activates it for us; do it here too so the page never
  // depends on that ordering.
  if (!settings.authToken && props.profile) settings.activateProfile(props.profile);
  if (!settings.agentUrl || !settings.authToken) return false;
  try {
    status.value = await getDriveStatus(settings.agentUrl, settings.authToken);
    statusError.value = '';
    return true;
  } catch (err) {
    statusError.value = err instanceof Error ? err.message : String(err);
    ElMessage.error(statusError.value);
    return false;
  }
}

async function loadFiles(pageToken?: string) {
  if (!settings.agentUrl || !settings.authToken) return;
  const page = await listDriveFiles(settings.agentUrl, settings.authToken, pageToken);
  filesMessage.value = page.message || '';
  files.value = pageToken ? [...files.value, ...page.files] : page.files;
  nextPageToken.value = page.next_page_token ?? null;
}

// Activating the profile inside ``loadStatus`` writes ``authToken``, which trips
// the watcher below — without this guard the first load would run twice.
let refreshing = false;

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  loading.value = true;
  try {
    if (!(await loadStatus())) return;
    if (linked.value) await loadFiles();
  } finally {
    loading.value = false;
    refreshing = false;
  }
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

function finishGrant(count: number) {
  stopPolling();
  granting.value = false;
  grantUrl.value = '';
  captureHint.value = '';
  pastedRedirect.value = '';
  manualHint.value = '';
  fileRef.value = '';
  if (popup && !popup.closed) popup.close();
  popup = null;
  if (count > 0) {
    ElMessage.success(`Granted access to ${count} file${count === 1 ? '' : 's'}.`);
  }
  void loadFiles();
}

/** Stop waiting, but keep the banner (and its picker link + paste box) on screen. */
function giveUpWaiting(hint: string) {
  stopPolling();
  granting.value = false;
  manualHint.value = hint;
}

async function onGrant() {
  if (!settings.agentUrl || !settings.authToken) return;
  // Open the window FIRST, synchronously in the click handler. The authorize URL
  // does not exist yet, but an await before window.open spends the user-gesture
  // token and the browser blocks the popup — so open a blank one now and navigate
  // it once the server answers. Same pattern as services/hubPublish.ts.
  popup = window.open('about:blank', 'cremind-google-drive', 'width=620,height=700');
  granting.value = true;
  manualHint.value = '';
  const refs = fileRef.value.trim() ? [fileRef.value.trim()] : undefined;
  const started = await startDriveGrant(settings.agentUrl, settings.authToken, { fileIds: refs });
  if (started.error || !started.authorize_url || !started.state) {
    if (popup && !popup.closed) popup.close();
    popup = null;
    granting.value = false;
    ElMessage.warning(started.message || 'Could not start a Drive grant.');
    return;
  }
  grantState.value = started.state;
  grantUrl.value = started.authorize_url;
  captureHint.value = started.capture_hint || '';
  if (popup && !popup.closed) {
    popup.location.href = started.authorize_url;
  } else {
    ElMessage.warning('The browser blocked the Google window — use "open it here" below.');
  }

  let polls = 0;
  pollTimer = window.setInterval(async () => {
    polls += 1;
    if (polls > MAX_POLLS) {
      giveUpWaiting(
        'Timed out waiting for the file picker. If you already approved, paste the URL '
        + 'your browser landed on below — or open the picker again.',
      );
      return;
    }
    try {
      const out = await getDriveGrant(settings.agentUrl, settings.authToken, grantState.value);
      if (out.status === 'error') {
        giveUpWaiting(
          `${out.error || 'The Google consent was denied.'} You can open the picker again to retry.`,
        );
        return;
      }
      if (out.status === 'unknown' || out.status === 'timeout') {
        // The server forgot this round (in-flight grants live in memory, so a
        // restart drops them). Pasting cannot help — only a fresh round can.
        giveUpWaiting(
          'The server no longer recognizes this grant round — it may have restarted. '
          + 'Cancel and click "Grant access" to start a new one.',
        );
        return;
      }
      if (out.status === 'completed' && out.files.length) {
        if (out.note) ElMessage.warning(out.note);
        finishGrant(out.files.length);
      }
    } catch {
      // A transient poll failure is not fatal; the next tick retries.
    }
  }, POLL_MS);
}

async function onPasteRedirect() {
  if (!pastedRedirect.value.trim() || !settings.agentUrl || !settings.authToken) return;
  try {
    const out = await completeDriveGrant(
      settings.agentUrl, settings.authToken, pastedRedirect.value.trim(),
    );
    if (!out.files.length) {
      ElMessage.warning(out.note || 'No files were granted from that URL.');
      return;
    }
    finishGrant(out.files.length);
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

function onCancelGrant() {
  if (settings.agentUrl && settings.authToken && grantState.value) {
    void cancelDriveGrant(settings.agentUrl, settings.authToken, grantState.value);
  }
  finishGrant(0);
}

onMounted(() => { void refresh(); });
// A token arriving after mount (login flow, session refresh) means the first
// load ran without one — retry once it lands.
watch(() => settings.authToken, (token, previous) => {
  if (token && !previous) void refresh();
});
onUnmounted(() => { stopPolling(); });

// The Accounts section owns unlinking, so the page tells us to reload after one:
// our cached status would otherwise still name an account that is gone.
async function reload() {
  status.value = null;
  files.value = [];
  await refresh();
}
defineExpose({ reload });
</script>

<template>
  <div class="drive-section">
    <p class="group-note">
      Cremind has <strong>per-file</strong> Drive access: it can only open files you
      pick here, plus files it creates itself. Pasting a Drive link is not enough —
      the file has to be granted. Spreadsheets and documents are different: the
      gsheets and gdocs skills read and write those straight from a URL, with no
      grant needed.
    </p>

    <ElCard class="section" shadow="never">
      <div v-if="loading" class="muted">Loading…</div>
      <template v-else-if="statusError">
        <div class="row">
          <Icon icon="mdi:alert-circle-outline" class="row-icon" />
          <div class="grow">
            <strong>Couldn't check your Drive access</strong>
            <p class="muted">{{ statusError }}</p>
          </div>
          <ElButton @click="refresh">Retry</ElButton>
        </div>
      </template>
      <template v-else-if="!status">
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
      <template v-else-if="!linked">
        <div class="row">
          <Icon icon="mdi:link-off" class="row-icon" />
          <div>
            <strong>Not linked</strong>
            <p class="muted">
              Ask the agent to link the <code>gdrive</code> skill to your Google
              account, then come back to grant files.
            </p>
          </div>
        </div>
      </template>
      <template v-else>
        <div class="row">
          <Icon icon="mdi:google-drive" class="row-icon" />
          <div class="grow">
            <strong>{{ status?.email || 'Linked' }}</strong>
            <p class="muted">
              Access: {{ status?.access_model }}<template v-if="status?.access_note">
                — {{ status.access_note }}</template>
            </p>
          </div>
          <ElButton :loading="granting" type="primary" @click="onGrant">
            Grant access
          </ElButton>
        </div>

        <div v-if="stale" class="banner warn">
          <Icon icon="mdi:alert" />
          <span>{{ status?.hint }}</span>
        </div>

        <div class="grant-row">
          <ElInput
            v-model="fileRef"
            placeholder="Optional: paste a Drive link to pre-select that file"
            clearable
          />
        </div>

        <div v-if="granting || manualHint" class="banner">
          <div class="grow">
            <p v-if="granting">
              Waiting for you to pick files in the Google window. If it didn't open,
              <a :href="grantUrl" target="_blank" rel="noopener">open it here</a>.
            </p>
            <p v-else>
              {{ manualHint }}
              <a v-if="grantUrl" :href="grantUrl" target="_blank" rel="noopener">
                Open the picker
              </a>
            </p>
            <p v-if="captureHint" class="muted">{{ captureHint }}</p>
            <div class="paste-row">
              <ElInput
                v-model="pastedRedirect"
                placeholder="…or paste the URL your browser landed on"
                clearable
                @keyup.enter="onPasteRedirect"
              />
              <ElButton :disabled="!pastedRedirect.trim()" @click="onPasteRedirect">
                Use URL
              </ElButton>
            </div>
          </div>
          <ElButton text @click="onCancelGrant">Cancel</ElButton>
        </div>
      </template>
    </ElCard>

    <ElCard v-if="linked && !loading" class="section" shadow="never">
      <template #header>
        <div class="card-head">
          <span>Files Cremind can open</span>
          <ElButton text size="small" @click="() => loadFiles()">
            <Icon icon="mdi:refresh" /> Refresh
          </ElButton>
        </div>
      </template>

      <p v-if="filesMessage" class="muted">{{ filesMessage }}</p>
      <p v-else-if="!files.length" class="muted">
        Nothing granted yet. Use <strong>Grant access</strong> above to pick files.
      </p>
      <ElTable v-else :data="files" size="small" style="width: 100%">
        <ElTableColumn prop="name" label="Name" min-width="220" />
        <ElTableColumn label="Type" width="130">
          <template #default="{ row }">{{ shortType(row.mime_type) }}</template>
        </ElTableColumn>
        <ElTableColumn label="Granted via" width="120">
          <template #default="{ row }">
            <ElTag v-if="row.origin === 'picker'" size="small" type="success">Picked</ElTag>
            <ElTag v-else size="small" type="info">Created</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="" width="60">
          <template #default="{ row }">
            <a v-if="row.web_view_link" :href="row.web_view_link" target="_blank" rel="noopener">
              <Icon icon="mdi:open-in-new" />
            </a>
          </template>
        </ElTableColumn>
      </ElTable>
      <div v-if="nextPageToken" class="more-row">
        <ElButton text size="small" @click="() => loadFiles(nextPageToken!)">Load more</ElButton>
      </div>
    </ElCard>

    <ElCard v-if="linked && !loading" class="section" shadow="never">
      <template #header><span>Giving access back</span></template>
      <p class="muted">
        Google offers no per-file revoke, so there is no way to hand back a single
        file. <strong>Unlinking the account</strong> — under Accounts above — revokes
        every file grant at once, and re-linking does not bring them back, so the
        files have to be picked again. Removing Cremind at
        <a :href="status?.revoke_url" target="_blank" rel="noopener">your Google
        account connections</a> does the Google half by hand, if you would rather.
      </p>
    </ElCard>
  </div>
</template>

<style scoped>
.group-note {
  color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 16px;
  max-width: 640px;
}
.section { margin-bottom: 16px; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.row { display: flex; align-items: center; gap: 12px; }
.row-icon { font-size: 1.5rem; color: var(--text-secondary); flex: none; }
.grow { flex: 1; min-width: 0; }
.muted { color: var(--text-secondary); font-size: 0.875rem; margin: 4px 0 0; }
.banner {
  display: flex; align-items: flex-start; gap: 10px; margin-top: 14px;
  padding: 10px 12px; border-radius: 6px; background: var(--bg-color-page, rgba(0, 0, 0, 0.03));
  font-size: 0.875rem;
}
.banner.warn { color: var(--el-color-warning); }
.banner p { margin: 0 0 6px; }
.grant-row { margin-top: 14px; }
.paste-row { display: flex; gap: 8px; margin-top: 8px; }
.more-row { margin-top: 10px; text-align: center; }
</style>
